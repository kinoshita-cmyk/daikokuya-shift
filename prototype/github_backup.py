"""
GitHub 自動バックアップ
================================================
従業員が希望を提出するたびに、自動的に GitHub のプライベートリポジトリへ
バックアップする。Streamlit Cloud のファイルが消えてもデータが残るよう保護。

設定:
    Streamlit Secrets に以下を追加:
        GITHUB_TOKEN = "ghp_xxx..."           Personal Access Token (Contents:Read+Write)
        GITHUB_BACKUP_REPO = "user/repo-name"  バックアップ先リポジトリ（Private 推奨）

設計:
- 失敗してもアプリ動作は止めない（ベストエフォート）
- HTTP リクエストのタイムアウトを短く設定
- 既存ファイルがあれば SHA を取得して上書き
- バックアップ先のディレクトリ構造:
    preferences/YYYY-MM/{従業員名}_TIMESTAMP.json
    shifts/YYYY-MM/finalized_TIMESTAMP.json
    config/employees_TIMESTAMP.json
"""

from __future__ import annotations
import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .submission_window import now_jst

# requests は標準では入っていないが、anthropic SDK が依存しているので利用可能
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# Streamlit Secrets / 環境変数のキー名
SECRET_GITHUB_TOKEN = "GITHUB_TOKEN"
SECRET_GITHUB_BACKUP_REPO = "GITHUB_BACKUP_REPO"

_SYNCED_PREFERENCE_MONTHS: set[str] = set()
_SYNCED_CONFIG_NAMES: set[str] = set()
_SYNCED_LOCK_MONTHS: set[str] = set()
_SYNCED_SHIFT_MONTHS: set[str] = set()


def _sanitize_debug_value(value: Any) -> Any:
    """バックアップ診断ログに秘密情報を書かないための簡易サニタイズ。"""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(word in key_text for word in ("token", "secret", "password", "api_key")):
                safe[str(key)] = "[redacted]"
            else:
                safe[str(key)] = _sanitize_debug_value(item)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_debug_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


def _debug_log(event: str, **details: Any) -> None:
    """GitHubバックアップまわりの診断ログをローカルに残す。"""
    try:
        from .paths import BACKUP_DIR

        log_dir = BACKUP_DIR / "debug"
        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": now_jst().isoformat(timespec="seconds"),
            "event": event,
            "details": _sanitize_debug_value(details),
        }
        log_path = log_dir / f"backup_debug_{now_jst().strftime('%Y-%m-%d')}.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _get_secret(key: str, default: str = "") -> str:
    """Streamlit Secrets または環境変数から値を取得"""
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


def is_github_backup_enabled() -> bool:
    """GitHub バックアップが利用可能か（設定済みか）"""
    return bool(
        HAS_REQUESTS
        and _get_secret(SECRET_GITHUB_TOKEN)
        and _get_secret(SECRET_GITHUB_BACKUP_REPO)
    )


def _get_token() -> str:
    return _get_secret(SECRET_GITHUB_TOKEN)


def _get_repo() -> str:
    return _get_secret(SECRET_GITHUB_BACKUP_REPO)


def _github_headers() -> dict:
    return {
        "Authorization": f"token {_get_token()}",
        "Accept": "application/vnd.github.v3+json",
    }


# ============================================================
# GitHub API ラッパー
# ============================================================

