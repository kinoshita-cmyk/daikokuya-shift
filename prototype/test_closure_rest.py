"""全店休業による不可避な連休と、追加の自然休を生成・検証で区別する。"""
from copy import deepcopy
import ast
from itertools import product
from pathlib import Path
import unittest
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype.consecutive_counts import consecutive_off_limit_windows
from prototype.infeasibility_diagnosis import diagnose_known_conflicts
from prototype.models import Employee, MonthlyShift, OperationMode, ShiftAssignment, Store
from prototype.validator import ValidationResult, _check_consecutive_off, _check_monthly_avoid_same_off_rules


class ClosureRestTest(unittest.TestCase):
    def test_december_actual_conflicts_and_no_extra_free_day(self):
        modes = {30: OperationMode.CLOSED, 31: OperationMode.CLOSED}
        before = deepcopy(modes)
        windows, exceptions = consecutive_off_limit_windows(31, 2, [29, 30], modes)
        self.assertNotIn((29, 30, 31), windows)
        self.assertIn((29, 30, 31), exceptions)
        self.assertIn((28, 29, 30), windows)
        windows, exceptions = consecutive_off_limit_windows(31, 1, [30], modes)
        self.assertNotIn((30, 31), windows)
        self.assertIn((30, 31), exceptions)
        self.assertIn((29, 30), windows)
        self.assertEqual(modes, before)

    def test_all_requested_leave_is_still_exempt_without_claiming_closure_exception(self):
        windows, exceptions = consecutive_off_limit_windows(3, 2, [1, 2, 3], {})
        self.assertFalse(windows)
        self.assertFalse(exceptions)

    def test_reduced_minimum_and_unspecified_modes_do_not_create_exemptions(self):
        for mode in (OperationMode.NORMAL, OperationMode.REDUCED, OperationMode.MINIMUM, None):
            windows, exceptions = consecutive_off_limit_windows(3, 2, [1, 2], {3: mode})
            self.assertEqual(windows, [(1, 2, 3)])
            self.assertFalse(exceptions)

    def test_generation_and_validation_agree_for_requested_closed_and_free_rest(self):
        employee = Employee(name="検証対象")
        for max_off in (1, 2):
            size = max_off + 1
            for causes in product(("requested", "closed", "free"), repeat=size):
                requested = [d for d, cause in enumerate(causes, 1) if cause == "requested"]
                modes = {d: OperationMode.CLOSED for d, cause in enumerate(causes, 1) if cause == "closed"}
                windows, _ = consecutive_off_limit_windows(size, max_off, requested, modes)
                model = cp_model.CpModel()
                off = {d: model.NewBoolVar(str(d)) for d in range(1, size + 1)}
                for var in off.values():
                    model.Add(var == 1)
                for window in windows:
                    model.Add(sum(off[d] for d in window) <= max_off)
                solver = cp_model.CpSolver()
                solver.parameters.num_search_workers = 1
                invalid = solver.Solve(model) == cp_model.INFEASIBLE
                self.assertEqual(invalid, "free" in causes, causes)
                for year, month in ((2026, 12), (2027, 1), (2027, 8)):
                    shift = MonthlyShift(year, month, operation_modes=modes, assignments=[
                        ShiftAssignment(employee.name, d, Store.OFF) for d in off])
                    result = ValidationResult()
                    with patch("prototype.validator._validation_employees", return_value=[employee]):
                        _check_consecutive_off(shift, result, size, {employee.name: requested},
                                               {employee.name: max_off})
                    self.assertEqual(any(i.severity == "ERROR" for i in result.issues), invalid,
                                     (year, month, causes, result.issues))

    def test_missing_assignment_on_open_day_is_not_a_closure_exception(self):
        person = Employee(name="検証対象")
        shift = MonthlyShift(2026, 12, operation_modes={3: OperationMode.CLOSED}, assignments=[])
        result = ValidationResult()
        with patch("prototype.validator._validation_employees", return_value=[person]):
            _check_consecutive_off(shift, result, 3, {person.name: [1]})
        self.assertTrue(any(i.category == "3連休" for i in result.issues))

    def test_core_presence_warning_only_on_store_open_days(self):
        for mode, expected in ((OperationMode.CLOSED, False), (OperationMode.MINIMUM, False),
                               (OperationMode.NORMAL, True), (OperationMode.REDUCED, True)):
            shift = MonthlyShift(2026, 12, operation_modes={1: mode}, assignments=[
                ShiftAssignment(n, 1, Store.OFF) for n in ("長尾", "野澤")])
            result = ValidationResult()
            with patch("prototype.validator.fixed_suzuran_core_presence_rules",
                       return_value=[("長尾", "野澤", "自店主力")]):
                _check_monthly_avoid_same_off_rules(shift, result, 1)
            self.assertEqual(any(i.category == "すずらん主力不在" for i in result.issues), expected)

    def test_diagnostics_explain_auto_exemption_without_rewriting_requests(self):
        requested = {"今津": [29, 30], "下地": [30]}
        before = deepcopy(requested)
        findings = diagnose_known_conflicts(2026, 12, off_requests=requested,
            operation_modes={30: OperationMode.CLOSED, 31: OperationMode.CLOSED},
            employee_max_consecutive_off={"下地": 1},
            employees=[Employee(name="今津"), Employee(name="下地")])
        exemptions = [row for row in findings if row["区分"] == "全店休業を含む連休の自動免除"]
        self.assertEqual(len(exemptions), 2)
        self.assertTrue(all(row["確度"] == "情報" for row in exemptions))
        self.assertIn("今津: 29・30・31", str(exemptions))
        self.assertIn("下地: 30・31", str(exemptions))
        self.assertEqual(requested, before)

    def test_rule_book_overrides_old_description_and_keeps_monthly_settings(self):
        from prototype import rules
        from prototype.rule_book import build_month
        from prototype.rule_config import RuleConfig
        original = deepcopy(rules.load_monthly_exceptions_raw())
        ledger = {"rules": [{"分類": "絶対条件", "ルール": "3連休回避", "内容": "古い説明"}]}
        for month in (1, 12):
            book = build_month(2027, month, RuleConfig(), [], ledger)
            self.assertIn("全店休業", str(book))
            self.assertNotIn("古い説明", str(book))
        self.assertEqual(rules.load_monthly_exceptions_raw(), original)

    def test_restored_generation_details_show_closure_exception(self):
        from streamlit.testing.v1 import AppTest
        source = (Path(__file__).resolve().parents[1] / "app/app.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                    and n.name == "render_generation_metadata_summary")
        script = "import streamlit as st\n" + ast.get_source_segment(source, node)
        script += "\nrender_generation_metadata_summary({'message': '検証用', 'input_summary': {'closure_off_exceptions': [{'employee': '今津', 'days': [29,30,31], 'max_off_days': 2}]}})"
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        text = "\n".join(item.value for item in app.markdown)
        self.assertIn("今津: 29・30・31日", text)
        self.assertIn("提出希望の変更なし", text)


if __name__ == "__main__":
    unittest.main()
