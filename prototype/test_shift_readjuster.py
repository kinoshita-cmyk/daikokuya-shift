from __future__ import annotations

import unittest
from unittest.mock import patch

from prototype import shift_readjuster as readjuster
from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.shift_readjuster import (
    apply_adjustment_proposal,
    build_readjustment_quality_snapshot,
    classify_tobishi_days,
    compare_readjustment_quality,
    new_protected_issues,
    propose_tobishi_reduction,
    propose_tobishi_reoptimization_options,
    propose_yamamoto_cleanup,
    readjustment_shift_signature,
    tobishi_days,
)
from prototype.validator import Issue, ValidationResult


class ShiftReadjusterTest(unittest.TestCase):
    @staticmethod
    def _assignment(employee: str, day: int, store: Store) -> ShiftAssignment:
        return ShiftAssignment(employee=employee, day=day, store=store)

    def test_tobishi_proposal_preserves_daily_store_counts_and_workdays(self) -> None:
        assignments = []
        target_work_days = {2, 4, 5, 6}
        for day in range(1, 31):
            assignments.append(self._assignment(
                "今津", day,
                Store.OMIYA if day in target_work_days else Store.OFF,
            ))
            assignments.append(self._assignment(
                "鈴木", day,
                Store.OFF if day == 2 else Store.SUZURAN,
            ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)
        before_store_counts = {
            (day, store): sum(
                1 for a in shift.assignments
                if a.day == day and a.store == store
            )
            for day in range(1, 31)
            for store in Store
        }
        before_workdays = {
            name: sum(
                1 for a in shift.assignments
                if a.employee == name and a.store != Store.OFF
            )
            for name in ("今津", "鈴木")
        }

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            proposal = propose_tobishi_reduction(
                shift,
                employee_names=["今津"],
                max_swaps=1,
            )

        self.assertTrue(proposal.has_changes)
        adjusted = MonthlyShift(
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
        )
        for change in proposal.changes:
            adjusted.assignments = [
                a for a in adjusted.assignments
                if not (a.employee == change.employee and a.day == change.day)
            ]
            if change.after_store is not None:
                adjusted.assignments.append(ShiftAssignment(
                    employee=change.employee,
                    day=change.day,
                    store=change.after_store,
                ))

        after_store_counts = {
            (day, store): sum(
                1 for a in adjusted.assignments
                if a.day == day and a.store == store
            )
            for day in range(1, 31)
            for store in Store
        }
        after_workdays = {
            name: sum(
                1 for a in adjusted.assignments
                if a.employee == name and a.store != Store.OFF
            )
            for name in ("今津", "鈴木")
        }
        self.assertEqual(before_store_counts, after_store_counts)
        self.assertEqual(before_workdays, after_workdays)
        self.assertLess(
            len(tobishi_days(adjusted, "今津")),
            len(tobishi_days(shift, "今津")),
        )

    def test_tobishi_repair_can_join_lone_work_from_adjacent_day(self) -> None:
        assignments = []
        work_days = {
            # 5日が単独出勤。5日そのものを岩野の勤務日に移しても
            # 単独出勤のままだが、8日を4日へ移すと4-5日勤務になる。
            "春山": {5, 8, 9, 10},
            "岩野": {2, 3, 4, 9, 10},
        }
        stores = {
            "春山": Store.OMIYA,
            "岩野": Store.AKABANE,
        }
        for employee, employee_work_days in work_days.items():
            for day in range(1, 31):
                assignments.append(self._assignment(
                    employee,
                    day,
                    stores[employee] if day in employee_work_days else Store.OFF,
                ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        self.assertEqual(tobishi_days(shift, "春山"), [5])
        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            proposal = propose_tobishi_reduction(
                shift,
                employee_names=["春山"],
                max_swaps=1,
            )

        self.assertTrue(proposal.has_changes)
        adjusted = apply_adjustment_proposal(shift, proposal)
        self.assertEqual(tobishi_days(adjusted, "春山"), [])
        self.assertNotEqual(
            adjusted.get_assignment("春山", 4).store,
            Store.OFF,
        )
        self.assertNotEqual(
            adjusted.get_assignment("春山", 5).store,
            Store.OFF,
        )

    def test_employee_requested_tobishi_is_reported_but_not_adjusted(self) -> None:
        assignments = []
        target_work_days = {2, 4, 5, 6}
        for day in range(1, 31):
            assignments.append(self._assignment(
                "今津", day,
                Store.OMIYA if day in target_work_days else Store.OFF,
            ))
            assignments.append(self._assignment(
                "鈴木", day,
                Store.OFF if day == 2 else Store.SUZURAN,
            ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)
        context = {"off_requests": {"今津": [1, 3]}}

        categories = classify_tobishi_days(shift, "今津", [1, 3])
        self.assertEqual(categories["actionable"], [])
        self.assertEqual(categories["requested_exception"], [2])

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            proposal = propose_tobishi_reduction(
                shift,
                validation_context=context,
                employee_names=["今津"],
                max_swaps=1,
            )

        self.assertFalse(proposal.has_changes)
        self.assertEqual(
            proposal.before_metrics["休みに挟まれた単独出勤"], 0
        )
        self.assertEqual(proposal.before_metrics["本人希望による許容"], 1)

    def test_one_sided_off_request_stays_actionable(self) -> None:
        shift = MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                self._assignment("今津", 1, Store.OFF),
                self._assignment("今津", 2, Store.AKABANE),
                self._assignment("今津", 3, Store.OFF),
            ],
        )

        categories = classify_tobishi_days(shift, "今津", [1])

        self.assertEqual(categories["actionable"], [2])
        self.assertEqual(categories["requested_exception"], [])

    def test_yamamoto_cleanup_only_removes_unneeded_akabane_days(self) -> None:
        shift = MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                self._assignment("今津", 1, Store.AKABANE),
                self._assignment("板倉", 1, Store.AKABANE),
                self._assignment("田中", 1, Store.AKABANE),
                self._assignment("山本", 1, Store.AKABANE),
                self._assignment("今津", 2, Store.AKABANE),
                self._assignment("板倉", 2, Store.AKABANE),
                self._assignment("山本", 2, Store.AKABANE),
            ],
        )

        proposal = propose_yamamoto_cleanup(shift)

        self.assertEqual(len(proposal.changes), 1)
        self.assertEqual(proposal.changes[0].day, 1)
        self.assertIsNone(proposal.changes[0].after_store)

    def test_isolated_off_day_is_not_a_readjustment_goal(self) -> None:
        assignments = []
        target_work_days = {1, 2, 4, 5, 6}
        other_work_days = set(range(1, 31)) - {6}
        for day in range(1, 31):
            assignments.append(self._assignment(
                "今津", day,
                Store.OMIYA if day in target_work_days else Store.OFF,
            ))
            assignments.append(self._assignment(
                "鈴木", day,
                Store.SUZURAN if day in other_work_days else Store.OFF,
            ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        self.assertEqual(tobishi_days(shift, "今津"), [])
        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            proposal = propose_tobishi_reduction(
                shift,
                employee_names=["今津"],
                max_swaps=1,
            )

        self.assertFalse(proposal.has_changes)

    def test_tobishi_search_tries_next_candidate_after_rule_violation(self) -> None:
        assignments = []
        target_work_days = {2, 4, 5, 6}
        for day in range(1, 31):
            assignments.append(self._assignment(
                "今津", day,
                Store.OMIYA if day in target_work_days else Store.OFF,
            ))
            for name in ("大塚", "鈴木"):
                assignments.append(self._assignment(
                    name, day,
                    Store.SUZURAN if day == 3 else Store.OFF,
                ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        def validate_candidate(candidate, *_args, **_kwargs):
            otsuka_is_at_omiya = any(
                assignment.employee == "大塚"
                and assignment.store == Store.OMIYA
                for assignment in candidate.assignments
            )
            if otsuka_is_at_omiya:
                return ValidationResult(issues=[Issue(
                    severity="ERROR",
                    category="絶対配置不可",
                    day=2,
                    employee="大塚",
                    message="テスト用の配置不可",
                )])
            return ValidationResult()

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            side_effect=validate_candidate,
        ):
            proposal = propose_tobishi_reduction(
                shift,
                employee_names=["今津"],
                max_swaps=1,
            )

        self.assertTrue(proposal.has_changes)
        changed_names = {change.employee for change in proposal.changes}
        self.assertIn("鈴木", changed_names)
        self.assertNotIn("大塚", changed_names)

    def test_default_readjustment_also_reduces_non_core_lone_work(self) -> None:
        assignments = []
        work_days = {
            "鈴木": {2, 4, 5, 6},
            "大塚": {3, 5, 6, 7},
        }
        for employee, employee_work_days in work_days.items():
            for day in range(1, 31):
                assignments.append(self._assignment(
                    employee,
                    day,
                    Store.AKABANE if day in employee_work_days else Store.OFF,
                ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            proposal = propose_tobishi_reduction(shift, max_swaps=1)

        self.assertTrue(proposal.has_changes)
        adjusted = apply_adjustment_proposal(shift, proposal)
        self.assertLess(
            sum(len(tobishi_days(adjusted, name)) for name in work_days),
            sum(len(tobishi_days(shift, name)) for name in work_days),
        )

    def test_exact_reoptimization_improves_two_named_employees_together(self) -> None:
        assignments = []
        work_days = {
            "今津": {2, 4, 5, 6},
            # 7-8勤務を置き、10日を11日へ動かしても新しい3連休に
            # ならない現実的な交換候補にする。
            "春山": {7, 8, 10, 12, 13, 14},
            "鈴木": {3},
            "大塚": {11},
        }
        stores = {
            "今津": Store.OMIYA,
            "春山": Store.OMIYA,
            "鈴木": Store.SUZURAN,
            "大塚": Store.SUZURAN,
        }
        for employee, employee_work_days in work_days.items():
            for day in range(1, 31):
                assignments.append(self._assignment(
                    employee,
                    day,
                    stores[employee] if day in employee_work_days else Store.OFF,
                ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            options = propose_tobishi_reoptimization_options(
                shift,
                employee_names=["今津", "春山"],
                max_swaps=2,
            )

        best = options[0]
        self.assertTrue(best.has_changes)
        self.assertEqual(
            best.before_metrics["休みに挟まれた単独出勤"], 2
        )
        self.assertEqual(
            best.after_metrics["休みに挟まれた単独出勤"], 0
        )
        changed_names = {change.employee for change in best.changes}
        self.assertIn("今津", changed_names)
        self.assertIn("春山", changed_names)

    def test_exact_reoptimization_keeps_complementary_unsafe_moves(self) -> None:
        assignments = []
        work_days = {
            "今津": {2, 4, 5, 6},
            "春山": {10, 12, 13, 14},
            "鈴木": {3},
            "大塚": {11},
        }
        stores = {
            "今津": Store.OMIYA,
            "春山": Store.OMIYA,
            "鈴木": Store.SUZURAN,
            "大塚": Store.SUZURAN,
        }
        for employee, employee_work_days in work_days.items():
            for day in range(1, 31):
                assignments.append(self._assignment(
                    employee,
                    day,
                    stores[employee] if day in employee_work_days else Store.OFF,
                ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)

        def validate_combination(candidate, *_args, **_kwargs):
            remaining = sum(
                len(tobishi_days(candidate, employee))
                for employee in ("今津", "春山")
            )
            if candidate is shift or remaining in {0, 2}:
                return ValidationResult()
            return ValidationResult(issues=[Issue(
                severity="WARNING",
                category="組み合わせ途中",
                day=1,
                employee="今津",
                message="もう一方の交換と組み合わせれば解消します",
            )])

        with patch(
            "prototype.shift_readjuster.validate_with_context",
            side_effect=validate_combination,
        ):
            options = propose_tobishi_reoptimization_options(
                shift,
                employee_names=["今津", "春山"],
                max_swaps=2,
            )

        best = options[0]
        self.assertTrue(best.has_changes)
        self.assertEqual(
            best.after_metrics["休みに挟まれた単独出勤"], 0
        )
        self.assertEqual(len(best.changes), 8)

    def test_exact_reoptimization_tries_safe_pool_before_complementary_pool(self) -> None:
        assignments = []
        target_work_days = {2, 4, 5, 6}
        for day in range(1, 31):
            assignments.append(self._assignment(
                "今津",
                day,
                Store.OMIYA if day in target_work_days else Store.OFF,
            ))
            assignments.append(self._assignment(
                "鈴木",
                day,
                Store.OFF if day == 2 else Store.SUZURAN,
            ))
        shift = MonthlyShift(year=2026, month=9, assignments=assignments)
        safe_move = readjuster._ReciprocalMove(
            target="今津",
            other="鈴木",
            target_work_day=2,
            target_off_day=3,
        )
        complementary_move = readjuster._ReciprocalMove(
            target="今津",
            other="鈴木",
            target_work_day=4,
            target_off_day=3,
        )
        searched_pools = []

        def solve_pool(original, *, moves, **_kwargs):
            searched_pools.append(list(moves))
            self.assertEqual(list(moves), [safe_move])
            return (
                readjuster._apply_move_set(original, [safe_move]),
                1,
                len(moves),
            )

        with patch(
            "prototype.shift_readjuster._enumerate_reciprocal_moves",
            return_value=[safe_move, complementary_move],
        ), patch(
            "prototype.shift_readjuster._candidate_move_pool",
            return_value=([safe_move], [complementary_move], 2),
        ), patch(
            "prototype.shift_readjuster._solve_tobishi_move_set",
            side_effect=solve_pool,
        ), patch(
            "prototype.shift_readjuster.validate_with_context",
            return_value=ValidationResult(),
        ):
            options = propose_tobishi_reoptimization_options(
                shift,
                employee_names=["今津"],
                max_swaps=2,
            )

        self.assertTrue(options[0].has_changes)
        self.assertTrue(searched_pools)
        self.assertTrue(all(pool == [safe_move] for pool in searched_pools))

    def test_quality_guard_reports_regressed_indicators(self) -> None:
        baseline = {
            "tobishi_work:今津": {
                "label": "今津の単独出勤（休・出・休）",
                "value": 1,
            },
            "short_staff_days": {
                "label": "人員不足がある日数",
                "value": 2,
            },
        }
        current = {
            "tobishi_work:今津": {
                "label": "今津の単独出勤（休・出・休）",
                "value": 2,
            },
            "short_staff_days": {
                "label": "人員不足がある日数",
                "value": 1,
            },
        }

        regressions = compare_readjustment_quality(baseline, current)

        self.assertEqual(len(regressions), 1)
        self.assertEqual(regressions[0].label, "今津の単独出勤（休・出・休）")
        self.assertEqual(regressions[0].before, 1)
        self.assertEqual(regressions[0].after, 2)

    def test_new_issue_detection_distinguishes_stores_on_same_day(self) -> None:
        before = ValidationResult(issues=[Issue(
            severity="WARNING",
            category="店舗人数",
            day=8,
            employee=None,
            message="大宮駅前店が2名です",
        )])
        after_issue = Issue(
            severity="WARNING",
            category="店舗人数",
            day=8,
            employee=None,
            message="赤羽駅前店が2名です",
        )
        after = ValidationResult(issues=[after_issue])

        self.assertEqual(new_protected_issues(before, after), [after_issue])

    def test_quality_snapshot_tracks_tobishi_staffing_and_errors(self) -> None:
        shift = MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                self._assignment("今津", 1, Store.OFF),
                self._assignment("今津", 2, Store.AKABANE),
                self._assignment("今津", 3, Store.OFF),
                self._assignment("山本", 2, Store.AKABANE),
            ],
        )
        validation = ValidationResult(issues=[Issue(
            severity="ERROR",
            category="絶対配置不可",
            day=2,
            employee="今津",
            message="テスト用",
        )])

        snapshot = build_readjustment_quality_snapshot(
            shift,
            validation,
            {2: {Store.OMIYA}},
        )

        self.assertEqual(snapshot["tobishi_work:今津"]["value"], 1)
        self.assertEqual(snapshot["yamamoto_workdays"]["value"], 1)
        self.assertEqual(snapshot["short_staff_days"]["value"], 1)
        self.assertEqual(
            snapshot["issue:ERROR:絶対配置不可"]["value"], 1
        )

    def test_shift_signature_changes_with_assignment(self) -> None:
        first = MonthlyShift(
            year=2026,
            month=9,
            assignments=[self._assignment("今津", 1, Store.AKABANE)],
        )
        second = MonthlyShift(
            year=2026,
            month=9,
            assignments=[self._assignment("今津", 1, Store.OMIYA)],
        )
        self.assertNotEqual(
            readjustment_shift_signature(first),
            readjustment_shift_signature(second),
        )


if __name__ == "__main__":
    unittest.main()
