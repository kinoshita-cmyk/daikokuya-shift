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
    flexible_off: list[tuple] = field(default_factory=list)
    natural_language_notes: dict[str, str] = field(default_factory=dict)
    paid_leave_days: dict[str, int] = field(default_factory=dict)
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
        if isinstance(notes, dict) and notes.get(author):
            data.natural_language_notes[author] = notes[author]

        # 有給日数
        paid = d.get("paid_leave_days", 0)
        if paid:
            data.paid_leave_days[author] = int(paid)

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
        print(f"  柔軟休み: {len(data.flexible_off)}件")
        print(f"  有給申請者: {len(data.paid_leave_days)}名")
        print()
