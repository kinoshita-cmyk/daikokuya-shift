"""Safe, focused re-adjustments for an already generated monthly shift.

The monthly solver has to balance every rule at once.  This module handles a
smaller second pass after generation.  It only creates proposals; the caller
must show the diff and require an explicit confirmation before applying it.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Iterable, Optional

from ortools.sat.python import cp_model

from .employees import get_employee
from .models import MonthlyShift, Role, ShiftAssignment, Skill, Store
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


@dataclass(frozen=True)
class QualityRegression:
    key: str
    label: str
    before: int
    after: int

    @property
    def increase(self) -> int:
        return self.after - self.before


@dataclass(frozen=True)
class _ReciprocalMove:
    """One work/off exchange that keeps both headcount and workdays fixed."""

    target: str
    other: str
    target_work_day: int
    target_off_day: int


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
        "default_holidays": ctx.get("default_holidays", 8),
        "max_consec": max_consec,
    }


def validate_with_context(
    shift: MonthlyShift,
    context: Optional[dict] = None,
    max_consec: int = 5,
) -> ValidationResult:
    return validate(shift=shift, **_validation_kwargs(context, max_consec))


def _issue_signature(issue) -> tuple:
    # Message text often contains a count or an employee list.  A harmless
    # re-adjustment can change that text while leaving the same underlying
    # issue in place, so identity is based on its structured location.
    return (
        issue.severity,
        issue.category,
        issue.day,
        issue.employee,
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


def tobishi_days(shift: MonthlyShift, employee: str) -> list[int]:
    """Return only OFF-WORK-OFF days, the system's tobishi definition."""
    days = monthrange(shift.year, shift.month)[1]
    working = _working_map(shift, employee)
    return [
        day for day in range(2, days)
        if not working[day - 1] and working[day] and not working[day + 1]
    ]


def classify_tobishi_days(
    shift: MonthlyShift,
    employee: str,
    off_request_days: Optional[Iterable[int]] = None,
) -> dict[str, list[int]]:
    """Split lone work into actionable and employee-requested exceptions.

    A lone work day is treated as an employee-requested exception only when
    both surrounding off days were submitted as absolute off requests.  When
    only one side was requested, the other side may still be safely adjusted.
    """
    requested = set()
    for raw_day in off_request_days or []:
        try:
            requested.add(int(raw_day))
        except (TypeError, ValueError):
            continue
    allowed = []
    actionable = []
    for day in tobishi_days(shift, employee):
        target = allowed if day - 1 in requested and day + 1 in requested else actionable
        target.append(day)
    return {
        "actionable": actionable,
        "requested_exception": allowed,
    }


def readjustment_shift_signature(shift: MonthlyShift) -> str:
    """Return a stable identity for the currently applied shift state."""
    parts = [f"{int(shift.year):04d}-{int(shift.month):02d}"]
    for assignment in sorted(
        shift.assignments,
        key=lambda item: (
            item.employee,
            int(item.day),
            item.store.name,
            bool(item.is_paid_leave),
        ),
    ):
        parts.append(
            f"{assignment.employee}|{int(assignment.day)}|{assignment.store.name}|"
            f"{int(bool(assignment.is_paid_leave))}"
        )
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _longest_consecutive_work(shift: MonthlyShift, employee: str) -> int:
    longest = 0
    current = 0
    days = monthrange(shift.year, shift.month)[1]
    for day in range(1, days + 1):
        assignment = shift.get_assignment(employee, day)
        if assignment is not None and assignment.store != Store.OFF:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def build_readjustment_quality_snapshot(
    shift: MonthlyShift,
    validation: ValidationResult,
    short_staff_by_store: Optional[dict[int, set[Store]]] = None,
    off_requests: Optional[dict[str, Iterable[int]]] = None,
) -> dict[str, dict[str, object]]:
    """Capture manager-facing quality indicators; lower values are better."""
    metrics: dict[str, dict[str, object]] = {}
    assignment_names = sorted({item.employee for item in shift.assignments})

    for employee in assignment_names:
        try:
            profile = get_employee(employee)
        except KeyError:
            profile = None
        if profile is not None and getattr(profile, "is_eco_core", False):
            categories = classify_tobishi_days(
                shift,
                employee,
                (off_requests or {}).get(employee, []),
            )
            metrics[f"tobishi_work:{employee}"] = {
                "label": f"{employee}の改善対象の単独出勤（休・出・休）",
                "value": len(categories["actionable"]),
            }
        if profile is not None and not getattr(profile, "is_auxiliary", False):
            metrics[f"max_consecutive:{employee}"] = {
                "label": f"{employee}の最長連勤",
                "value": _longest_consecutive_work(shift, employee),
            }

    yamamoto_workdays = sum(
        1 for item in shift.assignments
        if item.employee == "山本" and item.store != Store.OFF
    )
    metrics["yamamoto_workdays"] = {
        "label": "山本の出勤日数",
        "value": yamamoto_workdays,
    }

    short_staff_by_store = short_staff_by_store or {}
    metrics["short_staff_store_days"] = {
        "label": "店舗別の人員不足件数",
        "value": sum(len(stores) for stores in short_staff_by_store.values()),
    }
    metrics["short_staff_days"] = {
        "label": "人員不足がある日数",
        "value": len(short_staff_by_store),
    }

    issue_counts: dict[tuple[str, str], int] = {}
    for issue in validation.issues:
        # 飛び石は上の個人別指標で、より具体的に比較する。
        if issue.category == "飛び石勤務":
            continue
        key = (str(issue.severity), str(issue.category))
        issue_counts[key] = issue_counts.get(key, 0) + 1
    for (severity, category), count in sorted(issue_counts.items()):
        severity_label = "エラー" if severity == "ERROR" else "警告"
        metrics[f"issue:{severity}:{category}"] = {
            "label": f"{category}の{severity_label}",
            "value": int(count),
        }
    return metrics


