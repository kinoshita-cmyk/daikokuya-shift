"""
従業員提出データを生成エンジン用に変換するローダー
================================================
backups/YYYY-MM/preferences_*.json から実際の提出データを読み込み、
generator.generate_shift() に渡せる形式に変換する。

未提出者は off_requests に含まれず、生成エンジンで自由に配置される。
これにより「全員提出を待たなくてもシフト生成できる」運用が可能。
"""

from __future__ import annotations
import json
import re
from calendar import monthrange
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import BACKUP_DIR
from .models import Store


@dataclass
class SubmissionData:
    """ある月の全提出データを集計したもの"""
    year: int
    month: int
    off_requests: dict[str, list[int]] = field(default_factory=dict)
    work_requests: list[tuple] = field(default_factory=list)
    preferred_work_requests: list[tuple] = field(default_factory=list)
    flexible_off: list[tuple] = field(default_factory=list)
    natural_language_notes: dict[str, str] = field(default_factory=dict)
    paid_leave_days: dict[str, int] = field(default_factory=dict)
    requested_holiday_days: dict[str, int] = field(default_factory=dict)
    preferred_consecutive_off: list[tuple[str, int]] = field(default_factory=list)
    parsed_note_summaries: dict[str, dict] = field(default_factory=dict)
    submitted_employees: list[str] = field(default_factory=list)
    pending_employees: list[str] = field(default_factory=list)

    @property
    def submission_count(self) -> int:
        return len(self.submitted_employees)


def _store_from_name(name: Optional[str]) -> Optional[Store]:
    """店舗名から Store enum を逆引き"""
    if not name:
        return None
    try:
        return Store[name]
    except KeyError:
        # display_name で検索
        for s in Store:
            if s.display_name == name or s.name == name:
                return s
    return None


@dataclass
class ParsedNaturalLanguageNote:
    """自由記載から拾った、生成エンジンへ渡せる希望。"""

    off_requests: list[int] = field(default_factory=list)
    work_requests: list[tuple[int, Optional[Store]]] = field(default_factory=list)
    flexible_off: list[tuple[list[int], int]] = field(default_factory=list)
    paid_leave_days: Optional[int] = None
    requested_holiday_days: Optional[int] = None
    preferred_consecutive_off_days: Optional[int] = None
    ignored_optional_work_days: list[int] = field(default_factory=list)

    @property
    def has_constraints(self) -> bool:
        return bool(
            self.off_requests
            or self.work_requests
            or self.flexible_off
            or self.paid_leave_days
            or self.requested_holiday_days
            or self.preferred_consecutive_off_days
        )


_FULLWIDTH_TRANS = str.maketrans(
    {
        **{chr(ord("０") + i): str(i) for i in range(10)},
        "，": ",",
        "、": ",",
        "．": ".",
        "　": " ",
    }
)


def _normalize_note_text(text: str) -> str:
    return (text or "").translate(_FULLWIDTH_TRANS)


def _safe_day(day, days_in_month: int) -> Optional[int]:
    try:
        day_int = int(day)
    except (TypeError, ValueError):
        return None
    if 1 <= day_int <= days_in_month:
        return day_int
    return None