def _push_file(
    file_path_in_repo: str,
    content: bytes,
    commit_message: str,
    timeout: int = 8,
) -> tuple[bool, str]:
    """
    ファイルを GitHub の指定パスに作成または更新する。

    Returns:
        (success: bool, message: str)
    """
    if not HAS_REQUESTS:
        return False, "requests library not installed"

    token = _get_token()
    repo = _get_repo()
    if not token or not repo:
        return False, "GITHUB_TOKEN または GITHUB_BACKUP_REPO 未設定"

    url = f"https://api.github.com/repos/{repo}/contents/{file_path_in_repo}"
    headers = _github_headers()

    # 既存ファイルの SHA 取得（上書き時に必要）
    existing_sha: Optional[str] = None
    try:
        check = requests.get(url, headers=headers, timeout=timeout)
        if check.status_code == 200:
            existing_sha = check.json().get("sha")
    except Exception:
        pass  # 失敗したら新規作成として扱う

    # base64 エンコード
    content_b64 = base64.b64encode(content).decode("utf-8")
    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch": "main",
    }
    if existing_sha:
        payload["sha"] = existing_sha

    # PUT 送信
    try:
        response = requests.put(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code in (200, 201):
            return True, f"OK ({response.status_code})"
        else:
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "タイムアウト"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _fetch_repo_file(repo_path: str, timeout: int = 8) -> tuple[bool, bytes, str]:
    """GitHub上の1ファイルを取得する。"""
    if not is_github_backup_enabled():
        return False, b"", "未設定"
    repo = _get_repo()
    url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
    try:
        response = requests.get(url, headers=_github_headers(), timeout=timeout)
        if response.status_code == 404:
            return False, b"", "見つかりません"
        if response.status_code != 200:
            return False, b"", f"HTTP {response.status_code}"
        data = response.json()
        encoded = str(data.get("content", "")).replace("\n", "")
        if not encoded:
            return False, b"", "内容が空です"
        return True, base64.b64decode(encoded), "OK"
    except requests.exceptions.Timeout:
        return False, b"", "タイムアウト"
    except Exception as e:
        return False, b"", f"{type(e).__name__}: {e}"


def _fetch_github_item_content(item: dict, timeout: int = 8) -> tuple[bool, bytes, str]:
    """GitHub contents API の item からファイル内容を取得する。"""
    try:
        file_url = item.get("url")
        if not file_url:
            return False, b"", "GitHub応答にURLがありません"
        response = requests.get(file_url, headers=_github_headers(), timeout=timeout)
        if response.status_code != 200:
            return False, b"", f"HTTP {response.status_code}"
        data = response.json()
        encoded = str(data.get("content", "")).replace("\n", "")
        if not encoded:
            return False, b"", "内容が空です"
        return True, base64.b64decode(encoded), "OK"
    except requests.exceptions.Timeout:
        return False, b"", "タイムアウト"
    except Exception as e:
        return False, b"", f"{type(e).__name__}: {e}"


def _list_repo_dir(repo_path: str, timeout: int = 8) -> tuple[bool, list[dict], str]:
    """GitHub上のディレクトリ一覧を取得する。"""
    if not is_github_backup_enabled():
        return False, [], "未設定"
    repo = _get_repo()
    url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
    try:
        response = requests.get(url, headers=_github_headers(), timeout=timeout)
        if response.status_code == 404:
            return False, [], "見つかりません"
        if response.status_code != 200:
            return False, [], f"HTTP {response.status_code}"
        data = response.json()
        if not isinstance(data, list):
            return False, [], "ディレクトリではありません"
        return True, data, "OK"
    except requests.exceptions.Timeout:
        return False, [], "タイムアウト"
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"


def sync_preferences_from_github(
    year: int,
    month: int,
    local_backup_dir: Path,
    timeout: int = 8,
) -> tuple[int, str]:
    """
    GitHubバックアップに保存済みの希望提出データをローカルへ復元する。

    Streamlit Cloud は再起動時にローカル保存が消えることがあるため、
    提出状況を読む前にこの同期を行う。
    """
    if not is_github_backup_enabled():
        return 0, "未設定"

    year = int(year)
    month = int(month)
    sync_key = f"{year:04d}-{month:02d}"
    local_month_dir = Path(local_backup_dir) / sync_key
    local_month_dir.mkdir(parents=True, exist_ok=True)
    if sync_key in _SYNCED_PREFERENCE_MONTHS and any(local_month_dir.glob("preferences_*.json")):
        return 0, "同期済み"

    repo = _get_repo()
    repo_dirs = [
        f"backups/{sync_key}",
        # 古い実装や手動バックアップで使っていた可能性がある保存先。
        f"preferences/{sync_key}",
    ]
    headers = _github_headers()

    try:
        restored = 0
        checked = []
        errors = []
        for repo_dir in repo_dirs:
            url = f"https://api.github.com/repos/{repo}/contents/{repo_dir}"
            response = requests.get(url, headers=headers, timeout=timeout)
            checked.append(repo_dir)
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                errors.append(f"{repo_dir}: HTTP {response.status_code}")
                continue

            items = response.json()
            if not isinstance(items, list):
                errors.append(f"{repo_dir}: GitHub応答形式が不正")
                continue

            for item in items:
                name = str(item.get("name", ""))
                if not (name.startswith("preferences_") and name.endswith(".json")):
                    continue
                local_path = local_month_dir / name
                if local_path.exists():
                    continue
                file_url = item.get("url")
                if not file_url:
                    continue
                file_response = requests.get(file_url, headers=headers, timeout=timeout)
                if file_response.status_code != 200:
                    continue
                file_data = file_response.json()
                encoded = str(file_data.get("content", "")).replace("\n", "")
                if not encoded:
                    continue
                content = base64.b64decode(encoded)
                with open(local_path, "wb") as f:
                    f.write(content)
                restored += 1

        _SYNCED_PREFERENCE_MONTHS.add(sync_key)
        if restored:
            return restored, f"{restored}件復元（確認先: {', '.join(checked)}）"
        if errors:
            return 0, " / ".join(errors)
        return 0, f"GitHub上に提出データなし（確認先: {', '.join(checked)}）"
    except requests.exceptions.Timeout:
        return 0, "タイムアウト"
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def sync_latest_config_from_github(
    config_name: str,
    local_config_dir: Path,
    timeout: int = 8,
    force: bool = False,
) -> tuple[bool, str]:
    """
    GitHubバックアップ上の最新設定ファイルをローカルへ復元する。

    Streamlit Cloud は再起動時に実行環境内の設定変更が巻き戻るため、
    rule_config などの運用設定を読む前にこの同期を行う。
    """
    if not is_github_backup_enabled():
        return False, "未設定"

    config_name = str(config_name).strip()
    if not config_name:
        return False, "設定名が空です"
    if not force and config_name in _SYNCED_CONFIG_NAMES:
        return False, "同期済み"

    repo = _get_repo()
    headers = _github_headers()
    local_config_dir = Path(local_config_dir)
    local_config_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_config_dir / f"{config_name}.json"

    try:
        candidates = []
        # 新しい実装では latest を最優先する。
        latest_url = (
            f"https://api.github.com/repos/{repo}/contents/"
            f"config/{config_name}_latest.json"
        )
        latest_response = requests.get(latest_url, headers=headers, timeout=timeout)
        if latest_response.status_code == 200:
            candidates.append(latest_response.json())

        # 古い実装の timestamp 付き設定バックアップも拾う。
        list_url = f"https://api.github.com/repos/{repo}/contents/config"
        list_response = requests.get(list_url, headers=headers, timeout=timeout)
        if list_response.status_code == 200:
            items = list_response.json()
            if isinstance(items, list):
                prefix = f"{config_name}_"
                for item in items:
                    name = str(item.get("name", ""))
                    if (
                        name.startswith(prefix)
                        and name.endswith(".json")
                        and name != f"{config_name}_latest.json"
                    ):
                        candidates.append(item)

        if not candidates:
            _SYNCED_CONFIG_NAMES.add(config_name)
            return False, "GitHub上に設定バックアップなし"

        # latest がある場合は先頭、それ以外はファイル名順で最新を採用。
        selected = candidates[0]
        if str(selected.get("name", "")) != f"{config_name}_latest.json":
            selected = sorted(
                candidates,
                key=lambda item: str(item.get("name", "")),
                reverse=True,
            )[0]

        file_url = selected.get("url")
        if not file_url:
            return False, "GitHub応答にURLがありません"
        file_response = requests.get(file_url, headers=headers, timeout=timeout)
        if file_response.status_code != 200:
            return False, f"HTTP {file_response.status_code}"
        file_data = file_response.json()
        encoded = str(file_data.get("content", "")).replace("\n", "")
        if not encoded:
            return False, "設定ファイルの内容が空です"

        content = base64.b64decode(encoded)
        # JSONとして読めることだけ確認してから置き換える。
        json.loads(content.decode("utf-8"))
        with open(local_path, "wb") as f:
            f.write(content)
        _SYNCED_CONFIG_NAMES.add(config_name)
        return True, f"{selected.get('name')} を復元"
    except requests.exceptions.Timeout:
        return False, "タイムアウト"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def sync_latest_lock_and_snapshot_from_github(
    year: int,
    month: int,
    local_backup_dir: Path,
    local_lock_dir: Path,
    timeout: int = 8,
    force: bool = False,
) -> tuple[bool, str]:
    """
    GitHubバックアップ上のロック情報と確定シフト本体をローカルへ復元する。

    Streamlit Cloud の再起動で locks/ や backups/ が消えても、
    前月ロックを自動的に戻して連勤持ち越しへ使えるようにする。
    """
    _debug_log(
        "sync_lock_start",
        year=year,
        month=month,
        force=force,
    )
    if not is_github_backup_enabled():
        _debug_log("sync_lock_skipped", year=year, month=month, reason="not_configured")
        return False, "未設定"

    year = int(year)
    month = int(month)
    sync_key = f"{year:04d}-{month:02d}"
    if not force and sync_key in _SYNCED_LOCK_MONTHS:
        _debug_log("sync_lock_skipped", year=year, month=month, reason="already_synced")
        return False, "同期済み"

    repo = _get_repo()
    headers = _github_headers()
    local_lock_dir = Path(local_lock_dir)
    local_backup_dir = Path(local_backup_dir)
    local_lock_dir.mkdir(parents=True, exist_ok=True)
    local_month_dir = local_backup_dir / sync_key
    local_month_dir.mkdir(parents=True, exist_ok=True)
    lock_path = local_lock_dir / f"{sync_key}.lock"

    try:
        selected_action = ""
        selected_content: Optional[bytes] = None
        history_url = f"https://api.github.com/repos/{repo}/contents/locks/{sync_key}"
        history_response = requests.get(history_url, headers=headers, timeout=timeout)
        if history_response.status_code == 200:
            items = history_response.json()
            if isinstance(items, list):
                candidates = [
                    item for item in items
                    if str(item.get("name", "")).endswith(".json")
                    and (
                        str(item.get("name", "")).startswith("lock_")
                        or str(item.get("name", "")).startswith("unlock_")
                    )
                ]
                if candidates:
                    selected = sorted(candidates, key=lambda item: str(item.get("name", "")))[-1]
                    selected_action = "unlock" if str(selected.get("name", "")).startswith("unlock_") else "lock"
                    ok, content, msg = _fetch_github_item_content(selected, timeout=timeout)
                    if ok:
                        selected_content = content
                    else:
                        return False, msg

        # 旧実装/最新ミラー用。履歴がない場合だけ locks/YYYY-MM.lock を見る。
        if selected_content is None:
            ok, content, _msg = _fetch_repo_file(f"locks/{sync_key}.lock", timeout=timeout)
            if ok:
                selected_action = "lock"
                selected_content = content

        if selected_content is None:
            _SYNCED_LOCK_MONTHS.add(sync_key)
            _debug_log("sync_lock_not_found", year=year, month=month)
            return False, "GitHub上にロック情報なし"

        if selected_action == "unlock":
            if lock_path.exists():
                archive_dir = local_lock_dir / "archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = archive_dir / f"{sync_key}_unlocked_sync.json"
                lock_path.replace(archive_path)
            _SYNCED_LOCK_MONTHS.add(sync_key)
            _debug_log("sync_lock_unlocked", year=year, month=month)
            return True, f"{sync_key} はGitHub上でロック解除済み"

        lock_data = json.loads(selected_content.decode("utf-8"))
        snapshot_file = str(lock_data.get("snapshot_file", "")).strip()
        if not snapshot_file:
            _debug_log("sync_lock_failed", year=year, month=month, reason="missing_snapshot_file")
            return False, "ロック情報に確定シフト名がありません"
        with open(lock_path, "wb") as f:
            f.write(selected_content)

        snapshot_path = local_month_dir / snapshot_file
        if not snapshot_path.exists():
            ok, content, _msg = _fetch_repo_file(
                f"backups/{sync_key}/{snapshot_file}",
                timeout=timeout,
            )
            if ok:
                with open(snapshot_path, "wb") as f:
                    f.write(content)
            else:
                shifts_url = f"https://api.github.com/repos/{repo}/contents/shifts/{sync_key}"
                shifts_response = requests.get(shifts_url, headers=headers, timeout=timeout)
                if shifts_response.status_code == 200:
                    shift_items = shifts_response.json()
                    if isinstance(shift_items, list):
                        finalized = [
                            item for item in shift_items
                            if str(item.get("name", "")).startswith("finalized_")
                            and str(item.get("name", "")).endswith(".json")
                        ]
                        if finalized:
                            selected_shift = sorted(finalized, key=lambda item: str(item.get("name", "")))[-1]
                            ok, content, msg = _fetch_github_item_content(
                                selected_shift, timeout=timeout,
                            )
                            if ok:
                                with open(snapshot_path, "wb") as f:
                                    f.write(content)
                            else:
                                _debug_log(
                                    "sync_lock_failed",
                                    year=year,
                                    month=month,
                                    reason="snapshot_fetch_failed",
                                    message=msg,
                                )
                                return False, msg

        if not snapshot_path.exists():
            _debug_log(
                "sync_lock_failed",
                year=year,
                month=month,
                reason="snapshot_missing",
                snapshot_file=snapshot_file,
            )
            return False, "ロックは復元しましたが、確定シフト本体が見つかりません"

        _SYNCED_LOCK_MONTHS.add(sync_key)
        _debug_log(
            "sync_lock_success",
            year=year,
            month=month,
            snapshot_file=snapshot_file,
        )
        return True, f"{sync_key} のロックと確定シフトを復元"
    except requests.exceptions.Timeout:
        _debug_log("sync_lock_timeout", year=year, month=month)
        return False, "タイムアウト"
    except Exception as e:
        _debug_log(
            "sync_lock_exception",
            year=year,
            month=month,
            error_type=type(e).__name__,
            error=str(e),
        )
        return False, f"{type(e).__name__}: {e}"


def _github_shift_name_matches(name: str, kind: Optional[str]) -> bool:
    """GitHub上のシフトファイル名が復元対象か判定する。"""
    if not name.endswith(".json"):
        return False
    if kind:
        return (
            name.startswith(f"shift_{kind}_")
            or name.startswith(f"{kind}_")
        )
    return (
        name.startswith("shift_draft_")
        or name.startswith("shift_finalized_")
        or name.startswith("draft_")
        or name.startswith("finalized_")
    )


def _local_shift_snapshot_name(name: str) -> str:
    """
    GitHubの旧保存名を、ローカルの ShiftBackup が読める名前にそろえる。

    旧: shifts/YYYY-MM/draft_20260525-123456.json
    新: backups/YYYY-MM/shift_draft_2026-05-25_123456.json
    """
    if name.startswith("shift_"):
        return name

    for kind in ("draft", "finalized"):
        prefix = f"{kind}_"
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        raw_ts = name[len(prefix):-5]
        try:
            parsed = datetime.strptime(raw_ts, "%Y%m%d-%H%M%S")
            return f"shift_{kind}_{parsed.strftime('%Y-%m-%d_%H%M%S')}.json"
        except ValueError:
            safe_ts = "".join(c if c.isalnum() else "_" for c in raw_ts)
            return f"shift_{kind}_github_{safe_ts}.json"
    return name


def sync_shift_snapshots_from_github(
    year: int,
    month: int,
    local_backup_dir: Path,
    kind: Optional[str] = None,
    timeout: int = 8,
    force: bool = False,
) -> tuple[int, str]:
    """
    GitHubバックアップ上の生成済みシフト下書き・確定版をローカルへ復元する。

    Streamlit Cloud の再起動で backups/ が消えても、管理画面で
    「自動保存の下書きを復元」できるようにする。
    """
    if not is_github_backup_enabled():
        return 0, "未設定"

    year = int(year)
    month = int(month)
    sync_key = f"{year:04d}-{month:02d}"
    kind_key = kind or "all"
    cache_key = f"{sync_key}:{kind_key}"
    local_month_dir = Path(local_backup_dir) / sync_key
    local_month_dir.mkdir(parents=True, exist_ok=True)

    if not force and cache_key in _SYNCED_SHIFT_MONTHS:
        return 0, "同期済み"

    repo = _get_repo()
    headers = _github_headers()
    repo_dirs = [
        f"backups/{sync_key}",
        # 旧実装の保存先。ここにしか下書きがない場合も復元する。
        f"shifts/{sync_key}",
    ]

    restored = 0
    checked: list[str] = []
    errors: list[str] = []

    try:
        for repo_dir in repo_dirs:
            checked.append(repo_dir)
            url = f"https://api.github.com/repos/{repo}/contents/{repo_dir}"
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                errors.append(f"{repo_dir}: HTTP {response.status_code}")
                continue

            items = response.json()
            if not isinstance(items, list):
                errors.append(f"{repo_dir}: GitHub応答形式が不正")
                continue

            for item in items:
                name = str(item.get("name", ""))
                if item.get("type") not in (None, "file"):
                    continue
                if not _github_shift_name_matches(name, kind):
                    continue

                local_name = _local_shift_snapshot_name(name)
                local_path = local_month_dir / local_name
                if local_path.exists():
                    continue

                ok, content, msg = _fetch_github_item_content(
                    item, timeout=timeout,
                )
                if not ok:
                    errors.append(f"{repo_dir}/{name}: {msg}")
                    continue

                try:
                    json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.append(f"{repo_dir}/{name}: JSONとして読めません")
                    continue

                with open(local_path, "wb") as f:
                    f.write(content)
                restored += 1

        _SYNCED_SHIFT_MONTHS.add(cache_key)
        if restored:
            return restored, f"{restored}件復元（確認先: {', '.join(checked)}）"
        if errors:
            return 0, " / ".join(errors[:3])
        return 0, f"復元対象なし（確認先: {', '.join(checked)}）"
    except requests.exceptions.Timeout:
        return restored, "タイムアウト"
    except Exception as e:
        return restored, f"{type(e).__name__}: {e}"


# ============================================================
# 公開 API（用途別バックアップ関数）
# ============================================================

def push_preference_to_github(
    local_file_path: Path,
    employee_name: str,
    year: int,
    month: int,
) -> tuple[bool, str]:
    """
    従業員の希望提出データを GitHub にプッシュ。

    保存先: backups/YYYY-MM/preferences_TIMESTAMP_{従業員名}.json
    """
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        local_file_path = Path(local_file_path)
        if not local_file_path.exists():
            return False, "ローカルファイルが見つからない"
        ts = now_jst().strftime("%Y%m%d-%H%M%S")
        # アプリは backups/YYYY-MM/preferences_*.json を読み込むため、
        # 再起動後もそのまま復元できるパスへ保存する。
        safe_name = "".join(c if c.isalnum() else "_" for c in employee_name)
        primary_repo_path = (
            f"backups/{year:04d}-{month:02d}/"
            f"preferences_{ts}_{safe_name}.json"
        )
        legacy_repo_path = (
            f"preferences/{year:04d}-{month:02d}/"
            f"preferences_{ts}_{safe_name}.json"
        )
        content = local_file_path.read_bytes()
        commit_msg = f"Preference: {employee_name} for {year}-{month:02d}"
        primary_success, primary_msg = _push_file(
            primary_repo_path, content, commit_msg,
        )
        legacy_success, legacy_msg = _push_file(
            legacy_repo_path, content, commit_msg + " (legacy mirror)",
        )
        if primary_success and legacy_success:
            return True, f"{primary_repo_path} と {legacy_repo_path} に保存"
        if primary_success:
            return True, f"{primary_repo_path} に保存（予備保存失敗: {legacy_msg}）"
        if legacy_success:
            return True, f"{legacy_repo_path} に保存（主保存失敗: {primary_msg}）"
        return False, f"主保存失敗: {primary_msg} / 予備保存失敗: {legacy_msg}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def push_all_preferences_to_github(
    local_backup_dir: Path,
    target_ym: str | None = None,
) -> dict:
    """
    ローカルに残っている提出データをまとめて GitHub へ同期する。

    コード更新前に提出済みだったデータを、あとから GitHub バックアップへ
    押し出すための管理者用リカバリ処理。
    """
    result = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "messages": [],
    }
    if not is_github_backup_enabled():
        result["failed_count"] += 1
        result["messages"].append("GitHub自動バックアップが未設定です。")
        return result

    base_dir = Path(local_backup_dir)
    if target_ym:
        month_dirs = [base_dir / str(target_ym)]
    else:
        month_dirs = sorted(p for p in base_dir.iterdir() if p.is_dir()) if base_dir.exists() else []

    for month_dir in month_dirs:
        if not month_dir.exists():
            result["skipped_count"] += 1
            result["messages"].append(f"{month_dir.name}: ローカル提出データなし")
            continue
        try:
            year_str, month_str = month_dir.name.split("-", 1)
            year = int(year_str)
            month = int(month_str)
        except ValueError:
            result["skipped_count"] += 1
            continue

        files = sorted(month_dir.glob("preferences_*.json"))
        if not files:
            result["skipped_count"] += 1
            result["messages"].append(f"{month_dir.name}: 提出ファイルなし")
            continue

        for file_path in files:
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)
                author = str(data.get("author", "")).strip()
                if not author or author == "system":
                    result["skipped_count"] += 1
                    continue
                success, msg = push_preference_to_github(
                    file_path, author, year, month,
                )
                if success:
                    result["success_count"] += 1
                    result["messages"].append(f"{month_dir.name} / {author}: 保存OK")
                else:
                    result["failed_count"] += 1
                    result["messages"].append(f"{month_dir.name} / {author}: {msg}")
            except Exception as e:
                result["failed_count"] += 1
                result["messages"].append(f"{file_path.name}: {type(e).__name__}: {e}")

    return result


