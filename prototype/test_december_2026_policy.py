"""12月追加条件・保存設定の維持・月境界を、実ソルバーと表示経路で確認する。"""
from copy import deepcopy
from datetime import datetime
from itertools import product
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.backup import ShiftBackup
from prototype.eco_support import solo_exception_allowed
from prototype.employees import shift_active_employees
from prototype.generator import generate_shift, _add_eco_support_pairing_constraints
from prototype.models import Employee, MonthlyShift, ShiftAssignment, Store, Skill, OperationMode, PreviousMonthCarryover
from prototype.preferred_off import submitted_flexible_off
from prototype.rule_book import monthly_cards, submission_cards
from prototype.staffing_policy import (
    ADVISOR_MAX_DAYS, AttendanceEvent, add_suzuran_backup_constraint, suzuran_backup_violation,
    attendance_events, add_attendance_goals, DECEMBER_SUPPORT_DESCRIPTION,
)
from prototype.submission_loader import SubmissionData, load_submissions_for_month
from prototype.validator import (
    ValidationResult, _check_tobishi_work_patterns, _check_eco_support_pairing,
    _check_staffing_policy, _check_yamamoto_monthly_max,
)


class DecemberPolicyTest(unittest.TestCase):
    def setUp(self):
        self.disabled = patch.object(rules, "DISABLED_STAFFING_EVENTS", frozenset())
        self.disabled.start()
        self.addCleanup(self.disabled.stop)

    def test_suzuran_generator_and_validator_agree_for_all_combinations(self):
        for first, second in product((Store.OFF, Store.AKABANE, Store.SUZURAN), repeat=2):
            with self.subTest(first=first, second=second):
                model = cp_model.CpModel()
                x, off = {}, {}
                for name, store in (("長尾", first), ("鈴木", second)):
                    x[name] = {1: {Store.SUZURAN: model.NewBoolVar(name)}}
                    off[name] = {1: model.NewBoolVar(name + "off")}
                    model.Add(x[name][1][Store.SUZURAN] == int(store == Store.SUZURAN))
                    model.Add(off[name][1] == int(store == Store.OFF))
                add_suzuran_backup_constraint(model, x, off, [1], {})
                self.assertEqual(cp_model.CpSolver().Solve(model) == cp_model.INFEASIBLE,
                                 suzuran_backup_violation(first, second))

    def test_suzuran_closed_store_is_exempt(self):
        for mode in (OperationMode.CLOSED, OperationMode.MINIMUM):
            model = cp_model.CpModel()
            add_suzuran_backup_constraint(model,
                {n: {1: {Store.SUZURAN: 0}} for n in ("長尾", "鈴木")},
                {n: {1: 0} for n in ("長尾", "鈴木")}, [1], {1: mode})
            self.assertEqual(cp_model.CpSolver().Solve(model), cp_model.OPTIMAL)

    def test_support_month_scope_and_matching_generator_validation(self):
        for year, month in ((2026, 11), (2026, 12), (2027, 1), (2027, 12)):
            for name in ("大類", "田中"):
                support = Employee(name=name, skill=Skill.ECO_SUPPORT)
                allowed = not ((year, month) == (2026, 12) and name == "大類")
                self.assertEqual(solo_exception_allowed(support, Store.SUZURAN, year, month), allowed)
                self.assertFalse(solo_exception_allowed(support, Store.OMIYA, year, month))
                model = cp_model.CpModel()
                variable = model.NewBoolVar("solo")
                _add_eco_support_pairing_constraints(model, {name: {1: {Store.SUZURAN: variable}}},
                    [1], [Store.SUZURAN], [], [support], allow_shortage_exceptions=True, year=year, month=month)
                model.Add(variable == 1)
                self.assertEqual(cp_model.CpSolver().Solve(model) != cp_model.INFEASIBLE, allowed)
                result = ValidationResult()
                with patch("prototype.validator.get_employee", return_value=support):
                    _check_eco_support_pairing(MonthlyShift(year, month, assignments=[
                        ShiftAssignment(name, 1, Store.SUZURAN)]), result, 1)
                self.assertEqual(result.issues[0].severity, "WARNING" if allowed else "ERROR")

    def test_advisor_default_and_manual_maximum(self):
        self.assertEqual(inspect.signature(generate_shift).parameters["advisor_max_days"].default, 3)
        for count in (0, 1, 3, 4):
            shift = MonthlyShift(2027, 1, assignments=[ShiftAssignment("顧問", d, Store.AKABANE)
                                                       for d in range(1, count + 1)])
            result = ValidationResult()
            _check_staffing_policy(shift, result, 31, {})
            self.assertEqual(any(i.category == "顧問の月間出勤上限" for i in result.issues), count > 3)

    def test_event_scoped_and_disabled_without_changing_saved_settings(self):
        original = deepcopy(rules.load_monthly_exceptions_raw())
        event = attendance_events(2026, 12)[0]
        self.assertEqual(event.employees, ("山本", "板倉", "今津", "鈴木", "春山", "土井", "南"))
        for ym in ((2026, 11), (2027, 1), (2027, 12)):
            self.assertFalse(attendance_events(*ym))
            self.assertNotIn(DECEMBER_SUPPORT_DESCRIPTION, rules.active_monthly_exception_descriptions(*ym))
        cards = monthly_cards(2026, 12)
        self.assertIn(event.description, [card["text"] for card in cards])
        self.assertIn(DECEMBER_SUPPORT_DESCRIPTION, [card["text"] for card in cards])
        with patch.object(rules, "DISABLED_STAFFING_EVENTS", {event.id}):
            self.assertFalse(attendance_events(2026, 12))
            self.assertTrue(attendance_events(2026, 12, include_disabled=True))
        self.assertEqual(rules.load_monthly_exceptions_raw(), original)

    def test_event_does_not_force_requested_leave_or_yamamoto_when_full(self):
        for requested_off, main_count, expected_yama in (({}, 3, 1), ({"山本": [26], "南": [26]}, 3, 0), ({}, 4, 0)):
            model = cp_model.CpModel()
            off = {n: {26: model.NewBoolVar(n + "off")} for n in AttendanceEvent().employees if n != "山本"}
            for name in off:
                if name in requested_off:
                    model.Add(off[name][26] == 1)
            x = {str(i): {26: {Store.AKABANE: 1}} for i in range(main_count)}
            goals, extra = add_attendance_goals(model, off, x, 2026, 12, requested_off, {}, {26: 0}, yamamoto_available=True)
            model.Maximize(sum(goals))
            solver = cp_model.CpSolver()
            self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
            self.assertEqual(solver.Value(extra[26]) if extra else 0, expected_yama)
            if "南" in requested_off:
                self.assertEqual(solver.Value(off["南"][26]), 1)

    def test_boundary_checks_year_change_missing_and_requested_off(self):
        employee = Employee(name="今津", is_eco_core=True)
        for year, month, prev_last in ((2026, 12, 30), (2027, 1, 31), (2028, 3, 29)):
            for pattern, expected in (((False, True, False, False), "前月最終日"),
                                      ((False, False, True, False), f"{month}/1"),
                                      ((False, False, True, True), None)):
                carry = PreviousMonthCarryover("今津", [], [],
                    recent_working_days=[prev_last - 1 + i for i in range(2) if pattern[i]],
                    recent_off_days=[prev_last - 1 + i for i in range(2) if not pattern[i]])
                shift = MonthlyShift(year, month, assignments=[ShiftAssignment("今津", d, Store.AKABANE if pattern[d + 1] else Store.OFF)
                                                               for d in (1, 2)])
                result = ValidationResult()
                with patch("prototype.validator._validation_employees", return_value=[employee]):
                    _check_tobishi_work_patterns(shift, result, 2, [carry])
                boundary = [i for i in result.issues if i.category == "月境界の飛び石勤務"]
                self.assertEqual(len(boundary), int(expected is not None))
                if expected:
                    self.assertIn(expected, boundary[0].message)
                    self.assertEqual(boundary[0].severity, "WARNING")
        shift = MonthlyShift(2026, 12, assignments=[ShiftAssignment("今津", 1, Store.AKABANE), ShiftAssignment("今津", 2, Store.OFF)])
        for carry in ([], [PreviousMonthCarryover("今津", [], [])]):
            result = ValidationResult()
            with patch("prototype.validator._validation_employees", return_value=[employee]):
                _check_tobishi_work_patterns(shift, result, 2, carry)
            self.assertFalse(result.issues)

    def test_triangle_is_soft_explicit_group_is_preserved(self):
        data = {"author": "下地", "monthly_target_workdays": 22, "base_holidays": 9,
                "flexible_off": [{"employee": "下地", "candidate_days": [8, 9, 10], "n_required": 1}]}
        self.assertEqual(list(submitted_flexible_off(data)), [("下地", [8, 9, 10], 0)])
        data["author"] = "system"
        self.assertEqual(list(submitted_flexible_off(data)), [("下地", [8, 9, 10], 1)])
        sub = SubmissionData(year=2026, month=12)
        sub.flexible_off = [("下地", [8], 0), ("下地", [12, 13, 14], 1)]
        text = submission_cards(sub, [Employee(name="下地")])[0]["text"]
        self.assertIn("△できれば休み：8日", text)
        self.assertNotIn("のうち0日", text)
        self.assertIn("のうち1日", text)

    def test_loader_keeps_calendar_and_free_text_conditions_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ShiftBackup(Path(directory))
            with patch("prototype.backup.now_jst", return_value=datetime.fromisoformat("2026-11-26T12:00:00+09:00")):
                path = manager.save_preferences(2026, 12, {}, [], [("下地", [8], 0)],
                    {"下地": "12日・13日・14日のうち1日休み希望"}, author="下地", triangle_off_days=[8])
            with patch("prototype.submission_loader.BACKUP_DIR", Path(directory)), \
                 patch("prototype.submission_loader.PROJECT_ROOT", Path(directory)), \
                 patch("prototype.submission_loader._load_latest_note_adjustments", return_value={}):
                sub = load_submissions_for_month(2026, 12, expected_employees=["下地"], sync_remote=False)
            self.assertIn(("下地", [8], 0), sub.flexible_off)
            self.assertIn(("下地", [12, 13, 14], 1), sub.flexible_off)
            self.assertEqual(json.loads(path.read_text())["triangle_off_days"], [8])