def _extract_days_from_text(text: str, target_month: int, days_in_month: int) -> list[int]:
    """文中の「4/2」「5月4日」「22日」などから対象月の日付を拾う。"""
    normalized = _normalize_note_text(text)
    days: list[int] = []

    for month_str, day_str in re.findall(r"(\d{1,2})\s*/\s*(\d{1,2})", normalized):
        if int(month_str) == int(target_month):
            day = _safe_day(day_str, days_in_month)
            if day is not None:
                days.append(day)

    for month_str, day_str in re.findall(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", normalized):
        if int(month_str) == int(target_month):
            day = _safe_day(day_str, days_in_month)
            if day is not None:
                days.append(day)

    for day_str in re.findall(r"(?<!月)(\d{1,2})\s*日", normalized):
        day = _safe_day(day_str, days_in_month)
        if day is not None:
            days.append(day)

    return sorted(set(days))


def _strip_total_count_phrases(text: str) -> str:
    """有給2日・出勤10日間など、具体日ではない数を日付抽出から外す。"""
    normalized = _normalize_note_text(text)
    patterns = [
        r"(?:有給|有休)\D{0,6}\d{1,2}\s*日",
        r"\d{1,2}\s*日(?:分)?\s*(?:有給|有休)",
        r"出勤\s*\d{1,2}\s*日(?:間)?",
        r"(?:計|合計)\s*\d{1,2}\s*日(?:間)?\s*(?:お)?休み",
        r"\d{1,2}\s*日間\s*(?:お)?休み",
        r"月の休みは\s*\d{1,2}\s*回",
        r"\d{1,2}\s*日休暇",
    ]
    for pattern in patterns:
        normalized = re.sub(pattern, "", normalized)
    return normalized


def _extract_work_days_from_sentence(
    sentence: str,
    target_month: int,
    days_in_month: int,
) -> list[int]:
    """出勤希望文から具体日を拾う。日付の「日」が省略された書き方にも対応する。"""
    cleaned = _strip_total_count_phrases(sentence)
    days = _extract_days_from_text(cleaned, target_month, days_in_month)
    for candidate_list in re.findall(
        r"(\d{1,2}(?:\s*[,\.・と]\s*\d{1,2})*)\s*(?:は|を|が)",
        cleaned,
    ):
        for day_str in re.findall(r"\d{1,2}", candidate_list):
            day = _safe_day(day_str, days_in_month)
            if day is not None:
                days.append(day)
    return sorted(set(days))


def _extract_number(patterns: list[str], text: str) -> Optional[int]:
    normalized = _normalize_note_text(text)
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            for group in match.groups():
                if group and str(group).isdigit():
                    return int(group)
    return None


def _extract_store_from_text(text: str) -> Optional[Store]:
    """自由記載に含まれる店舗名を Store に変換する。"""
    normalized = _normalize_note_text(text)
    store_keywords = [
        ("赤羽東口", Store.HIGASHIGUCHI),
        ("東口", Store.HIGASHIGUCHI),
        ("大宮西口", Store.NISHIGUCHI),
        ("西口", Store.NISHIGUCHI),
        ("すずらん", Store.SUZURAN),
        ("大宮駅前", Store.OMIYA),
        ("大宮", Store.OMIYA),
        ("赤羽駅前", Store.AKABANE),
        ("赤羽", Store.AKABANE),
    ]
    for keyword, store in store_keywords:
        if keyword in normalized:
            return store
    return None


def _extract_flexible_off_from_sentence(sentence: str, days_in_month: int) -> list[tuple[list[int], int]]:
    """「16,17日のいずれか1日休み」のような候補休を拾う。"""
    normalized = _normalize_note_text(sentence)
    if not any(word in normalized for word in ("いずれか", "どちらか", "どれか")):
        return []
    if not any(word in normalized for word in ("休み", "休日", "休暇")):
        return []

    pattern = (
        r"(?P<candidates>\d{1,2}(?:\s*[,\.・と]\s*\d{1,2})+)"
        r"\s*日?の?(?:いずれか|どちらか|どれか)"
        r".*?(?P<count>\d{1,2})\s*日"
    )
    results: list[tuple[list[int], int]] = []
    for match in re.finditer(pattern, normalized):
        candidates = []
        for day_str in re.findall(r"\d{1,2}", match.group("candidates")):
            day = _safe_day(day_str, days_in_month)
            if day is not None:
                candidates.append(day)
        required = int(match.group("count"))
        if candidates and required > 0:
            results.append((sorted(set(candidates)), required))
    return results


def parse_natural_language_note(
    note: str,
    target_year: int,
    target_month: int,
) -> ParsedNaturalLanguageNote:
    """
    自由記載のよくある表現を、生成エンジンで使える希望に変換する。

    Claude なしでも動く保険として、日付・店舗・有給・候補休の典型表現を拾う。
    """
    result = ParsedNaturalLanguageNote()
    normalized = _normalize_note_text(note)
    if not normalized.strip():
        return result
    days_in_month = monthrange(target_year, target_month)[1]
    sentences = [
        s.strip()
        for s in re.split(r"[。\n]+", normalized)
        if s.strip()
    ]

    paid = _extract_number(
        [
            r"(?:有給|有休)\D{0,6}(\d{1,2})\s*日",
            r"(\d{1,2})\s*日(?:分)?\s*(?:有給|有休)",
        ],
        normalized,
    )
    if paid is not None:
        result.paid_leave_days = paid

    requested_holidays = _extract_number(
        [
            r"(?:計|合計)\s*(\d{1,2})\s*日(?:間)?\s*(?:お)?休み",
            r"(\d{1,2})\s*日間\s*(?:お)?休み",
            r"月の休みは\s*(\d{1,2})\s*回",
            r"(\d{1,2})\s*日休暇",
        ],
        normalized,
    )
    requested_work_days = _extract_number(
        [r"出勤\s*(\d{1,2})\s*日(?:間)?"],
        normalized,
    )
    if requested_work_days is not None:
        requested_holidays = days_in_month - requested_work_days
    if requested_holidays is not None and 0 <= requested_holidays <= days_in_month:
        result.requested_holiday_days = requested_holidays

    consecutive_off = _extract_number([r"(\d{1,2})\s*連休"], normalized)
    if consecutive_off is not None and consecutive_off >= 2:
        result.preferred_consecutive_off_days = consecutive_off

    for sentence in sentences:
        for flex in _extract_flexible_off_from_sentence(sentence, days_in_month):
            result.flexible_off.append(flex)

        if any(word in sentence for word in ("どちらか", "いずれか", "どれか")):
            continue
        if "休み希望はなし" in sentence or "休み希望なし" in sentence:
            continue

        sentence_days = _extract_days_from_text(sentence, target_month, days_in_month)
        if not sentence_days:
            sentence_days = []

        is_total_holiday_sentence = any(
            word in sentence for word in ("日間", "計", "合計", "平均", "有給", "有休", "月の休み")
        )
        if (
            not is_total_holiday_sentence
            and any(word in sentence for word in ("休み希望", "休日希望", "休みたい", "休暇希望"))
        ):
            result.off_requests.extend(sentence_days)

        is_store_request = (
            _extract_store_from_text(sentence) is not None
            and "希望" in sentence
            and "休み" not in sentence
        )
        is_work_request = any(
            word in sentence
            for word in ("出勤希望", "出勤したい", "出たい", "出れます", "出られます", "出勤確定")
        ) or is_store_request
        if is_work_request:
            optional = any(word in sentence for word in ("不要であれば", "必要であれば", "可能なら", "できれば"))
            work_days = _extract_work_days_from_sentence(sentence, target_month, days_in_month)
            if optional:
                result.ignored_optional_work_days.extend(work_days)
            else:
                store = _extract_store_from_text(sentence)
                for day in work_days:
                    result.work_requests.append((day, store))

    result.off_requests = sorted(set(result.off_requests))
    result.ignored_optional_work_days = sorted(set(result.ignored_optional_work_days))
    # 同じ日の出勤希望は重複排除。店舗指定ありを優先する。
    by_day: dict[int, Optional[Store]] = {}
    for day, store in result.work_requests:
        if day not in by_day or by_day[day] is None:
            by_day[day] = store
    result.work_requests = sorted(by_day.items())
    return result


def load_submissions_for_month(
    year: int, month: int,
    expected_employees: Optional[list[str]] = None,
) -> SubmissionData:
    """
    指定月の全提出データを読み込み、generator に渡せる形式に変換する。

    Args:
        year: 対象年
        month: 対象月
        expected_employees: 期待する従業員リスト（未提出者の判定用）

    Returns:
        SubmissionData
    """
    data = SubmissionData(year=year, month=month)
    month_dir = BACKUP_DIR / f"{year:04d}-{month:02d}"
    if not month_dir.exists():
        if expected_employees:
            data.pending_employees = list(expected_employees)
        return data

    # 各従業員の最新提出を取得
    latest: dict[str, dict] = {}
    for f in sorted(month_dir.glob("preferences_*.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                d = json.load(fp)
            author = d.get("author", "")
            if not author or author == "system":
                continue
            saved_at = d.get("saved_at", "")
            if author not in latest or saved_at > latest[author].get("saved_at", ""):
                latest[author] = d
        except Exception:
            continue

    # generator 形式に変換
    for author, d in latest.items():
        # off_requests
        emp_offs_dict = d.get("off_requests", {})
        if isinstance(emp_offs_dict, dict):
            emp_offs = emp_offs_dict.get(author, [])
            if emp_offs:
                data.off_requests[author] = list(emp_offs)

        # work_requests: [{"employee": ..., "day": ..., "store": ...}, ...]
        for wr in d.get("work_requests", []):
            if not isinstance(wr, dict):
                continue
            emp = wr.get("employee")
            day = wr.get("day")
            store = _store_from_name(wr.get("store"))
            if emp and day:
                data.work_requests.append((emp, int(day), store))

        # flexible_off: [{"employee": ..., "candidate_days": [...], "n_required": ...}, ...]
        for fo in d.get("flexible_off", []):
            if isinstance(fo, dict):
                emp = fo.get("employee")
                cand = fo.get("candidate_days", [])
                n = fo.get("n_required", 0)
                if emp and cand:
                    data.flexible_off.append((emp, list(cand), int(n)))
            elif isinstance(fo, list) and len(fo) >= 3:
                # 旧形式（タプルで保存されたもの）
                data.flexible_off.append(tuple(fo))

        # 自由記述
        notes = d.get("natural_language_notes", {})
        note_text = ""
        if isinstance(notes, dict) and notes.get(author):
            note_text = notes[author]
            data.natural_language_notes[author] = note_text

        # 有給日数
        paid = d.get("paid_leave_days", 0)
        if paid:
            data.paid_leave_days[author] = int(paid)

        parsed_note = parse_natural_language_note(note_text, year, month)
        if parsed_note.has_constraints or parsed_note.ignored_optional_work_days:
            data.parsed_note_summaries[author] = {
                "off_requests": list(parsed_note.off_requests),
                "work_requests": [
                    {"day": day, "store": store.name if store else None}
                    for day, store in parsed_note.work_requests
                ],
                "flexible_off": [
                    {"candidate_days": days, "n_required": n}
                    for days, n in parsed_note.flexible_off
                ],
                "paid_leave_days": parsed_note.paid_leave_days,
                "requested_holiday_days": parsed_note.requested_holiday_days,
                "preferred_consecutive_off_days": parsed_note.preferred_consecutive_off_days,
                "ignored_optional_work_days": list(parsed_note.ignored_optional_work_days),
            }

        if parsed_note.off_requests:
            existing_off = set(data.off_requests.get(author, []))
            existing_off.update(parsed_note.off_requests)
            data.off_requests[author] = sorted(existing_off)

        existing_preferred_work_days = {
            (emp, day, store) for emp, day, store in data.preferred_work_requests
        }
        for day, store in parsed_note.work_requests:
            item = (author, day, store)
            if day not in set(data.off_requests.get(author, [])) and item not in existing_preferred_work_days:
                data.preferred_work_requests.append(item)
                existing_preferred_work_days.add(item)

        for candidate_days, n_required in parsed_note.flexible_off:
            data.flexible_off.append((author, candidate_days, n_required))

        if parsed_note.paid_leave_days is not None:
            data.paid_leave_days[author] = max(
                int(data.paid_leave_days.get(author, 0) or 0),
                int(parsed_note.paid_leave_days),
            )

        if parsed_note.requested_holiday_days is not None:
            data.requested_holiday_days[author] = int(parsed_note.requested_holiday_days)

        if parsed_note.preferred_consecutive_off_days is not None:
            data.preferred_consecutive_off.append(
                (author, int(parsed_note.preferred_consecutive_off_days))
            )

        data.submitted_employees.append(author)

    # 未提出者の判定
    if expected_employees:
        submitted_set = set(data.submitted_employees)
        data.pending_employees = [
            e for e in expected_employees if e not in submitted_set
        ]

    return data


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【提出データローダー 動作テスト】\n")

    from .employees import shift_active_employees
    expected = [e.name for e in shift_active_employees() if not e.is_auxiliary]

    for ym in ["2026-04", "2026-05", "2026-06"]:
        year, month = int(ym[:4]), int(ym[5:])
        data = load_submissions_for_month(year, month, expected)
        print(f"=== {year}年{month}月 ===")
        print(f"  提出済み: {len(data.submitted_employees)}名")
        if data.submitted_employees:
            print(f"    {', '.join(data.submitted_employees[:5])}"
                  f"{'...' if len(data.submitted_employees) > 5 else ''}")
        print(f"  未提出: {len(data.pending_employees)}名")
        print(f"  休み希望: {sum(len(v) for v in data.off_requests.values())}件")
        print(f"  出勤希望: {len(data.work_requests)}件")
        print(f"  自由記載の出勤希望: {len(data.preferred_work_requests)}件")
        print(f"  柔軟休み: {len(data.flexible_off)}件")
        print(f"  有給申請者: {len(data.paid_leave_days)}名")
        print()
