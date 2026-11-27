"""12/28の東口特別営業を、生成・検証・表示・需給計算で照合する。"""
import ast
from calendar import monthrange
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from prototype import rules
from prototype.capacity_balance import compute_monthly_capacity_balance
from prototype.excel_exporter import detect_key_warnings_by_store
from prototype.infeasibility_diagnosis import diagnose_known_conflicts
from prototype.models import MonthlyShift, OperationMode, ShiftAssignment, Store
from prototype.rule_book import monthly_cards
from prototype.staffing import detect_short_staff_by_store
from prototype.validator import (
    ValidationResult, _check_store_capacity, _check_eco_placement,
    _check_higashiguchi_monday_closed, _check_store_keyholders,
)


class SpecialStoreOpeningTest(unittest.TestCase):
    def shift(self, day=28, name=None, mode=OperationMode.NORMAL):
        return MonthlyShift(2026, 12,
            operation_modes={d: mode if d == day else OperationMode.CLOSED for d in range(1, 32)},
            assignments=[ShiftAssignment(name, day, Store.HIGASHIGUCHI)] if name else [])

    def test_only_2026_december_28_overrides_the_weekly_closure(self):
        before = deepcopy((rules.NORMAL_CAPACITY, rules.REDUCED_CAPACITY))
        for mode in (OperationMode.NORMAL, OperationMode.REDUCED):
            self.assertTrue(rules.is_store_open_on_day(2026, 12, 28, Store.HIGASHIGUCHI, mode))
            for year, month, day in ((2026, 12, 7), (2026, 12, 14), (2026, 12, 21),
                                     (2026, 11, 30), (2027, 1, 4), (2027, 12, 27)):
                self.assertFalse(rules.is_store_open_on_day(year, month, day, Store.HIGASHIGUCHI, mode))
        self.assertEqual(before, (rules.NORMAL_CAPACITY, rules.REDUCED_CAPACITY))
        for year, month in ((2026, 11), (2027, 1), (2027, 12)):
            self.assertFalse(rules.special_store_open_days(year, month))

    def test_explicit_closed_or_minimum_mode_is_not_overridden(self):
        for mode in (OperationMode.CLOSED, OperationMode.MINIMUM):
            self.assertFalse(rules.is_store_open_on_day(2026, 12, 28, Store.HIGASHIGUCHI, mode))
            shift = self.shift(mode=mode)
            self.assertNotIn(Store.HIGASHIGUCHI, detect_short_staff_by_store(shift).get(28, set()))

    def test_east_staff_and_independent_eco_are_required_on_special_open_day(self):
        for name, requires_eco in ((None, True), ("田中", True), ("土井", False)):
            shift = self.shift(name=name)
            result = ValidationResult()
            _check_store_capacity(shift, result, 28)
            _check_eco_placement(shift, result, 28)
            _check_higashiguchi_monday_closed(shift, result, 28)
            self.assertFalse(any(i.category == "東口月曜休店" for i in result.issues))
            east_eco = [i for i in result.issues if i.category == "必須エコ" and "赤羽東口店" in i.message]
            self.assertEqual(bool(east_eco), requires_eco)
            self.assertEqual(Store.HIGASHIGUCHI in detect_short_staff_by_store(shift).get(28, set()), requires_eco)
        result = ValidationResult()
        _check_higashiguchi_monday_closed(self.shift(day=21, name="土井"), result, 28)
        self.assertTrue(any(i.day == 21 and i.category == "東口月曜休店" for i in result.issues))

    def test_key_checks_match_in_validator_screen_excel_and_pdf(self):
        from prototype.pdf_exporter import detect_key_warnings_by_store as pdf_keys
        source = Path(__file__).resolve().parents[1] / "app/app.py"
        function = next(n for n in ast.parse(source.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "detect_key_warnings_by_store")
        namespace = dict(MonthlyShift=MonthlyShift, Store=Store, OperationMode=OperationMode,
                         monthrange=monthrange, STORE_KEYHOLDERS=rules.STORE_KEYHOLDERS,
                         SUZURAN_KEY_SUPPORT_FROM_OMIYA=rules.SUZURAN_KEY_SUPPORT_FROM_OMIYA,
                         is_store_open_on_day=rules.is_store_open_on_day)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        for name, missing in (("顧問", True), ("土井", False)):
            shift = self.shift(name=name)
            result = ValidationResult()
            _check_store_keyholders(shift, result, 28)
            self.assertEqual(any(i.category == "鍵不足" and "赤羽東口店" in i.message for i in result.issues), missing)
            for detect in (detect_key_warnings_by_store, pdf_keys, namespace[function.name]):
                self.assertEqual(Store.HIGASHIGUCHI in detect(shift).get(28, {}), missing)

    def test_capacity_balance_counts_one_extra_eco_and_one_extra_person(self):
        current = compute_monthly_capacity_balance(2026, 12, [], {})
        with patch.object(rules, "SPECIAL_STORE_OPEN_DAYS", {}):
            previous = compute_monthly_capacity_balance(2026, 12, [], {})
        for field in ("demand_total", "demand_eco_baseline", "east_west_demand"):
            self.assertEqual(current[field], previous[field] + 1)
        self.assertEqual(current["higashi_closed_days"], previous["higashi_closed_days"] - 1)

    def test_diagnosis_does_not_reject_special_opening(self):
        for day, closed in ((28, False), (21, True)):
            findings = diagnose_known_conflicts(2026, 12, required_assignments=[
                {"employee": "土井", "day": day, "store": Store.HIGASHIGUCHI, "severity": "ERROR"}])
            self.assertEqual(any(i["区分"] == "休店日への強制配置" for i in findings), closed)

    def test_monthly_card_and_description_are_scoped_and_saved_settings_untouched(self):
        before = deepcopy(rules.load_monthly_exceptions_raw())
        cards = [c for c in monthly_cards(2026, 12) if "特別営業" in c["title"]]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["scope"], "2026-12")
        self.assertEqual(cards[0]["strength"], "絶対")
        self.assertIn("赤羽東口店", cards[0]["text"])
        for year, month in ((2026, 11), (2027, 1), (2027, 12)):
            self.assertFalse(any("特別営業" in c["title"] for c in monthly_cards(year, month)))
        rules.reload_monthly_exceptions()
        self.assertEqual(before, rules.load_monthly_exceptions_raw())
        self.assertTrue(rules.is_store_open_on_day(2026, 12, 28, Store.HIGASHIGUCHI, OperationMode.NORMAL))

    def test_actual_generator_can_staff_special_monday_but_not_other_mondays(self):
        from prototype.test_december_2026_policy import DecemberGenerationTest
        fixture = DecemberGenerationTest()
        for day, allowed in ((28, True), (21, False)):
            shift, diagnostics = fixture.fixture([day], event=False, yama_off=True)
            self.assertEqual(shift is not None, allowed, diagnostics)
            if shift:
                self.assertEqual(shift.get_assignment("土井", day).store, Store.HIGASHIGUCHI)
                result = ValidationResult()
                _check_higashiguchi_monday_closed(shift, result, 31)
                self.assertFalse(result.issues)


if __name__ == "__main__":
    unittest.main()