def compare_readjustment_quality(
    baseline: dict[str, dict[str, object]],
    current: dict[str, dict[str, object]],
) -> list[QualityRegression]:
    """Return indicators that became worse than the last applied baseline."""
    regressions = []
    for key, current_item in current.items():
        before_item = baseline.get(key)
        before = int(before_item.get("value", 0)) if before_item else 0
        after = int(current_item.get("value", 0))
        if after <= before:
            continue
        regressions.append(QualityRegression(
            key=key,
            label=str(current_item.get("label", key)),
            before=before,
            after=after,
        ))
    return sorted(
        regressions,
        key=lambda item: (-item.increase, item.label),
    )


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


def _adjustable_employee_names(shift: MonthlyShift) -> list[str]:
    """Return normal staff eligible for work/off reciprocal adjustment."""
    names = []
    for name in sorted({a.employee for a in shift.assignments}):
        try:
            employee = get_employee(name)
        except KeyError:
            continue
        if not getattr(employee, "is_shift_eligible", True):
            continue
        if getattr(employee, "is_auxiliary", False):
            continue
        if getattr(employee, "role", None) == Role.ADVISOR:
            continue
        if getattr(employee, "only_on_request_days", False):
            continue
        names.append(name)
    return names


def _tobishi_metrics(
    shift: MonthlyShift,
    names: Iterable[str],
    validation_context: Optional[dict] = None,
) -> dict[str, int]:
    off_requests = (validation_context or {}).get("off_requests", {}) or {}
    categories = [
        classify_tobishi_days(shift, name, off_requests.get(name, []))
        for name in names
    ]
    return {
        "休みに挟まれた単独出勤": sum(
            len(item["actionable"]) for item in categories
        ),
        "本人希望による許容": sum(
            len(item["requested_exception"]) for item in categories
        ),
    }


def _tobishi_score(metrics: dict[str, int]) -> int:
    return metrics["休みに挟まれた単独出勤"]


