"""シフト表の店舗別「人員少」表示に使う共通判定。"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from .employees import get_employee, is_probationary_employee
from .models import MonthlyShift, OperationMode, Skill, Store
from .rules import get_capacity


@dataclass(frozen=True)
class EffectiveStoreStaffing:
    """通常人数として数える、店舗別の実勤務人数。"""

    eco_count: int
    eco_support_count: int
    ticket_count: int
    total_count: int
    yamamoto_present: bool


def effective_store_staffing(
    shift: MonthlyShift,
    day: int,
    store: Store,
) -> EffectiveStoreStaffing:
    """補助要員・試用期間者を除いた人数と技能構成を返す。"""
    eco_count = 0
    eco_support_count = 0
    ticket_count = 0
    yamamoto_present = False
    seen_employees: set[str] = set()

    for assignment in shift.get_day_assignments(day):
        # 表示側の get_assignment() と同じく、同一人物・同一日は最初の1件だけを
        # 有効にする。復元・手動編集由来の重複で人数だけ水増しされるのを防ぐ。
        if assignment.employee in seen_employees:
            continue
        seen_employees.add(assignment.employee)
        if assignment.store != store:
            continue
        if assignment.employee == "山本":
            yamamoto_present = True
        try:
            employee = get_employee(assignment.employee)
        except KeyError:
            continue
        if employee.is_auxiliary or is_probationary_employee(
            employee,
            shift.year,
            shift.month,
            day,
        ):
            continue
        if employee.skill == Skill.ECO:
            eco_count += 1
        elif employee.skill == Skill.ECO_SUPPORT:
            eco_support_count += 1
        elif employee.skill == Skill.TICKET:
            ticket_count += 1

    return EffectiveStoreStaffing(
        eco_count=eco_count,
        eco_support_count=eco_support_count,
        ticket_count=ticket_count,
        total_count=eco_count + eco_support_count + ticket_count,
        yamamoto_present=yamamoto_present,
    )


def detect_short_staff_by_store(
    shift: MonthlyShift,
) -> dict[int, set[Store]]:
    """通常体制を基準に、日付ごとの人員不足店舗を返す。

    省人員・最小営業モードは2名運営を許容するための例外であり、
    「通常3名に満たない」という画面上の注意まで消すものではない。
    特に大宮駅前は営業モードにかかわらず、合計2名なら人数少として表示する。
    """
    short_by_store: dict[int, set[Store]] = {}
    days_in_month = monthrange(shift.year, shift.month)[1]

    for day in range(1, days_in_month + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        capacity_map = get_capacity(mode)
        weekday = date(shift.year, shift.month, day).weekday()

        for store, store_capacity in capacity_map.items():
            if weekday in store_capacity.closed_dow:
                continue
            staffing = effective_store_staffing(shift, day, store)

            if store == Store.HIGASHIGUCHI:
                if staffing.eco_count < 1:
                    short_by_store.setdefault(day, set()).add(store)
                continue

            if store == Store.AKABANE and mode == OperationMode.NORMAL:
                effective_ticket = (
                    staffing.ticket_count
                    + staffing.eco_support_count
                    + max(0, staffing.eco_count - 1)
                )
                if staffing.yamamoto_present:
                    effective_ticket += 1
                if staffing.eco_count < 1 or effective_ticket < 2:
                    short_by_store.setdefault(day, set()).add(store)
                continue

            if store == Store.OMIYA:
                if staffing.eco_count < 1 or staffing.total_count < 3:
                    short_by_store.setdefault(day, set()).add(store)
                continue

            if store == Store.SUZURAN and mode == OperationMode.NORMAL:
                if staffing.eco_count < 1 or staffing.total_count < 3:
                    short_by_store.setdefault(day, set()).add(store)
                continue

            if (
                staffing.eco_count < store_capacity.eco_min
                or staffing.total_count
                < store_capacity.eco_min + store_capacity.ticket_min
            ):
                short_by_store.setdefault(day, set()).add(store)

    return short_by_store
