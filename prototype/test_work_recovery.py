from __future__ import annotations

import ast
import json
import unittest
from calendar import monthrange
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype.carryover import build_previous_month_carryover
from prototype.employees import get_employee
from prototype.models import MonthlyShift, OperationMode, PreviousMonthCarryover, ShiftAssignment, Store
from prototype.work_recovery import (
    CLOSE_LONG_WORK_CATEGORY, CLOSE_LONG_WORK_DESCRIPTION, CLOSE_LONG_WORK_PENALTY,
    add_close_long_work_indicators, close_long_work_rest_days, previous_working_map,
)
from prototype.validator import ValidationResult, _check_close_long_work, _check_store_capacity, validate
from prototype.shift_readjuster import new_protected_issues, validate_with_context
from prototype.rules import (
    STORE_STAFFING_LIMITS, STORE_OVERAGE_PRIORITY, active_code_managed_monthly_rules,
)


def work_map(pattern):
    return {day: char == "W" for day, char in enumerate(pattern, 1)}


def make_shift(pattern, year=2026, month=10, employee="田中"):
    return MonthlyShift(year=year, month=month, assignments=[
        ShiftAssignment(employee, day, Store.AKABANE if value else Store.OFF)
        for day, value in work_map(pattern).items()
    ])


class WorkRecoveryTest(unittest.TestCase):
    def test_generator_and_validator_share_definition(self):
        cases = {
            "WWWWWRWWWWW": [6],
            "WWWWWRWWWWWRWWWWW": [6, 12],
            "WWWWWWRWWWWWW": [7],
            "WWWWWRRWWWWW": [],
            "WWWWWRWWWW": [],
            "WWWWRWWWWW": [],
            "WWWWWWWWWWW": [],
            "WWWWWRRWWRRWWRRWWRRWWWWW": [],
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                current = work_map(pattern)
                self.assertEqual(close_long_work_rest_days(current, len(pattern)), expected)
                model = cp_model.CpModel()
                working = {d: model.NewBoolVar(f"work_{d}") for d in current}
                for day, value in current.items():
                    model.Add(working[day] == int(value))
                indicators = add_close_long_work_indicators(model, working, len(pattern), "test")
                model.Minimize(CLOSE_LONG_WORK_PENALTY * sum(indicators.values()))
                solver = cp_model.CpSolver()
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual([d for d, var in indicators.items() if solver.Value(var)], expected)
                result = ValidationResult()
                with patch("prototype.validator._validation_employees", return_value=[get_employee("田中")]):
                    _check_close_long_work(make_shift(pattern), result, len(pattern), [])
                self.assertEqual([i.day - 5 for i in result.issues], expected)
                self.assertTrue(all(i.severity == "WARNING" for i in result.issues))
                self.assertEqual(result.summary_stats[CLOSE_LONG_WORK_CATEGORY + "（件）"], len(expected))

    def test_objective_prefers_two_day_recovery_with_equal_workdays(self):
        bad, good = "WWWWWRWWWWWR", "WWWWWRRWWWWW"
        model = cp_model.CpModel()
        use_good = model.NewBoolVar("use_good")
        working = {d: model.NewBoolVar(f"work_{d}") for d in range(1, 13)}
        for day, var in working.items():
            model.Add(var == int(bad[day - 1] == "W")).OnlyEnforceIf(use_good.Not())
            model.Add(var == int(good[day - 1] == "W")).OnlyEnforceIf(use_good)
        model.Add(sum(working.values()) == 10)
        model.Add(working[6] == 0)  # 本人の絶対休みは維持
        indicators = add_close_long_work_indicators(model, working, 12, "test")
        model.Minimize(CLOSE_LONG_WORK_PENALTY * sum(indicators.values()))
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(solver.Value(use_good), 1)

    def test_month_boundary_uses_last_ten_days_including_off_day(self):
        # 前月末に休みを挟んでいても、その前の5連勤を読み取る。
        for year, month in ((2026, 10), (2027, 1), (2028, 3)):
            with self.subTest(year=year, month=month):
                py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
                last = monthrange(py, pm)[1]
                previous = make_shift("R" * (last - 6) + "WWWWWR", py, pm)
                with patch("prototype.carryover.shift_active_employees", return_value=[get_employee("田中")]):
                    carry = build_previous_month_carryover(previous)
                # JSON往復・従来の持ち越し属性も維持。
                carry = [PreviousMonthCarryover(**json.loads(json.dumps(asdict(carry[0]))))]
                self.assertEqual(carry[0].last_working_days, [])
                self.assertEqual(carry[0].last_off_days, [last])
                working = previous_working_map(carry, "田中", year, month)
                working.update(work_map("WWWWW"))
                self.assertEqual(close_long_work_rest_days(working, 5), [0])
                model = cp_model.CpModel()
                indicators = add_close_long_work_indicators(model, working, 5, "boundary")
                solver = cp_model.CpSolver()
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual(solver.Value(indicators[0]), 1)
                result = ValidationResult()
                with patch("prototype.validator._validation_employees", return_value=[get_employee("田中")]):
                    _check_close_long_work(make_shift("WWWWW", year, month), result, 5, carry)
                self.assertEqual(result.issues[0].day, 5)
                self.assertIn(f"{pm}/{last}休み", result.issues[0].message)

    def test_legacy_carryover_and_missing_records_are_not_guessed(self):
        legacy = [PreviousMonthCarryover("田中", [26, 27, 28, 29, 30], [])]
        working = previous_working_map(legacy, "田中", 2026, 10)
        working.update(work_map("RWWWWW"))
        self.assertEqual(close_long_work_rest_days(working, 6), [1])
        unknown = [PreviousMonthCarryover("田中", [], [30])]
        working = previous_working_map(unknown, "田中", 2026, 10)
        working.update(work_map("WWWWW"))
        self.assertEqual(close_long_work_rest_days(working, 5), [])
        previous = make_shift("R" * 25 + "WWWWW", 2026, 9)
        previous.assignments = [a for a in previous.assignments if a.day != 28]
        with patch("prototype.carryover.shift_active_employees", return_value=[]):
            carry = build_previous_month_carryover(previous)
        self.assertNotIn(-2, previous_working_map(carry, "田中", 2026, 10))

    def test_full_validation_and_readjustment_guard_agree(self):
        good = make_shift("WWWWWRRWWWWW")
        bad = make_shift("WWWWWRWWWWWR")
        with patch("prototype.validator._validation_employees", return_value=[get_employee("田中")]):
            before = validate(good)
            after = validate_with_context(bad)
        warning = [i for i in after.issues if i.category == CLOSE_LONG_WORK_CATEGORY]
        self.assertEqual(len(warning), 1)
        self.assertEqual(warning[0].month, 10)
        introduced = new_protected_issues(before, after)
        self.assertIn(warning[0], introduced)
        self.assertFalse(any(i.category == CLOSE_LONG_WORK_CATEGORY for i in new_protected_issues(after, after)))
        self.assertFalse(any(i.category == CLOSE_LONG_WORK_CATEGORY for i in new_protected_issues(after, before)))

    def test_auxiliary_and_advisor_are_not_added_to_normal_rule(self):
        for employee in ("山本", "顧問"):
            result = ValidationResult()
            with patch("prototype.validator._validation_employees", return_value=[get_employee(employee)]):
                _check_close_long_work(make_shift("WWWWWRWWWWW", employee=employee), result, 11, [])
            self.assertEqual(result.issues, [])

    def test_ai_preview_requires_acknowledgement_for_new_recovery_warning(self):
        from prototype.shift_chat import PendingShiftChange, ShiftChatEngine

        shift = make_shift("WWWWWRRWWWWW" + "RWW" * 6 + "R")
        engine = ShiftChatEngine(shift, provider="local")
        engine.pending_changes = [
            PendingShiftChange("田中", 7, Store.AKABANE),
            PendingShiftChange("田中", 12, Store.OFF),
        ]
        report = engine.inspect_pending_changes()
        self.assertTrue(any(i.category == CLOSE_LONG_WORK_CATEGORY for i in report.new_warnings))
        message = engine.apply_pending_changes()
        self.assertIn("反映を停止", message)
        self.assertEqual(engine.shift.get_assignment("田中", 7).store, Store.OFF)
        self.assertEqual(engine.get_pending_change_count(), 2)


class OmiyaLimitIntegrationTest(unittest.TestCase):
    def test_ai_cannot_apply_four_person_preview_even_if_warnings_are_allowed(self):
        from prototype.shift_chat import PendingShiftChange, ShiftChatEngine

        shift = MonthlyShift(2026, 10, assignments=[
            ShiftAssignment(n, 1, Store.OMIYA) for n in ("下地", "春山", "黒澤")
        ] + [ShiftAssignment("大類", 1, Store.SUZURAN)])
        engine = ShiftChatEngine(shift, provider="local")
        engine.pending_changes = [PendingShiftChange("大類", 1, Store.OMIYA)]
        report = engine.inspect_pending_changes()
        self.assertTrue(any(i.category == "店舗人数上限" for i in report.new_errors))
        self.assertIn("反映を停止", engine.apply_pending_changes(allow_new_warnings=True))
        self.assertEqual(engine.shift.get_assignment("大類", 1).store, Store.SUZURAN)

    def test_validator_and_ai_reject_four_not_three(self):
        def shift(names):
            return MonthlyShift(2026, 10, assignments=[ShiftAssignment(n, 1, Store.OMIYA) for n in names])
        results = []
        for names in (["下地", "春山", "黒澤"], ["下地", "春山", "黒澤", "大類"]):
            result = ValidationResult()
            _check_store_capacity(shift(names), result, 1, allow_omiya_short=True)
            results.append(result)
        self.assertFalse(any(i.category == "店舗人数上限" for i in results[0].issues))
        limit_issues = [i for i in results[1].issues if i.category == "店舗人数上限"]
        self.assertEqual(len(limit_issues), 1)
        self.assertEqual(limit_issues[0].severity, "ERROR")
        self.assertIn("最大3名", limit_issues[0].message)
        self.assertIn(limit_issues[0], new_protected_issues(*results))
        self.assertNotIn(Store.OMIYA, STORE_OVERAGE_PRIORITY)

    def test_production_generator_enforces_three_person_max(self):
        from prototype.generator import generate_shift
        from prototype.employees import shift_active_employees

        # 1営業日の月に絞り、実際の生成処理で3名成立・4名不可を確認する。
        modes = {d: OperationMode.CLOSED for d in range(1, 32)}
        modes[15] = OperationMode.NORMAL
        employees = shift_active_employees()
        available = {"下地", "春山", "黒澤", "今津", "土井", "楯", "長尾", "板倉", "岩野", "野澤", "大類", "牧野"}
        for names, expected in ((["下地", "春山", "黒澤"], True),
                                (["下地", "春山", "黒澤", "大類"], False)):
            with self.subTest(names=names):
                status = {}
                generated = generate_shift(
                    2026, 10,
                    {e.name: [d for d in modes if d != 15 or e.name not in available] for e in employees}, [], [],
                    operation_modes=modes, disable_month_edge_rules=True,
                    required_assignments=[
                        {"employee": n, "day": 15, "store": Store.OMIYA, "severity": "ERROR"}
                        for n in names
                    ],
                    strict_warning_constraints=False, time_limit_seconds=3,
                    verbose=False, status_out=status,
                )
                self.assertEqual(generated is not None, expected, status)
                self.assertEqual(status["omiya_staffing_rule"]["max_total"], 3)
                if generated:
                    self.assertEqual(sum(a.store == Store.OMIYA for a in generated.get_day_assignments(15)), 3)
                    self.assertEqual(status["close_long_work_count"], 0)
                else:
                    self.assertEqual(status["status"], "INFEASIBLE")

    def test_rule_consistency_and_supply_calculation_still_agree(self):
        from prototype.rule_consistency import _check_store_capacity as check_config
        from prototype.capacity_balance import compute_monthly_capacity_balance
        issues = []
        check_config(issues)
        self.assertFalse(any(i.severity == "ERROR" for i in issues), issues)
        # 上限だけの変更。標準人数ベースの需要計算は変えない。
        result = compute_monthly_capacity_balance(2026, 8, [], {})
        self.assertEqual(result["demand_total"], 31 * 11 - 5)


class WorkRecoveryDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"build_numeric_ledger_rows_from_parameters", "build_effective_rule_visibility_rows"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        cls.scope = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), cls.scope)

    def test_settings_ledger_generation_and_ai_explain_same_rule(self):
        from prototype.shift_chat import SYSTEM_PROMPT
        numbers = self.scope["build_numeric_ledger_rows_from_parameters"]({})
        maximum = next(row["現在値"] for row in numbers if row["項目"] == "店舗最大人数")
        self.assertIn("大宮3", maximum)
        self.assertNotIn("大宮4", maximum)
        rows, _ = self.scope["build_effective_rule_visibility_rows"](2026, 10, {})
        recovery = next(row for row in rows if row["ルール"] == "5連勤の近接回避")
        self.assertEqual(recovery["現在有効な内容"], CLOSE_LONG_WORK_DESCRIPTION)
        self.assertIn("WARNING", recovery["強さ"])
        self.assertEqual(recovery["適用範囲"], "全月固定")
        notes = active_code_managed_monthly_rules(2026, 10)
        self.assertTrue(any(CLOSE_LONG_WORK_DESCRIPTION in note for note in notes))
        self.assertTrue(any("最大人数は3名" in note for note in notes))
        self.assertIn(CLOSE_LONG_WORK_DESCRIPTION, SYSTEM_PROMPT)
        self.assertIn("最大人数は3名", SYSTEM_PROMPT)
        path = Path(__file__).resolve().parents[1] / "config" / "rule_ledger_v1_0.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("大宮4名まで", json.dumps(ledger, ensure_ascii=False))
        self.assertNotIn("3.大宮駅前", json.dumps(ledger, ensure_ascii=False))
        self.assertTrue(any(r["ルール"] == "5連勤の近接回避" for r in ledger["rules"]))
        self.assertEqual(STORE_STAFFING_LIMITS[Store.OMIYA].max_total, 3)


if __name__ == "__main__":
    unittest.main()