def _metrics_from_working(
    working: dict[int, bool],
    off_request_days: Optional[Iterable[int]] = None,
) -> dict[str, int]:
    days = len(working)
    requested = set()
    for raw_day in off_request_days or []:
        try:
            requested.add(int(raw_day))
        except (TypeError, ValueError):
            continue
    isolated_work = [
        day for day in range(2, days)
        if not working[day - 1] and working[day] and not working[day + 1]
    ]
    allowed = [
        day for day in isolated_work
        if day - 1 in requested and day + 1 in requested
    ]
    return {
        "休みに挟まれた単独出勤": len(isolated_work) - len(allowed),
        "本人希望による許容": len(allowed),
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


def apply_adjustment_proposal(
    shift: MonthlyShift,
    proposal: AdjustmentProposal,
) -> MonthlyShift:
    """Apply an already validated proposal to a cloned shift."""
    adjusted = clone_shift(shift)
    for change in proposal.changes:
        _replace_assignment(
            adjusted,
            change.employee,
            change.day,
            change.after_store,
            is_paid_leave=change.is_paid_leave,
        )
    return adjusted


def _shift_state_key(shift: MonthlyShift) -> tuple:
    """Return a stable key for de-duplicating local-search states."""
    return tuple(sorted(
        (
            assignment.employee,
            int(assignment.day),
            assignment.store.value,
            bool(assignment.is_paid_leave),
        )
        for assignment in shift.assignments
    ))


def _off_request_days(context: Optional[dict], employee: str) -> set[int]:
    raw_days = (context or {}).get("off_requests", {}).get(employee, []) or []
    result = set()
    for day in raw_days:
        try:
            result.add(int(day))
        except (TypeError, ValueError):
            continue
    return result


def _assignment_map(shift: MonthlyShift) -> dict[tuple[str, int], ShiftAssignment]:
    return {
        (assignment.employee, int(assignment.day)): assignment
        for assignment in shift.assignments
    }


def _apply_reciprocal_swap(
    shift: MonthlyShift,
    *,
    target: str,
    other: str,
    target_work_day: int,
    target_off_day: int,
) -> MonthlyShift:
    """Swap work/off across two days while preserving daily store counts."""
    candidate = clone_shift(shift)
    target_work = shift.get_assignment(target, target_work_day)
    other_work = shift.get_assignment(other, target_off_day)
    if target_work is None or other_work is None:
        return candidate

    _replace_assignment(candidate, target, target_work_day, Store.OFF)
    _replace_assignment(candidate, other, target_work_day, target_work.store)
    _replace_assignment(candidate, target, target_off_day, other_work.store)
    _replace_assignment(candidate, other, target_off_day, Store.OFF)
    return candidate


def _candidate_swaps(
    shift: MonthlyShift,
    *,
    requested: list[str],
    priority_names: list[str],
    protected_names: list[str],
    validation_context: Optional[dict],
) -> list[tuple]:
    """Enumerate reciprocal swaps that directly reduce OFF-WORK-OFF days.

    A repair may either move the isolated work day itself or move another work
    day into an adjacent off day so the isolated day becomes part of a work
    block.  The latter is important when moving the isolated day would only
    recreate the same pattern elsewhere.
    """
    employee_names = sorted({a.employee for a in shift.assignments})
    assignments = _assignment_map(shift)
    working_by_employee = {
        name: _working_map(shift, name)
        for name in employee_names
    }
    off_request_cache = {
        name: _off_request_days(validation_context, name)
        for name in employee_names
    }
    metrics_by_employee = {
        name: _metrics_from_working(
            working,
            off_request_cache.get(name, set()),
        )
        for name, working in working_by_employee.items()
    }
    score_by_employee = {
        name: _tobishi_score(metrics)
        for name, metrics in metrics_by_employee.items()
    }
    current_target_score = sum(
        score_by_employee.get(name, 0) for name in requested
    )
    current_priority_score = sum(
        score_by_employee.get(name, 0) for name in priority_names
    )
    requested_set = set(requested)
    priority_set = set(priority_names)
    protected_set = set(protected_names)
    ranked = []

    for target in requested:
        target_working = working_by_employee.get(target)
        if not target_working:
            continue
        target_categories = classify_tobishi_days(
            shift,
            target,
            off_request_cache.get(target, set()),
        )
        isolated_work_set = set(target_categories["actionable"])
        adjacent_repair_off_days = {
            adjacent_day
            for isolated_day in isolated_work_set
            for adjacent_day in (isolated_day - 1, isolated_day + 1)
            if (
                1 <= adjacent_day <= len(target_working)
                and not target_working.get(adjacent_day, False)
            )
        }

        for target_work_day, target_is_working in target_working.items():
            if not target_is_working:
                continue
            target_work = assignments.get((target, target_work_day))
            if target_work is None or target_work.is_paid_leave:
                continue

            for target_off_day, is_working in target_working.items():
                if is_working or target_off_day == target_work_day:
                    continue
                # Also allow moving a regular work day next to the isolated
                # day.  The simulated score check below still requires the
                # completed swap to reduce OFF-WORK-OFF, so this does not make
                # isolated days off or two-day work blocks adjustment goals.
                if (
                    target_work_day not in isolated_work_set
                    and target_off_day not in adjacent_repair_off_days
                ):
                    continue
                target_off = assignments.get((target, target_off_day))
                if target_off and target_off.is_paid_leave:
                    continue
                if target_off_day in off_request_cache.get(target, set()):
                    continue

                target_after = dict(target_working)
                target_after[target_work_day] = False
                target_after[target_off_day] = True
                target_after_metrics = _metrics_from_working(
                    target_after,
                    off_request_cache.get(target, set()),
                )
                target_after_score = _tobishi_score(target_after_metrics)
                if target_after_score >= score_by_employee.get(target, 0):
                    continue
                if (
                    target_after_metrics["本人希望による許容"]
                    > metrics_by_employee[target]["本人希望による許容"]
                ):
                    continue

                for other in protected_names:
                    if other in {target, YamamotoLogic.EMPLOYEE_NAME}:
                        continue
                    other_at_target_work = assignments.get((other, target_work_day))
                    other_at_target_off = assignments.get((other, target_off_day))
                    if other_at_target_work is None or other_at_target_off is None:
                        continue
                    if other_at_target_work.store != Store.OFF:
                        continue
                    if other_at_target_off.store == Store.OFF:
                        continue
                    if (
                        other_at_target_work.is_paid_leave
                        or other_at_target_off.is_paid_leave
                    ):
                        continue
                    if target_work_day in off_request_cache.get(other, set()):
                        continue

                    other_after_score = score_by_employee.get(other, 0)
                    if other in protected_set:
                        other_after = dict(working_by_employee[other])
                        other_after[target_work_day] = True
                        other_after[target_off_day] = False
                        other_after_metrics = _metrics_from_working(
                            other_after,
                            off_request_cache.get(other, set()),
                        )
                        other_after_score = _tobishi_score(other_after_metrics)
                        if other_after_score > score_by_employee.get(other, 0):
                            continue
                        if (
                            other_after_metrics["本人希望による許容"]
                            > metrics_by_employee[other]["本人希望による許容"]
                        ):
                            continue

                    target_score = (
                        current_target_score
                        - score_by_employee.get(target, 0)
                        + target_after_score
                    )
                    if other in requested_set:
                        target_score += (
                            other_after_score - score_by_employee.get(other, 0)
                        )
                    if target_score >= current_target_score:
                        continue

                    priority_score = current_priority_score
                    if target in priority_set:
                        priority_score += (
                            target_after_score - score_by_employee.get(target, 0)
                        )
                    if other in priority_set:
                        priority_score += (
                            other_after_score - score_by_employee.get(other, 0)
                        )
                    if priority_score > current_priority_score:
                        continue

                    ranked.append((
                        priority_score,
                        target_score,
                        target_work_day,
                        target_off_day,
                        target,
                        other,
                    ))

    ranked.sort(key=lambda item: item)
    return ranked


def _propose_tobishi_beam(
    shift: MonthlyShift,
    *,
    validation_context: Optional[dict] = None,
    max_consec: int = 5,
    employee_names: Optional[Iterable[str]] = None,
    max_swaps: int = 4,
) -> AdjustmentProposal:
    """Search reciprocal swaps that reduce OFF-WORK-OFF patterns."""
    original = clone_shift(shift)
    working_shift = clone_shift(shift)
    all_core = _eco_core_names(working_shift)
    all_adjustable = _adjustable_employee_names(working_shift)
    explicit_names = list(employee_names or [])
    requested = [
        name for name in (explicit_names or all_adjustable)
        if name in all_adjustable
    ]
    priority_names = requested if explicit_names else [
        name for name in all_core if name in requested
    ]
    if not requested:
        return AdjustmentProposal(
            title="飛び石勤務の再調整",
            summary="再調整対象となる通常スタッフが見つかりませんでした。",
        )

    before_metrics = _tobishi_metrics(
        working_shift, requested, validation_context
    )
    before_global = _tobishi_metrics(
        working_shift, priority_names, validation_context
    )
    before_target_score = _tobishi_score(before_metrics)
    before_global_score = _tobishi_score(before_global)
    validation = validate_with_context(working_shift, validation_context, max_consec)

    # A small beam is enough for a monthly roster while still exploring paths
    # beyond the first greedy swap.  Each state has already passed validation.
    max_depth = min(6, max(0, int(max_swaps)))
    beam_width = 18
    candidates_per_state = 70
    beam: list[tuple[MonthlyShift, int]] = [(clone_shift(original), 0)]
    seen = {_shift_state_key(original)}
    safe_improvements: list[tuple[tuple, MonthlyShift, int]] = []
    validated_state_count = 0

    for _depth in range(1, max_depth + 1):
        raw_candidates: list[tuple[tuple, MonthlyShift, int]] = []
        for state_shift, swap_count in beam:
            swap_candidates = _candidate_swaps(
                state_shift,
                requested=requested,
                priority_names=priority_names,
                protected_names=all_adjustable,
                validation_context=validation_context,
            )
            for swap in swap_candidates[:candidates_per_state]:
                priority_score, target_score, work_day, off_day, target, other = swap
                candidate = _apply_reciprocal_swap(
                    state_shift,
                    target=target,
                    other=other,
                    target_work_day=work_day,
                    target_off_day=off_day,
                )
                state_key = _shift_state_key(candidate)
                if state_key in seen:
                    continue
                seen.add(state_key)
                change_count = len(_diff(original, candidate))
                rank = (priority_score, target_score, change_count, target, other)
                raw_candidates.append((rank, candidate, swap_count + 1))

        if not raw_candidates:
            break

        raw_candidates.sort(key=lambda item: item[0])
        next_beam: list[tuple[MonthlyShift, int]] = []
        for rank, candidate, swap_count in raw_candidates[:beam_width * 5]:
            trial_validation = validate_with_context(
                candidate, validation_context, max_consec
            )
            validated_state_count += 1
            if trial_validation.error_count > validation.error_count:
                continue
            if _new_protected_issues(validation, trial_validation):
                continue

            next_beam.append((candidate, swap_count))
            priority_score, target_score, change_count = rank[:3]
            if (
                target_score < before_target_score
                and priority_score <= before_global_score
            ):
                safe_improvements.append((
                    (priority_score, target_score, change_count, swap_count),
                    clone_shift(candidate),
                    swap_count,
                ))
            if len(next_beam) >= beam_width:
                break
        beam = next_beam
        if not beam:
            break

    safe_improvements.sort(key=lambda item: item[0])
    if safe_improvements:
        _, working_shift, accepted_swaps = safe_improvements[0]
    else:
        working_shift = clone_shift(original)
        accepted_swaps = 0

    after_metrics = _tobishi_metrics(
        working_shift, requested, validation_context
    )
    changes = _diff(original, working_shift)
    if not changes:
        return AdjustmentProposal(
            title="飛び石勤務の再調整",
            summary=(
                "本人の×休み、店舗人数、月間勤務日数、ほかの絶対条件を守ったまま"
                "改善できる候補を、このシフトの広域再探索では見つけられませんでした。"
            ),
            before_metrics=before_metrics,
            after_metrics=after_metrics,
            notes=[
                "対象: " + "、".join(requested),
                (
                    "休みに挟まれた単独出勤と、複数の相互入れ替えを比較しました"
                    f"（安全性を確認した候補 {validated_state_count}件）。"
                ),
            ],
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
            (
                f"安全性を確認した候補 {validated_state_count}件のうち、"
                f"条件を満たす改善案 {len(safe_improvements)}件を比較しました。"
            ),
            "本人の×休み、新しいエラー、飛び石以外の新しい警告を増やさない候補だけを採用しました。",
        ],
    )


