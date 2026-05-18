"""
前月末から当月月初への連勤持ち越しデータ作成。
================================================

ロック済みの前月確定シフトを読み取り、月末から連続して勤務している
日数を PreviousMonthCarryover として生成する。
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .backup import ShiftBackup
from .employees import shift_active_employees
from .models import MonthlyShift, PreviousMonthCarryover, Store
from .shift_lock import ShiftLockManager


@dataclass(frozen=True)
class LockedCarryoverResult:
    """前月ロック済みシフトから作成した持ち越し結果。"""

    previous_year: int
    previous_month: int
    carryover: list[PreviousMonthCarryover]
    loaded: bool
    message: str
    snapshot_path: Optional[Path] = None


def previous_year_month(year: int, month: int) -> tuple[int, int]:
    """指定年月の前月を返す。"""
    if int(month) == 1:
        return int(year) - 1, 12
    return int(year), int(month) - 1


def build_previous_month_carryover(
    previous_shift: MonthlyShift,
) -> list[PreviousMonthCarryover]:
    """前月シフトの月末から、従業員ごとの連勤・連休持ち越しを作る。"""
    last_day = monthrange(int(previous_shift.year), int(previous_shift.month))[1]
    employee_names = list(dict.fromkeys(
        [e.name for e in shift_active_employees()]
        + [a.employee for a in previous_shift.assignments]
    ))
    carryover: list[PreviousMonthCarryover] = []
    for name in employee_names:
        last_working_days: list[int] = []
        for day in range(last_day, 0, -1):
            assignment = previous_shift.get_assignment(name, day)
            if assignment is not None and assignment.store != Store.OFF:
                last_working_days.append(day)
            else:
                break

        last_off_days: list[int] = []
        for day in range(last_day, 0, -1):
            assignment = previous_shift.get_assignment(name, day)
            if assignment is None or assignment.store == Store.OFF:
                last_off_days.append(day)
            else:
                break

        if last_working_days or last_off_days:
            carryover.append(PreviousMonthCarryover(
                employee=name,
                last_working_days=sorted(last_working_days),
                last_off_days=sorted(last_off_days),
            ))
    return carryover


def load_locked_previous_month_carryover(
    year: int,
    month: int,
    backup: Optional[ShiftBackup] = None,
    lock_mgr: Optional[ShiftLockManager] = None,
) -> LockedCarryoverResult:
    """ロック済み前月シフトを読み込み、連勤持ち越し情報を返す。"""
    previous_year, previous_month = previous_year_month(int(year), int(month))
    backup = backup or ShiftBackup()
    lock_mgr = lock_mgr or ShiftLockManager()
    lock_info = lock_mgr.get_lock_info(previous_year, previous_month)
    if lock_info is None:
        return LockedCarryoverResult(
            previous_year=previous_year,
            previous_month=previous_month,
            carryover=[],
            loaded=False,
            message=(
                f"{previous_year}年{previous_month}月のロック済み確定シフトがないため、"
                "前月末の連勤持ち越しは未反映です。"
            ),
        )

    snapshot_path = (
        backup.backup_dir
        / f"{previous_year:04d}-{previous_month:02d}"
        / lock_info.snapshot_file
    )
    if not snapshot_path.exists():
        return LockedCarryoverResult(
            previous_year=previous_year,
            previous_month=previous_month,
            carryover=[],
            loaded=False,
            message=(
                f"{previous_year}年{previous_month}月のロック情報はありますが、"
                "確定シフト本体が見つからないため、前月末の連勤持ち越しは未反映です。"
            ),
            snapshot_path=snapshot_path,
        )

    previous_shift = backup.load_shift(snapshot_path)
    carryover = build_previous_month_carryover(previous_shift)
    working_count = sum(1 for item in carryover if item.last_working_days)
    return LockedCarryoverResult(
        previous_year=previous_year,
        previous_month=previous_month,
        carryover=carryover,
        loaded=True,
        message=(
            f"前月末の連勤持ち越しは、ロック済み"
            f"{previous_year}年{previous_month}月シフトから自動反映しました"
            f"（月末連勤あり {working_count}名）。"
        ),
        snapshot_path=snapshot_path,
    )
