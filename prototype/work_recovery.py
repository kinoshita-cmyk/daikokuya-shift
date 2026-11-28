"""短い休みを挟む長い連勤の共通判定。生成・検証・再調整で使用する。"""

from __future__ import annotations

from calendar import monthrange


LONG_WORK_RUN_DAYS = 5
LONG_WORK_AVOIDANCE_PENALTY = 600
LONG_WORK_AVOIDANCE_TITLE = "5連勤回避と大宮の人数の優先順位"
LONG_WORK_AVOIDANCE_DESCRIPTION = (
    "全月・全スタッフ共通で、5連勤をできるだけ減らすことを、"
    "大宮駅前の合計3名体制を維持することより強い生成目標にする。"
    "特定の人に5連勤が複数回集中する場合も回避の対象。"
    "5連勤そのものを絶対禁止にはせず、本人の×休み、最低限の店舗人数・技能、"
    "勤務日数、連勤上限、月別の絶対条件を優先する。"
    "大宮の2名体制は、もともと許可される月・組み合わせだけで許容する。"
    "複数の目標を総合評価するため、5連勤ゼロや最適解を保証するものではない。"
)
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


def long_work_windows(working: dict, days: int):
    """当月に5日目以降を迎える連勤の窓。前月の不明な日は推測しない。"""
    for end in range(1, days + 1):
        window = tuple(range(end - LONG_WORK_RUN_DAYS + 1, end + 1))
        if all(day in working for day in window):
            yield end, window


def add_long_work_indicators(model, working: dict, days: int, prefix: str) -> dict:
    indicators = {}
    for end, window in long_work_windows(working, days):
        indicator = model.NewBoolVar(f"long_work_{prefix}_{end}")
        matches = sum(working[day] for day in window)
        model.Add(matches == LONG_WORK_RUN_DAYS).OnlyEnforceIf(indicator)
        model.Add(matches < LONG_WORK_RUN_DAYS).OnlyEnforceIf(indicator.Not())
        indicators[end] = indicator
    return indicators


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