def _move_changes(
    shift: MonthlyShift,
    move: _ReciprocalMove,
) -> dict[tuple[str, int], Store]:
    target_work = shift.get_assignment(move.target, move.target_work_day)
    other_work = shift.get_assignment(move.other, move.target_off_day)
    if target_work is None or other_work is None:
        return {}
    return {
        (move.target, move.target_work_day): Store.OFF,
        (move.other, move.target_work_day): target_work.store,
        (move.target, move.target_off_day): other_work.store,
        (move.other, move.target_off_day): Store.OFF,
    }


def _enumerate_reciprocal_moves(
    shift: MonthlyShift,
    *,
    requested: list[str],
    priority_names: list[str],
    protected_names: list[str],
    validation_context: Optional[dict],
) -> list[_ReciprocalMove]:
    """Return unique direct-improvement moves for the exact repair model."""
    moves: list[_ReciprocalMove] = []
    seen_changes: set[tuple] = set()
    off_request_cache = {
        employee: _off_request_days(validation_context, employee)
        for employee in requested
    }
    before_isolated_work = {
        employee: len(classify_tobishi_days(
            shift,
            employee,
            off_request_cache.get(employee, set()),
        )["actionable"])
        for employee in requested
    }
    prioritize_lone_work = any(before_isolated_work.values())
    for swap in _candidate_swaps(
        shift,
        requested=requested,
        priority_names=priority_names,
        protected_names=protected_names,
        validation_context=validation_context,
    ):
        _, _, work_day, off_day, target, other = swap
        move = _ReciprocalMove(
            target=target,
            other=other,
            target_work_day=work_day,
            target_off_day=off_day,
        )
        if prioritize_lone_work:
            target_working = _working_map(shift, target)
            target_working[work_day] = False
            target_working[off_day] = True
            after_isolated_work = _metrics_from_working(
                target_working,
                off_request_cache.get(target, set()),
            )[
                "休みに挟まれた単独出勤"
            ]
            if after_isolated_work >= before_isolated_work.get(target, 0):
                continue
        changes = _move_changes(shift, move)
        if len(changes) != 4:
            continue
        change_key = tuple(sorted(
            (employee, day, store.value)
            for (employee, day), store in changes.items()
        ))
        if change_key in seen_changes:
            continue
        seen_changes.add(change_key)
        moves.append(move)
    return moves