class DecemberGenerationTest(unittest.TestCase):
    def fixture(self, open_days, *, event=True, yama_max=15, mode=OperationMode.NORMAL, yama_off=False,
                advisor_limit=ADVISOR_MAX_DAYS, shortage=False, flexible_off=None):
        base = {"今津": Store.AKABANE, "板倉": Store.AKABANE,
                "土井": Store.HIGASHIGUCHI, "楯": Store.NISHIGUCHI,
                "春山": Store.OMIYA, "大類": Store.OMIYA, "黒澤": Store.OMIYA,
                "長尾": Store.SUZURAN, "鈴木": Store.SUZURAN, "岩野": Store.SUZURAN}
        if not shortage:
            base["田中"] = Store.AKABANE
        staff = shift_active_employees(2026, 12)
        modes = {d: mode if d in open_days else OperationMode.CLOSED for d in range(1, 32)}
        requests = {e.name: [d for d in modes if d not in open_days or e.name not in base]
                    for e in staff if e.name not in ("顧問", "山本")}
        requests["山本"] = list(open_days) if yama_off else []
        required = [{"employee": n, "day": d, "store": s, "severity": "ERROR"} for d in open_days for n, s in base.items()]
        diagnostics = {}
        with patch.object(rules, "DISABLED_STAFFING_EVENTS", set() if event else {AttendanceEvent().id}), \
             patch.dict(rules.MONTHLY_YAMAMOTO_POLICIES, {(2026, 12): {"max_days": yama_max, "max_consecutive": 2}}, clear=True):
            shift = generate_shift(2026, 12, requests, [], [], required_assignments=required,
                flexible_off=flexible_off,
                operation_modes=modes, disable_month_edge_rules=True, strict_warning_constraints=False,
                advisor_max_days=advisor_limit, time_limit_seconds=5, verbose=False, status_out=diagnostics)
        if shift:
            for n, days in requests.items():
                for d in days:
                    self.assertEqual(shift.get_assignment(n, d).store, Store.OFF)
            for r in required:
                self.assertEqual(shift.get_assignment(r["employee"], r["day"]).store, r["store"])
        return shift, diagnostics

    def test_actual_generator_event_respects_leave_limit_and_disabled(self):
        for args, expected in (({}, [26]), ({"yama_off": True}, []), ({"yama_max": 0}, []), ({"event": False}, [])):
            with self.subTest(args=args):
                shift, info = self.fixture([26], **args)
                self.assertIsNotNone(shift, info)
                self.assertEqual([a.day for a in shift.assignments if a.employee == "山本" and a.store != Store.OFF], expected)

    def test_advisor_only_when_needed_and_up_to_three(self):
        shift, info = self.fixture([2, 9, 16], shortage=True, yama_off=True)
        self.assertIsNotNone(shift, info)
        self.assertEqual([a.day for a in shift.assignments if a.employee == "顧問" and a.store != Store.OFF], [2, 9, 16])
        shift, info = self.fixture([2, 9, 16, 23], shortage=True, yama_off=True)
        self.assertIsNone(shift)
        self.assertEqual(info["status"], "INFEASIBLE")
        shift, info = self.fixture([2], yama_off=True)
        self.assertIsNotNone(shift, info)
        self.assertFalse(any(a.employee == "顧問" and a.store != Store.OFF for a in shift.assignments))

    def test_reduced_mode_yamamoto_no_unbounded_postprocessing(self):
        shift, info = self.fixture([25, 26, 27], shortage=True, mode=OperationMode.REDUCED, yama_max=0,
                                   advisor_limit=3)
        self.assertIsNotNone(shift, info)
        self.assertFalse(any(a.employee == "山本" and a.store != Store.OFF for a in shift.assignments))

    def test_triangle_remains_optional_and_explicit_count_remains_hard(self):
        shift, info = self.fixture([8], flexible_off=[("今津", [8], 0)])
        self.assertIsNotNone(shift, info)
        self.assertEqual(shift.get_assignment("今津", 8).store, Store.AKABANE)
        self.assertEqual(info["triangle_off_preferences"], 1)
        shift, info = self.fixture([8], flexible_off=[("今津", [8], 1)])
        self.assertIsNone(shift)
        self.assertEqual(info["status"], "INFEASIBLE")


if __name__ == "__main__":
    unittest.main()
