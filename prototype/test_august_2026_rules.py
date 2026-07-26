"""2026年8月の確定条件と山本さん補助上限の回帰テスト。"""

import unittest

from prototype.employees import get_employee
from prototype.capacity_balance import _auxiliary_monthly_supply
from prototype.models import (
    Affinity,
    MonthlyShift,
    ShiftAssignment,
    Store,
)
from prototype.rules import (
    YamamotoLogic,
    is_omiya_anchor_relaxed_month,
    is_omiya_two_person_allowed_month,
    monthly_employee_store_override,
    tanaka_pair_training_rule,
    yamamoto_monthly_max_days,
)
from prototype.validator import (
    ValidationResult,
    _check_yamamoto_monthly_max,
)


class August2026RulesTest(unittest.TestCase):
    def test_august_rules_do_not_leak_into_september(self):
        self.assertTrue(is_omiya_two_person_allowed_month(2026, 8))
        self.assertFalse(is_omiya_two_person_allowed_month(2026, 9))
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

        august = MonthlyShift(year=2026, month=8)
        august.assignments = [
            ShiftAssignment(
                employee="山本",
                day=day,
                store=Store.AKABANE,
            )
            for day in range(1, 17)
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
            for day in range(1, 16)
        ]
        within_limit_result = ValidationResult()
        _check_yamamoto_monthly_max(within_limit, within_limit_result)
        self.assertFalse(within_limit_result.issues)

    def test_yamamoto_is_added_only_for_akabane_ticket_shortage(self):
        self.assertTrue(YamamotoLogic.should_deploy(1, 1, False))
        self.assertTrue(YamamotoLogic.should_deploy(2, 0, False))
        self.assertFalse(YamamotoLogic.should_deploy(1, 2, False))
        self.assertFalse(YamamotoLogic.should_deploy(2, 1, False))
        self.assertFalse(YamamotoLogic.should_deploy(1, 1, True))


if __name__ == "__main__":
    unittest.main()