def _apply_move_set(
    shift: MonthlyShift,
    moves: list[_ReciprocalMove],
) -> MonthlyShift:
    candidate = clone_shift(shift)
    for move in moves:
        for (employee, day), store in _move_changes(shift, move).items():
            _replace_assignment(candidate, employee, day, store)
    return candidate


def _individually_safe_move_pool(
    shift: MonthlyShift,
    *,
    moves: list[_ReciprocalMove],
    validation_context: Optional[dict],
    max_consec: int,
    baseline_validation: ValidationResult,
    max_per_target_pattern: int = 1,
) -> tuple[list[_ReciprocalMove], int]:
    """Keep a small set of safe substitutes for each target day pattern.

    The full validator contains operational rules that would be cumbersome to
    duplicate inside CP-SAT.  Checking each one-swap building block first
    removes most impossible combinations while retaining alternative partners.
    """
    safe_moves: list[_ReciprocalMove] = []
    safe_count_by_pattern: dict[tuple, int] = {}
    validated = 0
    for move in moves:
        pattern = (
            move.target,
            move.target_work_day,
            move.target_off_day,
        )
        if safe_count_by_pattern.get(pattern, 0) >= max_per_target_pattern:
            continue
        candidate = _apply_move_set(shift, [move])
        result = validate_with_context(candidate, validation_context, max_consec)
        validated += 1
        if result.error_count > baseline_validation.error_count:
            continue
        if _new_protected_issues(baseline_validation, result):
            continue
        safe_moves.append(move)
        safe_count_by_pattern[pattern] = safe_count_by_pattern.get(pattern, 0) + 1
    return safe_moves, validated


def _isolated_metric_vars(
    model: cp_model.CpModel,
    working: dict[tuple[str, int], cp_model.IntVar],
    employee: str,
    days: int,
    include_days: Optional[set[int]] = None,
) -> list[cp_model.IntVar]:
    isolated_work = []
    for day in range(2, days):
        if include_days is not None and day not in include_days:
            continue
        previous = working[(employee, day - 1)]
        current = working[(employee, day)]
        following = working[(employee, day + 1)]

        lone_work = model.NewBoolVar(f"iw_{employee}_{day}")
        model.Add(lone_work <= current)
        model.Add(lone_work + previous <= 1)
        model.Add(lone_work + following <= 1)
        model.Add(lone_work >= current - previous - following)
        isolated_work.append(lone_work)
    return isolated_work