def push_shift_to_github(
    local_file_path: Path,
    year: int,
    month: int,
    kind: str = "finalized",  # "draft" / "finalized"
) -> tuple[bool, str]:
    """
    確定シフトを GitHub にプッシュ。

    保存先:
      - backups/YYYY-MM/{ローカルファイル名}（アプリが復元時にそのまま読める主保存）
      - shifts/YYYY-MM/{kind}_TIMESTAMP.json（履歴用の予備保存）
    """
    _debug_log(
        "push_shift_start",
        year=year,
        month=month,
        kind=kind,
        local_file_path=local_file_path,
    )
    if not is_github_backup_enabled():
        _debug_log("push_shift_skipped", year=year, month=month, kind=kind, reason="not_configured")
        return False, "未設定"
    try:
        local_file_path = Path(local_file_path)
        if not local_file_path.exists():
            _debug_log(
                "push_shift_failed",
                year=year,
                month=month,
                kind=kind,
                reason="local_file_missing",
                local_file_path=local_file_path,
            )
            return False, "ローカルファイルが見つからない"
        ts = now_jst().strftime("%Y%m%d-%H%M%S")
        primary_repo_path = (
            f"backups/{year:04d}-{month:02d}/"
            f"{local_file_path.name}"
        )
        legacy_repo_path = f"shifts/{year:04d}-{month:02d}/{kind}_{ts}.json"
        content = local_file_path.read_bytes()
        commit_msg = f"Shift {kind}: {year}-{month:02d}"
        primary_success, primary_msg = _push_file(
            primary_repo_path, content, commit_msg,
        )
        legacy_success, legacy_msg = _push_file(
            legacy_repo_path, content, commit_msg + " (history)",
        )
        if primary_success and legacy_success:
            _debug_log(
                "push_shift_success",
                year=year,
                month=month,
                kind=kind,
                primary_repo_path=primary_repo_path,
                legacy_repo_path=legacy_repo_path,
                primary_success=primary_success,
                legacy_success=legacy_success,
            )
            return True, f"{primary_repo_path} と {legacy_repo_path} に保存"
        if primary_success:
            _debug_log(
                "push_shift_partial_success",
                year=year,
                month=month,
                kind=kind,
                primary_repo_path=primary_repo_path,
                legacy_repo_path=legacy_repo_path,
                primary_success=primary_success,
                legacy_success=legacy_success,
                legacy_msg=legacy_msg,
            )
            return True, f"{primary_repo_path} に保存（履歴保存失敗: {legacy_msg}）"
        if legacy_success:
            _debug_log(
                "push_shift_partial_success",
                year=year,
                month=month,
                kind=kind,
                primary_repo_path=primary_repo_path,
                legacy_repo_path=legacy_repo_path,
                primary_success=primary_success,
                legacy_success=legacy_success,
                primary_msg=primary_msg,
            )
            return True, f"{legacy_repo_path} に保存（主保存失敗: {primary_msg}）"
        _debug_log(
            "push_shift_failed",
            year=year,
            month=month,
            kind=kind,
            primary_repo_path=primary_repo_path,
            legacy_repo_path=legacy_repo_path,
            primary_msg=primary_msg,
            legacy_msg=legacy_msg,
        )
        return False, f"主保存失敗: {primary_msg} / 履歴保存失敗: {legacy_msg}"
    except Exception as e:
        _debug_log(
            "push_shift_exception",
            year=year,
            month=month,
            kind=kind,
            error_type=type(e).__name__,
            error=str(e),
        )
        return False, f"{type(e).__name__}: {e}"


