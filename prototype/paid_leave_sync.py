"""Month-start paid-leave plans. No submission parsing, GAS, or attendance edits.

The remote month record is a durable outbox. Freeze before sending; retry the
same payload after an uncertain response. The receiver must enforce the unique
(source, month) key and return a matching receipt in the same transaction.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote, urlsplit

import requests


JST = timezone(timedelta(hours=9))
SOURCE = "daikokuya-shift"
CONFIRMATION_KEY = "paid_leave_confirmation"
STATE_ROOT = "integrations/paid_leave"
LEASE_SECONDS = 900


class SyncError(ValueError):
    """A safe, user-visible message; never include response bodies or secrets."""


class ConflictError(SyncError):
    pass


def month_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value):
        raise SyncError("対象月は YYYY-MM で指定してください。")
    return value


def timestamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise SyncError("日時にタイムゾーンがありません。")
    return now.astimezone(JST).isoformat(timespec="seconds")


def _date(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise SyncError("保存日時が不正です。自動送信を停止しました。") from None


def format_jst(value: str) -> str:
    return _date(value).astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")


def json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json_bytes(payload)).hexdigest()


def _days(value, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise SyncError("有給日数に未確認・負数・整数以外の値があります。確定内容を確認してください。")
    return value


def validate_employees(rows: list, month: str) -> list[dict]:
    maximum = monthrange(int(month[:4]), int(month[5:]))[1]
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise SyncError("連携対象の従業員一覧がありません。")
    result, keys, names = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise SyncError("従業員データが不正です。")
        name, employee_id = row.get("employee_name"), row.get("employee_id")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise SyncError("従業員名が不正です。")
        if employee_id is not None and (
            not isinstance(employee_id, str) or not employee_id.strip() or len(employee_id) > 100
        ):
            raise SyncError("従業員番号は先頭の0を保持した文字列で指定してください。")
        key = f"id:{employee_id}" if employee_id else f"name:{name}"
        if row.get("employee_key", key) != key or key in keys or name in names:
            raise SyncError("従業員番号または氏名が重複・不一致です。")
        keys.add(key)
        names.add(name)
        result.append({
            "employee_key": key, "employee_id": employee_id, "employee_name": name,
            "paid_leave_days": _days(row.get("paid_leave_days"), maximum),
        })
    return sorted(result, key=lambda row: row["employee_key"])


def build_confirmation(year: int, month: int, paid_days: dict, employees: list,
                       assignments: list, now: datetime) -> dict:
    """Capture known generated totals, including explicit zeros, when locking.

    Missing totals must be passed as None and rejected, not converted to {}.
    Sick days, unassigned cells and excess holidays are never inferred as leave.
    """
    ym = month_key(f"{year:04d}-{month:02d}")
    if not isinstance(paid_days, dict):
        raise SyncError("生成時の有給日数が保存されていません。この確定版は自動連携できません。")
    eligible = [e for e in employees if not e.is_auxiliary]
    names = {e.name for e in eligible}
    if set(paid_days) - names:
        raise SyncError("有給日数に従業員マスタと一致しない氏名があります。")
    assigned = {a.employee for a in assignments}
    if not names or names - assigned:
        raise SyncError("連携対象者のシフトがありません。未完成下書きや従業員を確認してください。")
    rows = validate_employees([
        {"employee_name": e.name, "employee_id": e.employee_id or None,
         "paid_leave_days": paid_days.get(e.name, 0)} for e in eligible
    ], ym)
    for row in rows:
        work_days = {a.day for a in assignments
                     if a.employee == row["employee_name"] and a.store.name != "OFF"}
        if row["paid_leave_days"] + len(work_days) > monthrange(year, month)[1]:
            raise SyncError("有給日数と実出勤日数が暦の日数を超えています。")
    return {"schema_version": 1, "month": ym, "confirmed_at": timestamp(now), "employees": rows}


@dataclass
class Document:
    value: dict
    sha: str


class GitHubSyncRepository:
    """Private backup repo only. CAS never silently overwrites concurrent edits."""

    def __init__(self, token: str, repo: str, branch: str = "main", session=None):
        if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise SyncError("GitHubバックアップの接続設定がありません。")
        self.session = session or requests.Session()
        self.base = f"https://api.github.com/repos/{repo}"
        self.branch = branch
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        self._private_checked = False

    def _request(self, method: str, path: str, **kwargs):
        try:
            return self.session.request(method, self.base + path, headers=self.headers,
                                        timeout=(5, 20), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SyncError("GitHubに接続できません。連携状態は確定していません。再確認してください。") from None

    def ensure_private(self):
        if self._private_checked:
            return
        response = self._request("GET", "")
        data = self._json(response) if response.status_code == 200 else None
        if not isinstance(data, dict) or data.get("private") is not True:
            raise SyncError("有給連携の保存先は非公開のバックアップリポジトリにしてください。")
        self._private_checked = True

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            raise SyncError("GitHubから正しいJSONを取得できませんでした。") from None

    def head(self) -> str:
        self.ensure_private()
        response = self._request("GET", f"/git/ref/heads/{quote(self.branch, safe='')}")
        if response.status_code != 200:
            raise SyncError(f"GitHubの版を取得できませんでした（HTTP {response.status_code}）。")
        data = self._json(response)
        value = data.get("object", {}).get("sha", "") if isinstance(data, dict) else ""
        if not re.fullmatch(r"[a-f0-9]{40}", value):
            raise SyncError("GitHubの版情報が不正です。")
        return value

    def _content(self, path: str, ref: Optional[str] = None):
        self.ensure_private()
        response = self._request("GET", f"/contents/{quote(path, safe='/')}", params={"ref": ref or self.branch})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SyncError(f"GitHubの連携データを取得できませんでした（HTTP {response.status_code}）。")
        return self._json(response)

    def get(self, path: str, ref: Optional[str] = None) -> Optional[Document]:
        data = self._content(path, ref)
        if data is None:
            return None
        try:
            if data["type"] != "file" or data["encoding"] != "base64":
                raise ValueError
            raw = base64.b64decode(data["content"].replace("\n", ""), validate=True)
            value = json.loads(raw)
            if not isinstance(value, dict) or not data["sha"]:
                raise ValueError
            return Document(value, data["sha"])
        except (KeyError, TypeError, ValueError):
            raise SyncError("GitHubの保存データが破損しています。ゼロ日として送信せず停止しました。") from None

    def list_names(self, path: str, ref: str) -> list[str]:
        data = self._content(path, ref)
        if data is None:
            return []
        if not isinstance(data, list) or len(data) >= 1000:
            raise SyncError("ロック履歴を完全に確認できないため送信を停止しました。")
        try:
            return [item["name"] for item in data if item["type"] == "file"]
        except (KeyError, TypeError):
            raise SyncError("ロック履歴の形式が不正です。") from None

    def put(self, path: str, value: dict, sha: Optional[str]) -> str:
        self.ensure_private()
        body = {"message": "Paid leave sync state", "branch": self.branch,
                "content": base64.b64encode(json_bytes(value)).decode("ascii")}
        if sha:
            body["sha"] = sha
        response = self._request("PUT", f"/contents/{quote(path, safe='/')}", json=body)
        if response.status_code in (409, 422):
            raise ConflictError("別の操作で連携状態が更新されました。状態を再読み込みしてください。")
        if response.status_code not in (200, 201):
            raise SyncError(f"連携状態を保存できませんでした（HTTP {response.status_code}）。送信完了とは扱いません。")
        data = self._json(response)
        result = data.get("content", {}).get("sha") if isinstance(data, dict) else None
        if not result:
            raise SyncError("保存結果を確認できません。状態を再読み込みしてください。")
        return result


def read_locked_payload(repo, month: str, now: datetime) -> dict:
    """Read lock and snapshot from ONE Git commit; old unlocks must not revive."""
    month_key(month)
    revision = repo.head()
    history = repo.list_names(f"locks/{month}", revision)
    events = []
    for name in history:
        match = re.fullmatch(r"(lock|unlock)_(\d{8}-\d{6})\.json", name)
        if match:
            events.append((match[2], match[1], name))
        elif name.startswith(("lock_", "unlock_")):
            raise SyncError("不明なロック履歴があるため送信を停止しました。")
    if events:
        _, action, name = max(events)
        if action == "unlock":
            raise SyncError("当月シフトはロック解除中です。連携待ちです。")
        lock = repo.get(f"locks/{month}/{name}", revision)
    else:
        lock = repo.get(f"locks/{month}.lock", revision)
    if lock is None:
        raise SyncError("当月のロック済みシフトがありません。連携待ちです。")
    info = lock.value
    if (info.get("year"), info.get("month")) != (int(month[:4]), int(month[5:])):
        raise SyncError("ロック情報の対象月が一致しません。")
    filename = info.get("snapshot_file", "")
    if not isinstance(filename, str) or not re.fullmatch(r"shift_finalized_[\w-]+\.json", filename):
        raise SyncError("ロック済み確定版のファイル名が不正です。")
    snapshot = repo.get(f"backups/{month}/{filename}", revision)
    if snapshot is None:
        raise SyncError("ロックに対応する確定版がありません。連携待ちです。")
    data = snapshot.value
    if data.get("kind") != "finalized" or (data.get("year"), data.get("month")) != (int(month[:4]), int(month[5:])):
        raise SyncError("確定版の対象月・種類が一致しません。")
    metadata = data.get("metadata")
    confirmation = metadata.get(CONFIRMATION_KEY) if isinstance(metadata, dict) else None
    if not isinstance(confirmation, dict) or confirmation.get("schema_version") != 1 or confirmation.get("month") != month:
        raise SyncError("この確定版には連携用の有給確認データがありません。有給日数を確認して再ロックしてください。")
    _date(confirmation.get("confirmed_at"))
    _date(info.get("locked_at"))
    return {
        "schema_version": 1, "source": SOURCE, "kind": "monthly_paid_leave_plan",
        "month": month, "sync_id": f"{SOURCE}:paid-leave:{month}",
        "prepared_at": timestamp(now), "confirmed_at": confirmation["confirmed_at"],
        "source_snapshot": {"file": filename, "git_commit": revision,
                            "sha256": payload_hash(data), "locked_at": info["locked_at"]},
        "employees": validate_employees(confirmation.get("employees"), month),
    }


def new_state(month: str) -> dict:
    return {"schema_version": 1, "month": month_key(month), "paused": False,
            "status": "waiting", "attempts": 0}


def load_state(repo, month: str) -> tuple[dict, Optional[str]]:
    document = repo.get(f"{STATE_ROOT}/{month_key(month)}.json")
    if document is None:
        return new_state(month), None
    state = copy.deepcopy(document.value)
    if (state.get("schema_version") != 1 or state.get("month") != month
            or type(state.get("paused")) is not bool
            or type(state.get("attempts")) is not int or state["attempts"] < 0
            or state.get("status") not in {"waiting", "sending", "failed", "sent"}):
        raise SyncError("連携履歴が不正です。自動送信を停止しました。")
    payload = state.get("payload")
    if state["status"] != "waiting" or payload is not None:
        if (not isinstance(payload, dict) or payload.get("month") != month
                or payload.get("source") != SOURCE
                or payload.get("sync_id") != f"{SOURCE}:paid-leave:{month}"
                or payload_hash(payload) != state.get("payload_sha256")):
            raise SyncError("保存した送信内容が変わっています。自動送信を停止しました。")
        validate_employees(payload.get("employees"), month)
        if state["status"] == "sending":
            _date(state.get("sending_at"))
        if state["status"] == "sent":
            validate_receipt(state.get("receipt"), payload, state["payload_sha256"])
    return state, document.sha


def sending_is_active(state: dict, now: datetime) -> bool:
    return state.get("status") == "sending" and now - _date(state.get("sending_at")) < timedelta(seconds=LEASE_SECONDS)


def set_paused(repo, month: str, paused: bool, now: datetime, actor: str = "manager") -> dict:
    state, sha = load_state(repo, month)
    if state["status"] == "sent":
        raise SyncError("この月は連携済みです。追加・取消は勤務表側で行ってください。")
    if sending_is_active(state, now):
        raise SyncError("送信処理中のため保留を変更できません。少し待って再確認してください。")
    if type(paused) is not bool:
        raise SyncError("保留の指定が不正です。")
    state.update(paused=paused, control_updated_at=timestamp(now), control_updated_by=actor)
    repo.put(f"{STATE_ROOT}/{month}.json", state, sha)
    return state


def validate_receipt(receipt: dict, payload: dict, digest: str) -> dict:
    total = sum(row["paid_leave_days"] for row in payload["employees"])
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("status") not in {"accepted", "already_accepted"}
            or receipt.get("sync_id") != payload["sync_id"]
            or receipt.get("month") != payload["month"]
            or receipt.get("payload_sha256") != digest
            or type(receipt.get("employee_count")) is not int
            or receipt["employee_count"] != len(payload["employees"])
            or type(receipt.get("total_paid_leave_days")) is not int
            or receipt["total_paid_leave_days"] != total):
        raise SyncError("勤務表からの受領結果が送信内容と一致しません。連携済みとは扱いません。")
    _date(receipt.get("received_at"))
    return {key: receipt[key] for key in (
        "schema_version", "status", "sync_id", "month", "payload_sha256",
        "employee_count", "total_paid_leave_days", "received_at",
    )}


class AttendanceSender:
    def __init__(self, url: str, token: str, session=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not token or len(token) < 32):
            raise SyncError("勤務表のHTTPS連携URLと、32文字以上の専用連携キーを設定してください。")
        self.url, self.token = url, token
        self.session = session or requests.Session()

    def __call__(self, payload: dict, digest: str) -> dict:
        try:
            response = self.session.post(
                self.url, data=json_bytes(payload), timeout=(5, 20), allow_redirects=False,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                         "Idempotency-Key": payload["sync_id"], "X-Payload-SHA256": digest},
            )
        except requests.RequestException:
            raise SyncError("勤務表への送信結果が不明です。同じ内容で再送し、二重登録を防ぎます。") from None
        if response.status_code == 409:
            raise SyncError("勤務表側が受付を停止しました。月締め済み、または同じ月の別データ登録を確認してください。")
        if response.status_code not in (200, 201):
            raise SyncError(f"勤務表が受け付けませんでした（HTTP {response.status_code}）。接続設定・従業員の対応付けを確認してください。")
        try:
            receipt = response.json()
        except ValueError:
            raise SyncError("勤務表の受領結果を読み取れません。同じ内容で再送してください。") from None
        return validate_receipt(receipt, payload, digest)


@dataclass
class SyncResult:
    status: str
    message: str


def sync_month(repo, sender: Callable, month: str, now: datetime,
               manual: bool = False, expected_snapshot_sha256: Optional[str] = None) -> SyncResult:
    """Auto: current month from 06:00 JST on day 1, retry on following days.

    Manual may recover earlier months, never future months. Sent is immutable.
    A CAS 'sending' claim prevents a pause or second worker racing the send.
    """
    month_key(month)
    local = _date(timestamp(now))
    current = local.strftime("%Y-%m")
    if month > current:
        return SyncResult("not_due", "対象月が始まっていないため送信しません。")
    if not manual and (month != current or (local.day == 1 and local.hour < 6)):
        return SyncResult("not_due", "月初の連携時刻前、または自動連携の対象月外です。")
    state, sha = load_state(repo, month)
    if state["status"] == "sent":
        return SyncResult("sent", "この月は連携済みです。再送・上書きはしません。")
    if state["paused"]:
        return SyncResult("paused", "今月の有給連携は保留中です。")
    if sending_is_active(state, now):
        return SyncResult("sending", "別の送信処理が実行中です。")
    if not state.get("payload"):
        payload = read_locked_payload(repo, month, now)
        state.update(payload=payload, payload_sha256=payload_hash(payload))
    if (expected_snapshot_sha256 is not None
            and state["payload"]["source_snapshot"]["sha256"] != expected_snapshot_sha256):
        raise ConflictError("確認後に確定版が変更されました。連携状態と日数を再読み込みしてください。")
    state.update(status="sending", sending_at=timestamp(now), attempts=state["attempts"] + 1)
    state.pop("error", None)
    # No request leaves this process until the frozen payload is durable.
    sha = repo.put(f"{STATE_ROOT}/{month}.json", state, sha)
    try:
        receipt = validate_receipt(sender(state["payload"], state["payload_sha256"]),
                                   state["payload"], state["payload_sha256"])
        state.update(status="sent", receipt=receipt, completed_at=timestamp(now))
    except SyncError as exc:
        state.update(status="failed", error=str(exc))
    except Exception:
        state.update(status="failed", error="予期しない送信エラーです。内容を変えずに再送してください。")
    repo.put(f"{STATE_ROOT}/{month}.json", state, sha)
    if state["status"] == "failed":
        return SyncResult("failed", state["error"])
    return SyncResult("sent", "勤務表側の受領を確認し、有給日数を連携しました。")
