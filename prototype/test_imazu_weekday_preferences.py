from __future__ import annotations

import ast
import json
import unittest
from calendar import monthrange
from datetime import date
from pathlib import Path

from ortools.sat.python import cp_model

from prototype.generator import _imazu_weekday_preference_terms, generate_shift
from prototype.models import MonthlyShift, OperationMode, ShiftAssignment, Store
from prototype.rules import (
    IMAZU_MONDAY_CATEGORY, IMAZU_MONDAY_DESCRIPTION, IMAZU_MONDAY_WEIGHT,
    IMAZU_WEEKEND_STORE, IMAZU_WEEKEND_CATEGORY, IMAZU_WEEKEND_DESCRIPTION,
    STORE_ASSIGNMENT_EXTRA_WEIGHTS, active_code_managed_monthly_rules,
    imazu_monday_preferred_days, imazu_weekend_preferred_days,
)
from prototype.shift_readjuster import new_protected_issues, validate_with_context
from prototype.validator import (
    ValidationResult, _check_imazu_monday_preference, _check_imazu_weekend_preference,
)


def make_shift(assignments, operation_modes=None):
    return MonthlyShift(2026, 10, assignments=[
        ShiftAssignment("今津", day, store) for day, store in assignments.items()
    ], operation_modes=operation_modes or {})