def _solve_tobishi_move_set(
    shift: MonthlyShift,
    *,
    moves: list[_ReciprocalMove],
    requested: list[str],
    priority_names: list[str],
    protected_names: list[str],
    strategy: str,
    max_swaps: int,
    validation_context: Optional[dict],
    max_consec: int,
    baseline_validation: ValidationResult,
) -> tuple[Optional[MonthlyShift], int, int]:
    """Solve all compatible swaps together, then reject unsafe full solutions."""
    if not moves or max_swaps <= 0:
        return None, 0, 0

    model = cp_model.CpModel()
    selected = [model.NewBoolVar(f"move_{index}") for index in range(len(moves))]
    model.Add(sum(selected) >= 1)
    model.Add(sum(selected) <= max_swaps)

    move_changes = [_move_changes(shift, move) for move in moves]
    cell_moves: dict[tuple[str, int], list[int]] = {}
    for index, changes in enumerate(move_changes):
        for cell in changes:
            cell_moves.setdefault(cell, []).append(index)
    for indices in cell_moves.values():
        if len(indices) > 1:
            model.Add(sum(selected[index] for index in indices) <= 1)

    days = monthrange(shift.year, shift.month)[1]
    metric_names = sorted(set(protected_names) | set(requested))
    model_names = sorted(
        set(metric_names)
        | {move.target for move in moves}
        | {move.other for move in moves}
    )
    working: dict[tuple[str, int], cp_model.IntVar] = {}
    initial_working: dict[tuple[str, int], int] = {}
    for employee in model_names:
        for day in range(1, days + 1):
            initial_assignment = shift.get_assignment(employee, day)
            initial = int(bool(
                initial_assignment and initial_assignment.store != Store.OFF
            ))
            initial_working[(employee, day)] = initial
            work_var = model.NewBoolVar(f"work_{employee}_{day}")
            deltas = []
            for index in cell_moves.get((employee, day), []):
                after = move_changes[index][(employee, day)]
                after_work = int(after != Store.OFF)
                delta = after_work - initial
                if delta:
                    deltas.append(delta * selected[index])
            model.Add(work_var == initial + sum(deltas))
            working[(employee, day)] = work_var

    # Do not create a new over-limit work or off streak.  Existing exceptional
    # windows are left untouched and the full validator still checks the final
    # result, including previous-month carryover and monthly exceptions.
    ctx = validation_context or {}
    employee_work_limits = ctx.get("employee_max_consecutive_work", {}) or {}
    employee_off_limits = ctx.get("employee_max_consecutive_off", {}) or {}
    off_requests = ctx.get("off_requests", {}) or {}
    for employee in requested:
        work_limit = min(
            int(max_consec),
            int(employee_work_limits.get(employee, max_consec)),
        )
        if work_limit > 0:
            for start in range(1, days - work_limit + 1):
                window = list(range(start, start + work_limit + 1))
                if all(initial_working[(employee, day)] for day in window):
                    continue
                model.Add(
                    sum(working[(employee, day)] for day in window)
                    <= work_limit
                )

        off_limit = min(2, int(employee_off_limits.get(employee, 2)))
        requested_off = _off_request_days(validation_context, employee)
        if off_limit > 0:
            for start in range(1, days - off_limit + 1):
                window = list(range(start, start + off_limit + 1))
                original_is_off = all(
                    not initial_working[(employee, day)] for day in window
                )
                submitted_exception = all(day in requested_off for day in window)
                if original_is_off or submitted_exception:
                    continue
                model.Add(
                    sum(working[(employee, day)] for day in window) >= 1
                )

    isolated_by_employee: dict[str, list] = {}
    requested_exception_by_employee: dict[str, list] = {}
    for employee in metric_names:
        requested_days = _off_request_days(validation_context, employee)
        requested_exception_days = {
            day for day in range(2, days)
            if day - 1 in requested_days and day + 1 in requested_days
        }
        actionable_days = set(range(2, days)) - requested_exception_days
        isolated_by_employee[employee] = _isolated_metric_vars(
            model, working, employee, days, actionable_days
        )
        requested_exception_by_employee[employee] = _isolated_metric_vars(
            model, working, employee, days, requested_exception_days
        )

    target_iw = sum(
        variable
        for employee in requested
        for variable in isolated_by_employee[employee]
    )
    priority_iw = sum(
        variable
        for employee in priority_names
        for variable in isolated_by_employee[employee]
    )
    protected_iw = sum(
        variable
        for employee in protected_names
        for variable in isolated_by_employee[employee]
    )
    protected_requested_iw = sum(
        variable
        for employee in protected_names
        for variable in requested_exception_by_employee[employee]
    )

    # A repair must not create more lone work days for any normal employee,
    # including the reciprocal swap partner.
    for employee in protected_names:
        categories = classify_tobishi_days(
            shift,
            employee,
            _off_request_days(validation_context, employee),
        )
        before_count = len(categories["actionable"])
        before_requested_count = len(categories["requested_exception"])
        model.Add(sum(isolated_by_employee[employee]) <= before_count)
        model.Add(
            sum(requested_exception_by_employee[employee])
            <= before_requested_count
        )

    improvement_flags = []
    before_target_count = 0
    for employee in requested:
        before_work = classify_tobishi_days(
            shift,
            employee,
            _off_request_days(validation_context, employee),
        )["actionable"]
        before_count = len(before_work)
        before_target_count += before_count
        employee_iw = sum(isolated_by_employee[employee])
        model.Add(employee_iw <= before_count)
        if before_count > 0:
            improved = model.NewBoolVar(f"improved_{employee}")
            model.Add(employee_iw <= before_count - 1).OnlyEnforceIf(improved)
            model.Add(employee_iw == before_count).OnlyEnforceIf(improved.Not())
            improvement_flags.append(improved)

    model.Add(target_iw <= before_target_count - 1)
    if improvement_flags:
        model.Add(sum(improvement_flags) >= 1)
    not_improved = (
        len(improvement_flags) - sum(improvement_flags)
        if improvement_flags else 0
    )
    move_count = sum(selected)

    if strategy == "minimal":
        objective = (
            priority_iw * 1_000_000_000
            + target_iw * 10_000_000
            + move_count * 100_000
            + not_improved * 1_000
            + protected_iw * 10
            + protected_requested_iw
        )
    elif strategy == "balanced":
        objective = (
            priority_iw * 1_000_000_000
            + target_iw * 10_000_000
            + not_improved * 100_000
            + move_count * 10_000
            + protected_iw * 10
            + protected_requested_iw
        )
    else:
        objective = (
            priority_iw * 1_000_000_000
            + target_iw * 10_000_000
            + not_improved * 1_000_000
            # Once lone work days are minimized, prefer the smallest repair.
            + move_count * 100_000
            + protected_iw * 10
            + protected_requested_iw
        )
    model.Minimize(objective)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 8.0
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42
    validated = 0
    for _attempt in range(200):
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, validated, len(moves)
        chosen_indices = [
            index for index, variable in enumerate(selected)
            if solver.Value(variable)
        ]
        chosen_moves = [moves[index] for index in chosen_indices]
        candidate = _apply_move_set(shift, chosen_moves)
        candidate_validation = validate_with_context(
            candidate, validation_context, max_consec
        )
        validated += 1
        if (
            candidate_validation.error_count <= baseline_validation.error_count
            and not _new_protected_issues(
                baseline_validation, candidate_validation
            )
        ):
            return candidate, validated, len(moves)

        # Exclude this exact set and continue with the next-best full solution.
        model.Add(sum(selected[index] for index in chosen_indices) <= len(chosen_indices) - 1)

    return None, validated, len(moves)