def push_local_file_to_github(
    local_file_path: Path,
    repo_path: str,
    commit_message: str,
) -> tuple[bool, str]:
    """任意のローカルファイルを指定パスで GitHub に保存する。"""
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        local_file_path = Path(local_file_path)
        if not local_file_path.exists():
            return False, "ローカルファイルが見つからない"
        return _push_file(repo_path, local_file_path.read_bytes(), commit_message)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def push_lock_to_github(
    local_file_path: Path,
    year: int,
    month: int,
    action: str = "lock",
) -> tuple[bool, str]:
    """ロック/解除履歴ファイルを GitHub に保存する。"""
    _debug_log(
        "push_lock_start",
        year=year,
        month=month,
        action=action,
        local_file_path=local_file_path,
    )
    ts = now_jst().strftime("%Y%m%d-%H%M%S")
    history_path = f"locks/{year:04d}-{month:02d}/{action}_{ts}.json"
    history_success, history_msg = push_local_file_to_github(
        local_file_path,
        history_path,
        f"Lock {action}: {year}-{month:02d}",
    )
    latest_success = False
    latest_msg = ""
    if action == "lock":
        latest_success, latest_msg = push_local_file_to_github(
            local_file_path,
            f"locks/{year:04d}-{month:02d}.lock",
            f"Lock latest: {year}-{month:02d}",
        )
    if action != "lock":
        _debug_log(
            "push_lock_done",
            year=year,
            month=month,
            action=action,
            history_success=history_success,
            history_msg=history_msg,
        )
        return history_success, history_msg
    if history_success and latest_success:
        _debug_log(
            "push_lock_success",
            year=year,
            month=month,
            action=action,
            history_success=history_success,
            latest_success=latest_success,
        )
        return True, f"{history_path} と locks/{year:04d}-{month:02d}.lock に保存"
    if history_success:
        _debug_log(
            "push_lock_partial_success",
            year=year,
            month=month,
            action=action,
            history_success=history_success,
            latest_success=latest_success,
            latest_msg=latest_msg,
        )
        return True, f"{history_path} に保存（latest保存失敗: {latest_msg}）"
    if latest_success:
        _debug_log(
            "push_lock_partial_success",
            year=year,
            month=month,
            action=action,
            history_success=history_success,
            latest_success=latest_success,
            history_msg=history_msg,
        )
        return True, f"latestのみ保存（履歴保存失敗: {history_msg}）"
    _debug_log(
        "push_lock_failed",
        year=year,
        month=month,
        action=action,
        history_msg=history_msg,
        latest_msg=latest_msg,
    )
    return False, f"履歴保存失敗: {history_msg} / latest保存失敗: {latest_msg}"


