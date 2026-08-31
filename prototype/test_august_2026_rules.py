"""固定ルール・2026年8月条件・山本さん補助上限の回帰テスト。"""

import unittest

from prototype.generator import (
    AVOID_SAME_OFF_PENALTY,
    AKABANE_SHORTAGE_PENALTY,
    BALANCED_NORMAL_STORE_DIFF_PENALTY,
    OMIYA_SHORTAGE_PENALTY,
    REMOVED_SUPPORT_STORE_ASSIGNMENT_PENALTY,
    SUZURAN_CORE_ABSENCE_PENALTY,
)
from prototype.employees import get_employee, shift_active_employees
from prototype.capacity_balance import (
    _auxiliary_monthly_supply,
    balance_summary_lines,
)
from prototype.models import (
    Affinity,
    MonthlyShift,
    OperationMode,
    ShiftAssignment,
    Store,
)
from prototype.rules import (
    YamamotoLogic,
    fixed_suzuran_core_presence_rules,
    is_omiya_anchor_relaxed_month,
    is_omiya_two_person_allowed_month,
    monthly_avoid_same_off_rules,
    monthly_employee_store_override,
    tanaka_pair_training_rule,
    yamamoto_monthly_max_days,
)
from prototype.validator import (
    ValidationResult,
    _check_monthly_avoid_same_off_rules,
    _check_store_capacity,
    _check_yamamoto_monthly_max,
)


