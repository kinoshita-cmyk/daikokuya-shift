"""短い休みを挟む長い連勤の共通判定。生成・検証・再調整で使用する。"""

from __future__ import annotations

from calendar import monthrange


LONG_WORK_RUN_DAYS = 5
CLOSE_LONG_WORK_PENALTY = 300
CLOSE_LONG_WORK_CATEGORY = "5連勤の近接"
CLOSE_LONG_WORK_DESCRIPTION = (
    "5日以上の連勤が、休み1日だけを挟んで再び5日以上続く並びを強く回避する。"
    "2日以上の連休を挟む場合や、月初と月末に離れて5連勤がある場合は対象外。"
    "絶対条件を優先し、避けられず残る場合はWARNINGとする。"
    "月をまたぐ並びも、確認できる前月の記録を含めて判定する。"
    "飛び石勤務や月内の5連勤回数とは別の条件。"
)


def work_recovery_applies(employee, year: int, month: int) -> bool:
    from .employees import is_probationary_employee

    role = getattr(employee, "role", None)
    return (
        not getattr(employee, "is_auxiliary", False)
        and getattr(employee, "is_shift_eligible", True)
        and getattr(role, "name", "") not in {"ADVISOR", "REPRESENTATIVE"}
        and not is_probationary_employee(employee, year, month)
    )


def previous_working_map(prev_month, employee: str, year: int, month: int) -> dict[int, bool]:
    """前月最終日を0として返す。不明な日を勤務・休みと推測しない。"""
    prev_year, prev_month_number = (year, month - 1) if month > 1 else (year - 1, 12)
    last_day = monthrange(prev_year, prev_month_number)[1]
    for item in prev_month or []:
        if item.employee != employee:
            continue
        work = getattr(item, "recent_working_days", None)
        off = getattr(item, "recent_off_days", None)
        if work is None and off is None:
            work, off = item.last_working_days, item.last_off_days
        work, off = set(work or []), set(off or [])
        return {
            day - last_day: day in work
            for day in work | off
            if 1 <= day <= last_day and not (day in work and day in off)
        }
    return {}


def _recovery_windows(working: dict, days: int):
    # 後半の5連勤が当月内で成立する窓だけを数える。前月だけの警告は再掲しない。
    size = LONG_WORK_RUN_DAYS
    for rest_day in range(1 - size, days - size + 1):
        window = range(rest_day - size, rest_day + size + 1)
        if all(day in working for day in window):
            yield rest_day, tuple(day for day in window if day != rest_day)


def close_long_work_rest_days(working: dict[int, bool], days: int) -> list[int]:
    """5連勤以上・休み1日・5連勤以上を、間の休み1日につき1件と数える。"""
    return [
        rest for rest, work_days in _recovery_windows(working, days)
        if not working[rest] and all(working[day] for day in work_days)
    ]


def add_close_long_work_indicators(model, working: dict, days: int, prefix: str) -> dict:
    """同じ窓をソルバーの指標にする。指標自体は勤務を禁止しない。"""
    indicators = {}
    for rest, work_days in _recovery_windows(working, days):
        matches = sum(working[day] for day in work_days) + (1 - working[rest])
        size = len(work_days) + 1
        indicator = model.NewBoolVar(f"close_long_work_{prefix}_{rest}")
        model.Add(matches == size).OnlyEnforceIf(indicator)
        model.Add(matches <= size - 1).OnlyEnforceIf(indicator.Not())
        indicators[rest] = indicator
    return indicators