class ImazuWeekdayPreferenceTest(unittest.TestCase):
    def test_calendar_is_all_months_not_a_monthly_exception(self):
        for year in (2026, 2027, 2028):
            for month in range(1, 13):
                with self.subTest(year=year, month=month):
                    days = range(1, monthrange(year, month)[1] + 1)
                    self.assertEqual(imazu_monday_preferred_days(year, month), [
                        d for d in days if date(year, month, d).weekday() == 0
                    ])
                    self.assertEqual(imazu_weekend_preferred_days(year, month), [
                        d for d in days if date(year, month, d).weekday() in (5, 6)
                    ])

    def test_requested_off_and_closed_days_are_excluded(self):
        requests = {"今津": [3, 5], "春山": [4, 19]}
        modes = {10: OperationMode.CLOSED, 12: OperationMode.CLOSED}
        self.assertEqual(imazu_monday_preferred_days(2026, 10, requests, modes), [19, 26])
        self.assertEqual(imazu_weekend_preferred_days(2026, 10, requests, modes), [
            4, 11, 17, 18, 24, 25, 31,
        ])

    def test_monday_objective_is_strong_but_not_mandatory(self):
        self.assertGreater(IMAZU_MONDAY_WEIGHT, STORE_ASSIGNMENT_EXTRA_WEIGHTS[("今津", Store.AKABANE)])
        for forced_store in (None, Store.NISHIGUCHI, Store.OFF):
            with self.subTest(forced_store=forced_store):
                model = cp_model.CpModel()
                assignments = {s: model.NewBoolVar(s.value) for s in (Store.AKABANE, Store.NISHIGUCHI, Store.OFF)}
                model.Add(sum(assignments.values()) == 1)
                if forced_store:
                    model.Add(assignments[forced_store] == 1)
                terms = _imazu_weekday_preference_terms({"今津": {5: assignments}}, [5], Store.AKABANE)
                model.Maximize(IMAZU_MONDAY_WEIGHT * terms[5] + 10 * assignments[Store.NISHIGUCHI])
                solver = cp_model.CpSolver()
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual(solver.Value(assignments[forced_store or Store.AKABANE]), 1)

    def test_weekend_objective_prefers_akabane_without_increasing_workdays(self):
        for request_saturday_off in (False, True):
            with self.subTest(saturday_off=request_saturday_off):
                model = cp_model.CpModel()
                assignments = {
                    d: {s: model.NewBoolVar(f"store_{d}_{s.name}")
                        for s in (Store.AKABANE, Store.NISHIGUCHI, Store.OFF)}
                    for d in (2, 3, 4)
                }
                for choices in assignments.values():
                    model.Add(sum(choices.values()) == 1)
                model.Add(sum(1 - choices[Store.OFF] for choices in assignments.values()) == 2)
                requests = {"今津": [3]} if request_saturday_off else {}
                if request_saturday_off:
                    model.Add(assignments[3][Store.OFF] == 1)
                days = imazu_weekend_preferred_days(2026, 10, requests)
                terms = _imazu_weekday_preference_terms({"今津": assignments}, days, IMAZU_WEEKEND_STORE)
                model.Maximize(IMAZU_MONDAY_WEIGHT * sum(terms.values())
                               + 10 * sum(c[Store.NISHIGUCHI] for c in assignments.values()))
                solver = cp_model.CpSolver()
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual(sum(1 - solver.Value(c[Store.OFF]) for c in assignments.values()), 2)
                self.assertEqual(solver.Value(assignments[3][Store.OFF]), int(request_saturday_off))
                self.assertEqual(solver.Value(assignments[3][Store.AKABANE]), int(not request_saturday_off))
                self.assertEqual(solver.Value(assignments[4][Store.AKABANE]), 1)

    def test_weekend_other_store_is_allowed_but_does_not_satisfy_goal(self):
        for store in (Store.AKABANE, Store.NISHIGUCHI, Store.OMIYA, Store.OFF):
            for day in (3, 4):
                with self.subTest(store=store, day=day):
                    model = cp_model.CpModel()
                    choices = {s: model.NewBoolVar(s.name) for s in (Store.AKABANE, Store.NISHIGUCHI, Store.OMIYA, Store.OFF)}
                    model.Add(sum(choices.values()) == 1)
                    model.Add(choices[store] == 1)
                    terms = _imazu_weekday_preference_terms({"今津": {day: choices}}, [day], IMAZU_WEEKEND_STORE)
                    model.Maximize(IMAZU_MONDAY_WEIGHT * sum(terms.values()))
                    solver = cp_model.CpSolver()
                    self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                    self.assertEqual(solver.Value(terms[day]), int(store == Store.AKABANE))

    def test_missing_staff_or_blank_cells_are_not_scored(self):
        self.assertEqual(_imazu_weekday_preference_terms({}, [5], Store.AKABANE), {})
        result = ValidationResult()
        _check_imazu_monday_preference(make_shift({}), result)
        _check_imazu_weekend_preference(make_shift({}), result)
        self.assertEqual(result.issues, [])

    def test_monday_validation_is_warning_only_and_exempts_requested_off(self):
        shift = make_shift({5: Store.AKABANE, 6: Store.OFF, 12: Store.OFF,
                            19: Store.NISHIGUCHI, 26: Store.OFF})
        result = ValidationResult()
        _check_imazu_monday_preference(shift, result, {"今津": [12]})
        self.assertEqual([i.day for i in result.issues], [19, 26])
        self.assertTrue(all(i.category == IMAZU_MONDAY_CATEGORY and i.severity == "WARNING" for i in result.issues))

    def test_weekend_validation_requires_akabane_and_exempts_requested_off(self):
        shift = make_shift({3: Store.AKABANE, 4: Store.NISHIGUCHI, 10: Store.OFF,
                            11: Store.OFF, 17: Store.OFF, 18: Store.OFF},
                           {18: OperationMode.CLOSED})
        result = ValidationResult()
        _check_imazu_weekend_preference(shift, result, {"今津": [10]})
        self.assertEqual([i.day for i in result.issues], [4, 11, 17])
        self.assertTrue(all(i.category == IMAZU_WEEKEND_CATEGORY and i.severity == "WARNING" for i in result.issues))
        self.assertTrue(all("赤羽駅前店" in i.message for i in result.issues))
        self.assertIn(Store.NISHIGUCHI.display_name, result.issues[0].message)

    def test_weekend_other_store_to_off_does_not_add_a_new_goal_warning(self):
        before = ValidationResult()
        after = ValidationResult()
        _check_imazu_weekend_preference(make_shift({3: Store.NISHIGUCHI}), before)
        _check_imazu_weekend_preference(make_shift({3: Store.OFF}), after)
        self.assertEqual(new_protected_issues(before, after), [])

    def test_readjustment_validation_protects_already_satisfied_goals(self):
        before = make_shift({3: Store.AKABANE, 5: Store.AKABANE})
        after = make_shift({3: Store.NISHIGUCHI, 5: Store.OFF})
        old = validate_with_context(before)
        new = validate_with_context(after)
        introduced = new_protected_issues(old, new)
        for category in (IMAZU_MONDAY_CATEGORY, IMAZU_WEEKEND_CATEGORY):
            self.assertTrue(any(i.category == category for i in introduced))
        requests = {"off_requests": {"今津": [3, 5]}}
        exempt = new_protected_issues(validate_with_context(before, requests), validate_with_context(after, requests))
        self.assertFalse(any(i.category in (IMAZU_MONDAY_CATEGORY, IMAZU_WEEKEND_CATEGORY) for i in exempt))

    def test_ai_preview_reports_goal_regression_before_apply(self):
        from prototype.shift_chat import PendingShiftChange, ShiftChatEngine
        engine = ShiftChatEngine(make_shift({3: Store.AKABANE, 5: Store.AKABANE}), provider="local")
        engine.pending_changes = [PendingShiftChange("今津", 3, Store.NISHIGUCHI),
                                  PendingShiftChange("今津", 5, Store.OFF)]
        report = engine.inspect_pending_changes()
        self.assertTrue(any(i.category == IMAZU_MONDAY_CATEGORY for i in report.new_warnings))
        self.assertTrue(any(i.category == IMAZU_WEEKEND_CATEGORY for i in report.new_warnings))
        self.assertIn("反映を停止", engine.apply_pending_changes())
        self.assertEqual(engine.shift.get_assignment("今津", 3).store, Store.AKABANE)
        self.assertEqual(engine.shift.get_assignment("今津", 5).store, Store.AKABANE)
        self.assertEqual(engine.get_pending_change_count(), 2)