def diagnose_github_backup_month(
    year: int,
    month: int,
    timeout: int = 8,
) -> dict:
    """指定月のGitHubバックアップ状態を画面表示用に診断する。"""
    year = int(year)
    month = int(month)
    sync_key = f"{year:04d}-{month:02d}"
    result = {
        "enabled": is_github_backup_enabled(),
        "repo": _get_repo() if _get_repo() else "",
        "ym": sync_key,
        "lock_latest_exists": False,
        "lock_history_count": 0,
        "unlock_history_count": 0,
        "finalized_shift_count": 0,
        "draft_shift_count": 0,
        "preference_count": 0,
        "messages": [],
        "finalized_files": [],
        "draft_files": [],
        "lock_history_files": [],
    }
    _debug_log("diagnose_month_start", year=year, month=month)
    if not result["enabled"]:
        result["messages"].append("GitHub自動バックアップが未設定です。")
        _debug_log("diagnose_month_skipped", year=year, month=month, reason="not_configured")
        return result

    ok, _content, msg = _fetch_repo_file(f"locks/{sync_key}.lock", timeout=timeout)
    result["lock_latest_exists"] = bool(ok)
    result["messages"].append(f"locks/{sync_key}.lock: {'あり' if ok else msg}")

    ok, items, msg = _list_repo_dir(f"locks/{sync_key}", timeout=timeout)
    if ok:
        lock_files = [str(item.get("name", "")) for item in items if str(item.get("name", "")).endswith(".json")]
        result["lock_history_files"] = lock_files
        result["lock_history_count"] = sum(1 for name in lock_files if name.startswith("lock_"))
        result["unlock_history_count"] = sum(1 for name in lock_files if name.startswith("unlock_"))
        result["messages"].append(
            f"locks/{sync_key}/: lock履歴{result['lock_history_count']}件 / unlock履歴{result['unlock_history_count']}件"
        )
    else:
        result["messages"].append(f"locks/{sync_key}/: {msg}")

    ok, items, msg = _list_repo_dir(f"backups/{sync_key}", timeout=timeout)
    if ok:
        names = [str(item.get("name", "")) for item in items]
        finalized = [name for name in names if name.startswith("shift_finalized_") and name.endswith(".json")]
        drafts = [name for name in names if name.startswith("shift_draft_") and name.endswith(".json")]
        prefs = [name for name in names if name.startswith("preferences_") and name.endswith(".json")]
        result["finalized_files"] = finalized
        result["draft_files"] = drafts
        result["finalized_shift_count"] = len(finalized)
        result["draft_shift_count"] = len(drafts)
        result["preference_count"] = len(prefs)
        result["messages"].append(
            f"backups/{sync_key}/: 確定版{len(finalized)}件 / 下書き{len(drafts)}件 / 提出{len(prefs)}件"
        )
    else:
        result["messages"].append(f"backups/{sync_key}/: {msg}")

    ok, items, msg = _list_repo_dir(f"shifts/{sync_key}", timeout=timeout)
    if ok:
        names = [str(item.get("name", "")) for item in items]
        legacy_finalized = [name for name in names if name.startswith("finalized_") and name.endswith(".json")]
        legacy_drafts = [name for name in names if name.startswith("draft_") and name.endswith(".json")]
        if legacy_finalized or legacy_drafts:
            result["messages"].append(
                f"shifts/{sync_key}/: 旧形式 確定版{len(legacy_finalized)}件 / 下書き{len(legacy_drafts)}件"
            )
    else:
        result["messages"].append(f"shifts/{sync_key}/: {msg}")

    _debug_log("diagnose_month_done", year=year, month=month, result=result)
    return result