def _proposal_for_tobishi_solution(
    original: MonthlyShift,
    adjusted: MonthlyShift,
    *,
    requested: list[str],
    label: str,
    strategy: str,
    validated_count: int,
    move_pool_size: int,
    baseline_validation: ValidationResult,
    validation_context: Optional[dict],
    max_consec: int,
) -> AdjustmentProposal:
    before_metrics = _tobishi_metrics(
        original, requested, validation_context
    )
    after_metrics = _tobishi_metrics(
        adjusted, requested, validation_context
    )
    requested_core = [
        employee for employee in requested
        if employee in set(_eco_core_names(original))
    ]
    requested_other = [
        employee for employee in requested if employee not in requested_core
    ]
    before_metrics["うちエコ主力"] = _tobishi_metrics(
        original, requested_core, validation_context
    )["休みに挟まれた単独出勤"]
    after_metrics["うちエコ主力"] = _tobishi_metrics(
        adjusted, requested_core, validation_context
    )["休みに挟まれた単独出勤"]
    before_metrics["うちその他スタッフ"] = _tobishi_metrics(
        original, requested_other, validation_context
    )["休みに挟まれた単独出勤"]
    after_metrics["うちその他スタッフ"] = _tobishi_metrics(
        adjusted, requested_other, validation_context
    )["休みに挟まれた単独出勤"]
    changes = _diff(original, adjusted)
    after_validation = validate_with_context(adjusted, validation_context, max_consec)
    notes = ["対象: " + "、".join(requested)]
    off_requests = (validation_context or {}).get("off_requests", {}) or {}
    for employee in requested:
        before_categories = classify_tobishi_days(
            original, employee, off_requests.get(employee, [])
        )
        after_categories = classify_tobishi_days(
            adjusted, employee, off_requests.get(employee, [])
        )
        before_work = before_categories["actionable"]
        after_work = after_categories["actionable"]
        before_requested = before_categories["requested_exception"]
        after_requested = after_categories["requested_exception"]
        before_work_text = "・".join(map(str, before_work)) or "なし"
        after_work_text = "・".join(map(str, after_work)) or "なし"
        before_requested_text = "・".join(map(str, before_requested)) or "なし"
        after_requested_text = "・".join(map(str, after_requested)) or "なし"
        remaining_text = after_work_text
        notes.append(
            f"{employee}: 改善対象 {before_work_text} → {after_work_text} / "
            f"本人希望による許容 {before_requested_text} → "
            f"{after_requested_text} / 改善できず残存 {remaining_text}"
        )
    notes.extend([
        (
            f"交換候補{move_pool_size}組を同時に比較し、"
            f"完成案{validated_count}件を安全性確認しました。"
        ),
        (
            f"検証結果: エラー {baseline_validation.error_count}→"
            f"{after_validation.error_count}件 / 警告 "
            f"{baseline_validation.warning_count}→{after_validation.warning_count}件"
        ),
        "本人の×休み、各日の店舗人数、各人の月間勤務日数は変更しません。",
    ])
    return AdjustmentProposal(
        title=f"飛び石勤務の再最適化（{label}）",
        summary=(
            f"{label}として、{len(changes) // 4}組・{len(changes)}セルの"
            "相互入れ替えを提案します。"
        ),
        changes=changes,
        before_metrics=before_metrics,
        after_metrics=after_metrics,
        notes=notes,
    )