class August2026RulesTest(unittest.TestCase):
    def test_fixed_omiya_rule_and_august_rules_do_not_leak(self):
        self.assertTrue(is_omiya_two_person_allowed_month(2026, 8))
        self.assertTrue(is_omiya_two_person_allowed_month(2026, 9))
        self.assertFalse(is_omiya_anchor_relaxed_month(2026, 8))

        training = tanaka_pair_training_rule(2026, 8)
        self.assertIsNotNone(training)
        self.assertEqual(training["nishiguchi_count"], 6)
        self.assertEqual(training["akabane_count"], 5)
        self.assertEqual(training["higashiguchi_count"], 1)
        self.assertIsNone(tanaka_pair_training_rule(2026, 9))

        august_oorui = monthly_employee_store_override(2026, 8, "大類")
        self.assertEqual(august_oorui["primary_store"], Store.OMIYA)
        self.assertIn(
            Store.AKABANE,
            august_oorui["remove_support_stores"],
        )
        september_oorui = monthly_employee_store_override(2026, 9, "大類")
        self.assertIsNone(september_oorui["primary_store"])
        self.assertFalse(september_oorui["remove_support_stores"])

    def test_imazu_suzuran_is_support(self):
        imazu = get_employee("今津")
        self.assertEqual(imazu.affinities[Store.SUZURAN], Affinity.WEAK)
        self.assertEqual(
            imazu.affinities[Store.HIGASHIGUCHI],
            Affinity.MEDIUM,
        )
        self.assertEqual(
            imazu.affinities[Store.NISHIGUCHI],
            Affinity.MEDIUM,
        )

    def test_yamamoto_limits_are_validation_only(self):
        self.assertEqual(yamamoto_monthly_max_days(1), 14)
        self.assertEqual(yamamoto_monthly_max_days(2), 14)
        self.assertEqual(yamamoto_monthly_max_days(8), 15)
        self.assertEqual(_auxiliary_monthly_supply("山本", 1), 0)
        self.assertEqual(_auxiliary_monthly_supply("山本", 8), 0)

        non_consecutive_days = [
            day for day in range(1, 32) if day % 3 != 0
        ]
        august = MonthlyShift(year=2026, month=8)
        august.assignments = [
            ShiftAssignment(
                employee="山本",
                day=day,
                store=Store.AKABANE,
            )
            for day in non_consecutive_days[:16]
        ]
        result = ValidationResult()
        _check_yamamoto_monthly_max(august, result)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, "ERROR")

        within_limit = MonthlyShift(year=2026, month=8)
        within_limit.assignments = [
            ShiftAssignment(
                employee="山本",
                day=day,
                store=Store.AKABANE,
            )
            for day in non_consecutive_days[:15]
        ]
        within_limit_result = ValidationResult()
        _check_yamamoto_monthly_max(within_limit, within_limit_result)
        self.assertFalse(within_limit_result.issues)

        three_consecutive = MonthlyShift(year=2026, month=8)
        three_consecutive.assignments = [
            ShiftAssignment(employee="山本", day=day, store=Store.AKABANE)
            for day in (1, 2, 3)
        ]
        consecutive_result = ValidationResult()
        _check_yamamoto_monthly_max(three_consecutive, consecutive_result)
        self.assertEqual(
            [issue.category for issue in consecutive_result.issues],
            ["山本連続勤務上限"],
        )

    def test_yamamoto_is_added_only_for_akabane_ticket_shortage(self):
        self.assertTrue(YamamotoLogic.should_deploy(1, 1, False))
        self.assertTrue(YamamotoLogic.should_deploy(2, 0, False))
        self.assertFalse(YamamotoLogic.should_deploy(1, 2, False))
        self.assertFalse(YamamotoLogic.should_deploy(2, 1, False))
        self.assertFalse(YamamotoLogic.should_deploy(1, 1, True))

    def test_capacity_summary_does_not_infer_omiya_staffing_from_two_eco(self):
        lines = balance_summary_lines({
            "demand_total": 336,
            "supply_total": 336,
            "slack_total": 0,
            "mode_reduction": 0,
            "demand_eco_baseline": 150,
            "eco_supply": 169,
            "eco_surplus": 19,
            "omiya_normal_open_days": 31,
            "east_west_gap": 0,
            "east_west_demand": 57,
            "east_west_dedicated_supply": 57,
            "east_west_dedicated": ["土井", "楯"],
            "east_west_substitutes": ["春山", "長尾", "今津"],
        })
        summary = "\n".join(lines)
        self.assertIn("エコ担当はチケット対応も可能", summary)
        self.assertIn("エコ対応1名以上・合計3名", summary)
        self.assertNotIn("エコ2人にできる日は最大", summary)

    def test_removed_support_store_uses_yamamoto_before_omiya_shortage(self):
        self.assertGreater(
            REMOVED_SUPPORT_STORE_ASSIGNMENT_PENALTY,
            AKABANE_SHORTAGE_PENALTY,
        )
        self.assertLess(
            REMOVED_SUPPORT_STORE_ASSIGNMENT_PENALTY,
            OMIYA_SHORTAGE_PENALTY,
        )
        self.assertGreater(
            OMIYA_SHORTAGE_PENALTY,
            (
                BALANCED_NORMAL_STORE_DIFF_PENALTY
                + AKABANE_SHORTAGE_PENALTY
            ),
        )
        self.assertGreater(
            AVOID_SAME_OFF_PENALTY,
            BALANCED_NORMAL_STORE_DIFF_PENALTY,
        )
        self.assertLess(
            AVOID_SAME_OFF_PENALTY,
            OMIYA_SHORTAGE_PENALTY,
        )
        self.assertGreater(
            SUZURAN_CORE_ABSENCE_PENALTY,
            OMIYA_SHORTAGE_PENALTY,
        )

    def test_nagao_nozawa_same_off_avoidance_is_fixed_rule(self):
        pairs = {
            frozenset((first_name, second_name))
            for first_name, second_name, _reason
            in fixed_suzuran_core_presence_rules()
        }
        self.assertIn(frozenset(("長尾", "野澤")), pairs)

    def test_fixed_same_off_rule_does_not_change_august_generation(self):
        self.assertEqual(monthly_avoid_same_off_rules(2026, 8), ())

    def test_suzuran_core_warning_checks_store_presence_not_only_days_off(self):
        absent_shift = MonthlyShift(year=2026, month=8)
        absent_shift.assignments = [
            ShiftAssignment(
                employee="長尾",
                day=1,
                store=Store.NISHIGUCHI,
            ),
            ShiftAssignment(
                employee="野澤",
                day=1,
                store=Store.OFF,
            ),
        ]
        absent_result = ValidationResult()
        _check_monthly_avoid_same_off_rules(
            absent_shift,
            absent_result,
            days=1,
        )
        self.assertEqual(len(absent_result.issues), 1)
        self.assertEqual(
            absent_result.issues[0].category,
            "すずらん主力不在",
        )

        covered_shift = MonthlyShift(year=2026, month=8)
        covered_shift.assignments = [
            ShiftAssignment(
                employee="長尾",
                day=1,
                store=Store.SUZURAN,
            ),
            ShiftAssignment(
                employee="野澤",
                day=1,
                store=Store.OFF,
            ),
        ]
        covered_result = ValidationResult()
        _check_monthly_avoid_same_off_rules(
            covered_shift,
            covered_result,
            days=1,
        )
        self.assertFalse(covered_result.issues)

    def test_august_tanaka_higashiguchi_pair_is_not_over_capacity(self):
        shift = MonthlyShift(year=2026, month=8)
        shift.operation_modes = {
            day: OperationMode.NORMAL for day in range(1, 22)
        }
        shift.assignments = [
            ShiftAssignment(
                employee="土井",
                day=21,
                store=Store.HIGASHIGUCHI,
            ),
            ShiftAssignment(
                employee="田中",
                day=21,
                store=Store.HIGASHIGUCHI,
            ),
        ]
        result = ValidationResult()
        _check_store_capacity(
            shift,
            result,
            21,
            allow_omiya_short=True,
        )
        day_21_east_errors = [
            issue for issue in result.issues
            if issue.day == 21
            and issue.severity == "ERROR"
            and "東口" in issue.message
        ]
        self.assertFalse(day_21_east_errors)

    def test_omiya_two_person_warning_allows_one_eco_and_one_ticket(self):
        shift = MonthlyShift(year=2026, month=9)
        shift.operation_modes = {1: OperationMode.NORMAL}
        shift.assignments = [
            ShiftAssignment(
                employee="春山",
                day=1,
                store=Store.OMIYA,
            ),
            ShiftAssignment(
                employee="大類",
                day=1,
                store=Store.OMIYA,
            ),
        ]
        result = ValidationResult()
        _check_store_capacity(
            shift,
            result,
            1,
            allow_omiya_short=True,
        )
        omiya_issues = [
            issue for issue in result.issues
            if issue.day == 1 and "大宮駅前店" in issue.message
        ]
        self.assertEqual(len(omiya_issues), 1)
        self.assertEqual(omiya_issues[0].severity, "WARNING")
        self.assertIn("エコ対応1名・合計2名体制", omiya_issues[0].message)

    def test_omiya_two_person_warning_allows_two_eco_staff(self):
        shift = MonthlyShift(year=2026, month=9)
        shift.operation_modes = {1: OperationMode.NORMAL}
        shift.assignments = [
            ShiftAssignment(employee="春山", day=1, store=Store.OMIYA),
            ShiftAssignment(employee="下地", day=1, store=Store.OMIYA),
        ]
        result = ValidationResult()
        _check_store_capacity(
            shift,
            result,
            1,
            allow_omiya_short=True,
        )
        omiya_issues = [
            issue for issue in result.issues
            if issue.day == 1 and "大宮駅前店" in issue.message
        ]
        self.assertEqual(len(omiya_issues), 1)
        self.assertEqual(omiya_issues[0].severity, "WARNING")
        self.assertIn("エコ対応2名・合計2名体制", omiya_issues[0].message)

    def test_omiya_three_person_team_needs_only_one_eco_capable_staff(self):
        shift = MonthlyShift(year=2026, month=9)
        shift.operation_modes = {1: OperationMode.NORMAL}
        shift.assignments = [
            ShiftAssignment(employee="下地", day=1, store=Store.OMIYA),
            ShiftAssignment(employee="黒澤", day=1, store=Store.OMIYA),
            ShiftAssignment(employee="大類", day=1, store=Store.OMIYA),
        ]
        result = ValidationResult()
        _check_store_capacity(
            shift,
            result,
            1,
            allow_omiya_short=True,
        )
        omiya_issues = [
            issue for issue in result.issues
            if issue.day == 1 and "大宮駅前店" in issue.message
        ]
        self.assertFalse(omiya_issues)


if __name__ == "__main__":
    unittest.main()