def github_file_exists(repo_path: str, timeout: int = 8) -> tuple[bool, str]:
    """GitHubバックアップ上に指定ファイルが存在するか確認する。"""
    ok, _content, msg = _fetch_repo_file(str(repo_path), timeout=timeout)
    return ok, msg


def push_edit_log_to_github(
    local_file_path: Path,
    year: int,
    month: int,
) -> tuple[bool, str]:
    """編集履歴ファイルを GitHub に保存する。"""
    local_file_path = Path(local_file_path)
    repo_path = f"edits/{year:04d}-{month:02d}/{local_file_path.name}"
    return push_local_file_to_github(
        local_file_path,
        repo_path,
        f"Edit log: {year}-{month:02d}",
    )


def push_config_to_github(
    config_name: str,
    config_data: dict,
) -> tuple[bool, str]:
    """
    設定ファイル（従業員マスタ、ルール設定など）を GitHub にプッシュ。

    保存先: config/{config_name}_TIMESTAMP.json
    """
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        ts = now_jst().strftime("%Y%m%d-%H%M%S")
        content = json.dumps(
            config_data, ensure_ascii=False, indent=2,
        ).encode("utf-8")
        commit_msg = f"Config update: {config_name}"
        history_path = f"config/{config_name}_{ts}.json"
        latest_path = f"config/{config_name}_latest.json"
        history_success, history_msg = _push_file(
            history_path, content, commit_msg,
        )
        latest_success, latest_msg = _push_file(
            latest_path, content, commit_msg + " (latest)",
        )
        if history_success and latest_success:
            return True, f"{history_path} と {latest_path} に保存"
        if latest_success:
            return True, f"{latest_path} に保存（履歴保存失敗: {history_msg}）"
        if history_success:
            return True, f"{history_path} に保存（latest保存失敗: {latest_msg}）"
        return False, f"履歴保存失敗: {history_msg} / latest保存失敗: {latest_msg}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_connection() -> tuple[bool, str]:
    """
    GitHub への接続を試験する（認証OK・リポジトリアクセスOK確認）。

    Returns:
        (success, message)
    """
    if not HAS_REQUESTS:
        return False, "requests ライブラリが未インストール"
    token = _get_token()
    repo = _get_repo()
    if not token:
        return False, "GITHUB_TOKEN が未設定"
    if not repo:
        return False, "GITHUB_BACKUP_REPO が未設定"

    url = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code == 200:
            data = response.json()
            return True, (
                f"✅ 接続OK！リポジトリ: {data.get('full_name')} "
                f"({'Private' if data.get('private') else 'Public'})"
            )
        elif response.status_code == 401:
            return False, "認証エラー: GITHUB_TOKEN が無効か期限切れ"
        elif response.status_code == 404:
            return False, (
                f"リポジトリが見つかりません: {repo}\n"
                "（リポジトリ名のスペル違い、または PAT に権限がない可能性）"
            )
        else:
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "タイムアウト（GitHub にアクセスできない）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【GitHub バックアップ 動作テスト】\n")

    if not HAS_REQUESTS:
        print("❌ requests ライブラリが未インストール")
        exit(1)

    print("[1] 設定確認")
    print(f"  GITHUB_TOKEN: {'✓ 設定済み' if _get_token() else '✗ 未設定'}")
    print(f"  GITHUB_BACKUP_REPO: {_get_repo() or '✗ 未設定'}")
    print(f"  is_github_backup_enabled: {is_github_backup_enabled()}")

    print("\n[2] 接続テスト")
    success, msg = test_connection()
    print(f"  結果: {msg}")

    if success:
        print("\n[3] テストファイル送信")
        ok, msg = push_config_to_github(
            "test_connection",
            {"timestamp": now_jst().isoformat(timespec="seconds"), "test": True},
        )
        print(f"  結果: {'✅' if ok else '❌'} {msg}")