def propose_tobishi_reoptimization_options(
    shift: MonthlyShift,
    *,
    validation_context: Optional[dict] = None,
    max_consec: int = 5,
    employee_names: Optional[Iterable[str]] = None,
    max_swaps: int = 6,
) -> list[AdjustmentProposal]:
    """Return exact OFF-WORK-OFF repair options for an existing shift."""
    original = clone_shift(shift)
    all_core = _eco_core_names(original)
    all_adjustable = _adjustable_employee_names(original)
    explicit_names = list(employee_names or [])
    requested = [
        name for name in (explicit_names or all_adjustable)
        if name in all_adjustable
    ]
    priority_names = requested if explicit_names else [
        name for name in all_core if name in requested
    ]
    if not requested:
        return [AdjustmentProposal(
            title="飛び石勤務の再最適化",
            summary="再調整対象となる通常スタッフが見つかりませんでした。",
        )]

    baseline_validation = validate_with_context(
        original, validation_context, max_consec
    )
    raw_moves = _enumerate_reciprocal_moves(
        original,
        requested=requested,
        priority_names=priority_names,
        protected_names=all_adjustable,
        validation_context=validation_context,
    )
    moves, building_blocks_checked = _individually_safe_move_pool(
        original,
        moves=raw_moves,
        validation_context=validation_context,
        max_consec=max_consec,
        baseline_validation=baseline_validation,
    )
    strategy_labels = [
        ("improvement", "改善優先案"),
        ("balanced", "バランス案"),
        ("minimal", "変更最小案"),
    ]
    options: list[AdjustmentProposal] = []
    seen_states: set[tuple] = set()
    checked_total = building_blocks_checked
    for strategy, label in strategy_labels:
        adjusted, checked, pool_size = _solve_tobishi_move_set(
            original,
            moves=moves,
            requested=requested,
            priority_names=priority_names,
            protected_names=all_adjustable,
            strategy=strategy,
            max_swaps=max_swaps,
            validation_context=validation_context,
            max_consec=max_consec,
            baseline_validation=baseline_validation,
        )
        checked_total += checked
        if adjusted is None:
            continue
        state_key = _shift_state_key(adjusted)
        if state_key in seen_states:
            continue
        seen_states.add(state_key)
        options.append(_proposal_for_tobishi_solution(
            original,
            adjusted,
            requested=requested,
            label=label,
            strategy=strategy,
            validated_count=checked,
            move_pool_size=pool_size,
            baseline_validation=baseline_validation,
            validation_context=validation_context,
            max_consec=max_consec,
        ))

    if options:
        options.sort(key=lambda proposal: (
            proposal.after_metrics.get("うちエコ主力", 0),
            proposal.after_metrics.get("休みに挟まれた単独出勤", 0),
            len(proposal.changes),
        ))
        options[0].title = "飛び石勤務の再最適化（改善優先案）"
        return options
    before_metrics = _tobishi_metrics(
        original, requested, validation_context
    )
    return [AdjustmentProposal(
        title="飛び石勤務の再最適化",
        summary=(
            "本人の×休み、店舗人数、月間勤務日数、ほかの絶対条件を守る"
            "完成案を、組み合わせ再最適化でも見つけられませんでした。"
        ),
        before_metrics=before_metrics,
        after_metrics=before_metrics,
        notes=[
            "対象: " + "、".join(requested),
            (
                f"交換候補{len(raw_moves)}組を確認し、安全な候補{len(moves)}組から"
                f"完成案を探索しました（検証{checked_total}回）。"
            ),
        ],
    )]


def propose_tobishi_reduction(
    shift: MonthlyShift,
    *,
    validation_context: Optional[dict] = None,
    max_consec: int = 5,
    employee_names: Optional[Iterable[str]] = None,
    max_swaps: int = 6,
) -> AdjustmentProposal:
    """Return the strongest safe exact-repair option, with beam fallback."""
    options = propose_tobishi_reoptimization_options(
        shift,
        validation_context=validation_context,
        max_consec=max_consec,
        employee_names=employee_names,
        max_swaps=max_swaps,
    )
    if options and options[0].has_changes:
        return options[0]
    return _propose_tobishi_beam(
        shift,
        validation_context=validation_context,
        max_consec=max_consec,
        employee_names=employee_names,
        max_swaps=max_swaps,
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
