"""Safe, focused re-adjustments for an already generated monthly shift.

The monthly solver has to balance every rule at once.  This module handles a
smaller second pass after generation.  It only creates proposals; the caller
must show the diff and require an explicit confirmation before applying it.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .employees import get_employee
from .models import MonthlyShift, ShiftAssignment, Skill, Store
from .rules import YamamotoLogic
from .validator import ValidationResult, validate


@dataclass(frozen=True)
class ProposedChange:
    employee: str
    day: int
    before_store: Optional[Store]
    after_store: Optional[Store]
    is_paid_leave: bool = False


@dataclass
class AdjustmentProposal:
    title: str
    summary: str
    changes: list[ProposedChange] = field(default_factory=list)
    before_metrics: dict[str, int] = field(default_factory=dict)
    after_metrics: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


def clone_shift(shift: MonthlyShift) -> MonthlyShift:
    return MonthlyShift(
        year=shift.year,
        month=shift.month,
        assignments=[
            ShiftAssignment(
                employee=a.employee,
                day=a.day,
                store=a.store,
                is_paid_leave=a.is_paid_leave,
            )
            for a in shift.assignments
        ],
        operation_modes=dict(shift.operation_modes),
        comments=list(getattr(shift, "comments", []) or []),
    )


def _replace_assignment(
    shift: MonthlyShift,
    employee: str,
    day: int,
    store: Optional[Store],
    *,
    is_paid_leave: bool = False,
) -> None:
    shift.assignments = [
        a for a in shift.assignments
        if not (a.employee == employee and a.day == day)
    ]
    if store is not None:
        shift.assignments.append(ShiftAssignment(
            employee=employee,
            day=day,
            store=store,
            is_paid_leave=is_paid_leave,
        ))


def _validation_kwargs(context: Optional[dict], max_consec: int) -> dict:
    ctx = context or {}
    return {
        "work_requests": ctx.get("work_requests", []),
        "preferred_work_requests": ctx.get("preferred_work_requests", []),
        "preferred_work_groups": ctx.get("preferred_work_groups", []),
        "off_requests": ctx.get("off_requests", {}),
        "prev_month": ctx.get("prev_month", []),
        "holiday_overrides": ctx.get("holiday_overrides", {}),
        "exact_holiday_days": ctx.get("exact_holiday_days", {}),
        "paid_leave_days": ctx.get("paid_leave_days", {}),
        "employee_max_consecutive_work": ctx.get(
            "employee_max_consecutive_work", {}
        ),
        "employee_max_consecutive_off": ctx.get(
            "employee_max_consecutive_off", {}
        ),
        "monthly_store_count_rules": ctx.get("monthly_store_count_rules", []),
        "required_assignments": ctx.get("required_assignments", []),
        "allow_omiya_short": ctx.get("allow_omiya_short"),
        "max_consec": max_consec,
    }


def validate_with_context(
    shift: MonthlyShift,
    context: Optional[dict] = None,
    max_consec: int = 5,
) -> ValidationResult:
    return validate(shift=shift, **_validation_kwargs(context, max_consec))


def _issue_signature(issue) -> tuple:
    return (
        issue.severity,
        issue.category,
        issue.day,
        issue.employee,
        issue.message,
    )


def _new_protected_issues(
    before: ValidationResult,
    after: ValidationResult,
) -> list:
    """Return newly introduced errors or non-tobishi warnings."""
    before_signatures = {_issue_signature(i) for i in before.issues}
    return [
        issue for issue in after.issues
        if _issue_signature(issue) not in before_signatures
        and (
            issue.severity == "ERROR"
            or (
                issue.severity == "WARNING"
                and issue.category != "飛び石勤務"
            )
        )
    ]


def _working_map(shift: MonthlyShift, employee: str) -> dict[int, bool]:
    days = monthrange(shift.year, shift.month)[1]
    result = {}
    for day in range(1, days + 1):
        assignment = shift.get_assignment(employee, day)
        result[day] = bool(assignment and assignment.store != Store.OFF)
    return result


def tobishi_days(shift: MonthlyShift, employee: str) -> tuple[list[int], list[int]]:
    """Return (isolated work days, isolated off days)."""
    days = monthrange(shift.year, shift.month)[1]
    working = _working_map(shift, employee)
    isolated_work = [
        day for day in range(2, days)
        if not working[day - 1] and working[day] and not working[day + 1]
    ]
    isolated_off = [
        day for day in range(2, days)
        if working[day - 1] and not working[day] and working[day + 1]
    ]
    return isolated_work, isolated_off


def _eco_core_names(shift: MonthlyShift) -> list[str]:
    names = []
    for name in sorted({a.employee for a in shift.assignments}):
        try:
            employee = get_employee(name)
        except KeyError:
            continue
        if getattr(employee, "is_eco_core", False):
            names.append(name)
    return names


def _tobishi_metrics(shift: MonthlyShift, names: Iterable[str]) -> dict[str, int]:
    isolated_work = 0
    isolated_off = 0
    for name in names:
        work_days, off_days = tobishi_days(shift, name)
        isolated_work += len(work_days)
        isolated_off += len(off_days)
    return {
        "休みに挟まれた単独出勤": isolated_work,
        "出勤に挟まれた単独休日": isolated_off,
    }


def _tobishi_score(metrics: dict[str, int]) -> int:
    # A lone work day is the stronger concern; a lone off day is secondary.
    return (
        metrics["休みに挟まれた単独出勤"] * 10
        + metrics["出勤に挟まれた単独休日"] * 2
    )


def _metrics_from_working(working: dict[int, bool]) -> dict[str, int]:
    days = len(working)
    isolated_work = sum(
        1 for day in range(2, days)
        if not working[day - 1] and working[day] and not working[day + 1]
    )
    isolated_off = sum(
        1 for day in range(2, days)
        if working[day - 1] and not working[day] and working[day + 1]
    )
    return {
        "休みに挟まれた単独出勤": isolated_work,
        "出勤に挟まれた単独休日": isolated_off,
    }


def _diff(original: MonthlyShift, adjusted: MonthlyShift) -> list[ProposedChange]:
    keys = {
        (a.employee, a.day) for a in original.assignments
    } | {
        (a.employee, a.day) for a in adjusted.assignments
    }
    changes = []
    for employee, day in sorted(keys, key=lambda item: (item[1], item[0])):
        before = original.get_assignment(employee, day)
        after = adjusted.get_assignment(employee, day)
        before_store = before.store if before else None
        after_store = after.store if after else None
        before_paid = bool(before and before.is_paid_leave)
        after_paid = bool(after and after.is_paid_leave)
        if before_store == after_store and before_paid == after_paid:
            continue
        changes.append(ProposedChange(
            employee=employee,
            day=day,
            before_store=before_store,
            after_store=after_store,
            is_paid_leave=after_paid,
        ))
    return changes


def propose_tobishi_reduction(
    shift: MonthlyShift,
    *,
    validation_context: Optional[dict] = None,
    max_consec: int = 5,
    employee_names: Optional[Iterable[str]] = None,
    max_swaps: int = 4,
) -> AdjustmentProposal:
    """Propose reciprocal day swaps that reduce eco-core tobishi patterns.

    Each accepted move swaps work/off between two employees on two days.  This
    keeps every day's store headcount and both employees' monthly work counts
    unchanged, while the validator prevents new hard violations.
    """
    original = clone_shift(shift)
    working_shift = clone_shift(shift)
    all_core = _eco_core_names(working_shift)
    requested = [name for name in (employee_names or all_core) if name in all_core]
    if not requested:
        return AdjustmentProposal(
            title="飛び石勤務の再調整",
            summary="対象となるエコ主力が見つかりませんでした。",
        )

    before_metrics = _tobishi_metrics(working_shift, requested)
    current_global = _tobishi_metrics(working_shift, all_core)
    validation = validate_with_context(working_shift, validation_context, max_consec)
    off_requests = (validation_context or {}).get("off_requests", {}) or {}
    employee_names_in_shift = sorted({a.employee for a in working_shift.assignments})
    accepted_swaps = 0
    candidate_history: list[MonthlyShift] = []

    while accepted_swaps < min(4, max(0, int(max_swaps))):
        current_target = _tobishi_metrics(working_shift, requested)
        current_target_score = _tobishi_score(current_target)
        current_global_score = _tobishi_score(current_global)
        working_by_employee = {
            name: _working_map(working_shift, name)
            for name in employee_names_in_shift
        }
        score_by_employee = {
            name: _tobishi_score(_metrics_from_working(working))
            for name, working in working_by_employee.items()
        }
        ranked_candidates = []

        for target in requested:
            target_working = working_by_employee[target]
            isolated_work_days, _ = tobishi_days(working_shift, target)
            for work_day in isolated_work_days:
                target_work = working_shift.get_assignment(target, work_day)
                if not target_work or target_work.is_paid_leave:
                    continue
                for off_day, target_is_working in target_working.items():
                    if target_is_working or off_day == work_day:
                        continue
                    target_off = working_shift.get_assignment(target, off_day)
                    if target_off and target_off.is_paid_leave:
                        continue
                    if off_day in {int(d) for d in off_requests.get(target, [])}:
                        continue
                    for other in employee_names_in_shift:
                        if other == target or other == YamamotoLogic.EMPLOYEE_NAME:
                            continue
                        other_at_work_day = working_shift.get_assignment(other, work_day)
                        other_at_off_day = working_shift.get_assignment(other, off_day)
                        if not other_at_work_day or not other_at_off_day:
                            continue
                        if other_at_work_day.store != Store.OFF:
                            continue
                        if other_at_off_day.store == Store.OFF:
                            continue
                        if other_at_work_day.is_paid_leave or other_at_off_day.is_paid_leave:
                            continue
                        if work_day in {int(d) for d in off_requests.get(other, [])}:
                            continue

                        target_after = dict(target_working)
                        target_after[work_day] = False
                        target_after[off_day] = True
                        target_after_score = _tobishi_score(
                            _metrics_from_working(target_after)
                        )
                        target_score = (
                            current_target_score
                            - score_by_employee[target]
                            + target_after_score
                        )

                        other_after_score = score_by_employee.get(other, 0)
                        if other in all_core or other in requested:
                            other_after = dict(working_by_employee[other])
                            other_after[work_day] = True
                            other_after[off_day] = False
                            other_after_score = _tobishi_score(
                                _metrics_from_working(other_after)
                            )
                        if other in requested:
                            target_score += (
                                other_after_score - score_by_employee[other]
                            )
                        global_score = current_global_score
                        global_score += target_after_score - score_by_employee[target]
                        if other in all_core:
                            global_score += (
                                other_after_score - score_by_employee[other]
                            )
                        if target_score >= current_target_score:
                            continue
                        if global_score >= current_global_score:
                            continue
                        ranked_candidates.append((
                            target_score,
                            global_score,
                            work_day,
                            off_day,
                            target,
                            other,
                            target_work.store,
                            other_at_off_day.store,
                        ))

        if not ranked_candidates:
            break

        ranked_candidates.sort(key=lambda item: item[:6])
        candidate_info = ranked_candidates[0]
        _, _, work_day, off_day, target, other, work_store, other_store = (
            candidate_info
        )
        candidate = clone_shift(working_shift)
        _replace_assignment(candidate, target, work_day, Store.OFF)
        _replace_assignment(candidate, other, work_day, work_store)
        _replace_assignment(candidate, target, off_day, other_store)
        _replace_assignment(candidate, other, off_day, Store.OFF)
        working_shift = candidate
        current_global = _tobishi_metrics(working_shift, all_core)
        candidate_history.append(clone_shift(working_shift))
        accepted_swaps += 1

    # Full monthly validation is expensive, so validate complete proposals
    # from best to smallest instead of validating every pattern-only candidate.
    safe_shift = None
    safe_swap_count = 0
    for index in range(len(candidate_history) - 1, -1, -1):
        trial = candidate_history[index]
        trial_validation = validate_with_context(
            trial, validation_context, max_consec
        )
        if trial_validation.error_count > validation.error_count:
            continue
        if _new_protected_issues(validation, trial_validation):
            continue
        safe_shift = trial
        safe_swap_count = index + 1
        break
    if safe_shift is not None:
        working_shift = safe_shift
        accepted_swaps = safe_swap_count
    else:
        working_shift = clone_shift(original)
        accepted_swaps = 0

    after_metrics = _tobishi_metrics(working_shift, requested)
    changes = _diff(original, working_shift)
    if not changes:
        return AdjustmentProposal(
            title="飛び石勤務の再調整",
            summary=(
                "本人の×休み、店舗人数、月間勤務日数、ほかの絶対条件を守ったまま"
                "改善できる安全な入れ替えは見つかりませんでした。"
            ),
            before_metrics=before_metrics,
            after_metrics=after_metrics,
            notes=["対象: " + "、".join(requested)],
        )

    return AdjustmentProposal(
        title="飛び石勤務の再調整",
        summary=(
            f"{accepted_swaps}組の相互入れ替えを提案します。"
            "各日の店舗人数と各人の月間勤務日数は変わりません。"
        ),
        changes=changes,
        before_metrics=before_metrics,
        after_metrics=after_metrics,
        notes=[
            "対象: " + "、".join(requested),
            "新しいエラーや飛び石以外の警告を増やさない候補だけを採用しました。",
        ],
    )


def propose_yamamoto_cleanup(shift: MonthlyShift) -> AdjustmentProposal:
    """Remove Yamamoto assignments that are no longer required at Akabane."""
    redundant_days = []
    for assignment in shift.assignments:
        if (
            assignment.employee != YamamotoLogic.EMPLOYEE_NAME
            or assignment.store != Store.AKABANE
        ):
            continue
        regular_workers = [
            a for a in shift.get_day_assignments(assignment.day)
            if a.employee != YamamotoLogic.EMPLOYEE_NAME
            and a.store == Store.AKABANE
        ]
        eco_count = 0
        ticket_count = 0
        for worker in regular_workers:
            try:
                skill = get_employee(worker.employee).skill
            except KeyError:
                continue
            if skill in (Skill.ECO, Skill.ECO_SUPPORT):
                eco_count += 1
            elif skill == Skill.TICKET:
                ticket_count += 1
        if not YamamotoLogic.should_deploy(eco_count, ticket_count, False):
            redundant_days.append(assignment.day)

    changes = []
    for day in sorted(redundant_days):
        changes.append(ProposedChange(
            employee=YamamotoLogic.EMPLOYEE_NAME,
            day=day,
            before_store=Store.AKABANE,
            after_store=None,
        ))

    if not changes:
        return AdjustmentProposal(
            title="山本の出勤日整理",
            summary=(
                "現在の山本の出勤日は、いずれも赤羽の通常スタッフだけでは"
                "チケット対応人数が足りない日でした。自動で外せる日はありません。"
            ),
            before_metrics={"山本の出勤日": sum(
                1 for a in shift.assignments
                if a.employee == YamamotoLogic.EMPLOYEE_NAME
                and a.store != Store.OFF
            )},
        )

    before_count = sum(
        1 for a in shift.assignments
        if a.employee == YamamotoLogic.EMPLOYEE_NAME and a.store != Store.OFF
    )
    return AdjustmentProposal(
        title="山本の出勤日整理",
        summary=(
            "赤羽の通常スタッフで必要人数を満たしており、現在は不要になった"
            f"{len(changes)}日分を空欄へ戻す提案です。"
        ),
        changes=changes,
        before_metrics={"山本の出勤日": before_count},
        after_metrics={"山本の出勤日": before_count - len(changes)},
        notes=["本人の×休みは変更しません。"],
    )