class ImazuWeekdayIntegrationTest(unittest.TestCase):
    def test_production_generator_respects_fixed_assignment_and_requested_off(self):
        from prototype.employees import shift_active_employees

        available = {"下地", "春山", "黒澤", "今津", "土井", "楯", "長尾", "板倉", "岩野", "野澤", "大類", "牧野"}
        for day, forced_store in ((5, Store.AKABANE), (5, Store.NISHIGUCHI),
                                  (17, Store.AKABANE), (17, Store.NISHIGUCHI), (17, Store.OFF),
                                  (18, Store.AKABANE), (18, Store.NISHIGUCHI), (18, Store.OFF)):
            with self.subTest(day=day, forced_store=forced_store):
                modes = {d: OperationMode.CLOSED for d in range(1, 32)}
                modes[day] = OperationMode.NORMAL
                # 月曜は東口休業。東口専属の土井を休み希望にしておく。
                available_today = available - ({"土井"} if day == 5 else set())
                off_requests = {
                    e.name: [d for d in modes if d != day or e.name not in available_today]
                    for e in shift_active_employees()
                }
                required = []
                if forced_store == Store.OFF:
                    off_requests["今津"].append(day)
                else:
                    required = [{"employee": "今津", "day": day, "store": forced_store, "severity": "ERROR"}]
                status = {}
                generated = generate_shift(
                    2026, 10, off_requests, [], [], operation_modes=modes,
                    disable_month_edge_rules=True, required_assignments=required,
                    strict_warning_constraints=False, time_limit_seconds=3,
                    verbose=False, status_out=status,
                )
                self.assertIsNotNone(generated, status)
                self.assertEqual(generated.get_assignment("今津", day).store, forced_store)
                monday, weekend = status["imazu_monday_preference"], status["imazu_weekend_preference"]
                self.assertEqual(monday["target_days"], [5] if day == 5 else [])
                self.assertEqual(monday["assigned_days"], [5] if day == 5 and forced_store == Store.AKABANE else [])
                self.assertEqual(weekend["target_days"], [day] if day in (17, 18) and forced_store != Store.OFF else [])
                self.assertEqual(weekend["assigned_days"], [day] if day in (17, 18) and forced_store == Store.AKABANE else [])

    def test_settings_ledger_and_generation_notes_share_the_same_descriptions(self):
        from prototype.shift_chat import SYSTEM_PROMPT

        root = Path(__file__).resolve().parents[1]
        path = root / "app" / "app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_effective_rule_visibility_rows"]
        scope = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
        ledger = json.loads((root / "config" / "rule_ledger_v1_0.json").read_text(encoding="utf-8"))
        for month in (1, 7, 8, 10):
            rows, _ = scope["build_effective_rule_visibility_rows"](2026, month, {})
            notes = active_code_managed_monthly_rules(2026, month)
            for category, description in ((IMAZU_MONDAY_CATEGORY, IMAZU_MONDAY_DESCRIPTION),
                                          (IMAZU_WEEKEND_CATEGORY, IMAZU_WEEKEND_DESCRIPTION)):
                with self.subTest(month=month, category=category):
                    row = next(r for r in rows if r["ルール"] == category)
                    self.assertEqual(row["現在有効な内容"], description)
                    self.assertEqual(row["適用範囲"], "全月固定")
                    self.assertIn("WARNING", row["強さ"])
                    self.assertTrue(any(description in note for note in notes))
                    self.assertIn(description, SYSTEM_PROMPT)
                    ledger_row = next(r for r in ledger["rules"] if r["ルール"] == category)
                    self.assertEqual(ledger_row["内容"], description)
        self.assertIn("赤羽駅前店で勤務", IMAZU_WEEKEND_DESCRIPTION)
        self.assertNotIn("店舗は指定しない", IMAZU_WEEKEND_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
