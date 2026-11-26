import tempfile
from dataclasses import replace
import unittest
from pathlib import Path
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.employees import shift_active_employees
from prototype.generator import generate_shift
from prototype.models import MonthlyShift, ShiftAssignment, Store, OperationMode
from prototype.rule_book import monthly_cards
from prototype.shift_readjuster import new_protected_issues
from prototype.staffing_events import (
    APPROVED_STAFFING_EVENTS, STAFFING_EVENT_CATEGORY, STAFFING_EVENT_REWARD,
    staffing_events, event_satisfied, add_staffing_event_goals,
)
from prototype.validator import (
    ValidationResult, _check_staffing_events, _check_store_capacity, _check_yamamoto_monthly_max,
)


EVENT = APPROVED_STAFFING_EVENTS[0]


class StaffingEventTests(unittest.TestCase):
    def setUp(self):
        self.disabled_patch = patch.object(rules, "DISABLED_STAFFING_EVENTS", frozenset())
        self.disabled_patch.start()
        self.addCleanup(self.disabled_patch.stop)

    def shift(self, names, month=11, day=5):
        return MonthlyShift(year=2026, month=month, assignments=[
            ShiftAssignment(name, day, Store.AKABANE) for name in names])

    def test_only_approved_month_and_disabled_state(self):
        for year, month in ((2026, 10), (2026, 12), (2027, 11)):
            self.assertEqual(staffing_events(year, month), ())
        self.assertEqual(staffing_events(2026, 11), (EVENT,))
        with patch.object(rules, "DISABLED_STAFFING_EVENTS", {EVENT.id}):
            self.assertEqual(staffing_events(2026, 11), ())
            self.assertEqual(staffing_events(2026, 11, include_disabled=True), (EVENT,))

    def test_all_conditions_together_and_either_candidate(self):
        for names, expected in (
            (("鈴木", "山本", "田中", "板倉"), True),
            (("鈴木", "山本", "大類", "板倉"), True),
            (("鈴木", "山本", "田中", "大類"), True),
            (("鈴木", "山本", "板倉", "岩野"), False),
            (("鈴木", "山本", "田中"), False),
            (("鈴木", "田中", "岩野", "板倉"), False),
            (("鈴木", "山本", "田中", "板倉", "岩野"), False),
        ):
            shift = self.shift(names)
            self.assertEqual(event_satisfied(shift, EVENT), expected)
            result = ValidationResult()
            _check_staffing_events(shift, result)
            self.assertEqual(len(result.issues), 0 if expected else 1)
            if not expected:
                self.assertEqual(result.issues[0].severity, "WARNING")
                self.assertEqual(result.issues[0].day, 5)

    def test_goal_is_soft_and_requires_full_combination(self):
        for names, off, mode, yamamoto, expected in (
            (("鈴木", "田中", "板倉"), {}, OperationMode.NORMAL, True, 1),
            (("鈴木", "大類", "板倉"), {}, OperationMode.NORMAL, True, 1),
            (("鈴木", "田中", "板倉"), {"山本": [5]}, OperationMode.NORMAL, True, 0),
            (("鈴木", "田中", "板倉"), {}, OperationMode.REDUCED, True, 0),
            (("鈴木", "田中", "板倉"), {}, OperationMode.NORMAL, False, 0),
            (("田中", "岩野", "板倉"), {}, OperationMode.NORMAL, True, 0),
            (("鈴木", "岩野", "板倉"), {}, OperationMode.NORMAL, True, 0),
            (("鈴木", "田中"), {}, OperationMode.NORMAL, True, 0),
        ):
            model = cp_model.CpModel()
            x = {name: {5: {Store.AKABANE: model.NewBoolVar(name)}}
                 for name in ("鈴木", "田中", "大類", "板倉", "岩野")}
            for name in x:
                model.Add(x[name][5][Store.AKABANE] == int(name in names))
            goals, extras = add_staffing_event_goals(
                model, x, 2026, 11, off, {5: mode}, yamamoto_available=yamamoto)
            model.Maximize(STAFFING_EVENT_REWARD * sum(goals.values()))
            solver = cp_model.CpSolver()
            self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
            self.assertEqual(solver.Value(goals[EVENT.id]), expected)
            if expected:
                self.assertEqual(solver.Value(extras[5]), 1)

    def test_planned_four_is_not_conflicting_overstaff_warning(self):
        for day, names, expected_warning in (
            (5, ("鈴木", "山本", "田中", "板倉"), False),
            (5, ("鈴木", "山本", "岩野", "板倉"), True),
            (6, ("鈴木", "山本", "田中", "板倉"), True),
        ):
            result = ValidationResult()
            _check_store_capacity(self.shift(names, day=day), result, day, allow_omiya_short=True)
            overstaff = [i for i in result.issues if i.category == "店舗人数多め" and i.day == day
                         and "赤羽駅前" in i.message]
            self.assertEqual(bool(overstaff), expected_warning)

    def test_readjustment_detects_loss_of_completed_goal(self):
        before, after = ValidationResult(), ValidationResult()
        _check_staffing_events(self.shift(("鈴木", "山本", "田中", "板倉")), before)
        _check_staffing_events(self.shift(("鈴木", "田中", "板倉")), after)
        self.assertEqual(new_protected_issues(before, after)[0].category, STAFFING_EVENT_CATEGORY)

    def test_book_and_management_share_description_and_scope(self):
        cards = monthly_cards(2026, 11)
        card = next(c for c in cards if c["id"] == "monthly:event:" + EVENT.id)
        self.assertEqual(card["text"], EVENT.description)
        self.assertEqual(card["scope"], "2026-11")
        self.assertEqual(card["strength"], "目標")
        self.assertIn(EVENT.description, rules.active_monthly_exception_descriptions(2026, 11))
        self.assertNotIn(EVENT.description, rules.active_monthly_exception_descriptions(2026, 12))

    def test_disable_persists_across_reload_and_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rules, "MONTHLY_EXCEPTIONS_FILE", Path(directory) / "monthly.json"):
            data = {"disabled_staffing_events": [EVENT.id],
                    "operation_modes": {"2026-12": {"3": "省人員"}}}
            self.assertTrue(rules.save_monthly_exceptions(data)[0])
            rules.reload_monthly_exceptions()
            self.assertEqual(staffing_events(2026, 11), ())
            self.assertEqual(rules.load_monthly_exceptions_raw()["operation_modes"], data["operation_modes"])
        rules.reload_monthly_exceptions()

    def generate_fixture(self, *, off=False, maximum=15, neighboring_shortages=False):
        open_days = (4, 5, 6) if neighboring_shortages else (5,)
        modes = {day: OperationMode.NORMAL if day in open_days else OperationMode.CLOSED
                 for day in range(1, 31)}
        base = {"鈴木": Store.AKABANE, "田中": Store.AKABANE, "板倉": Store.AKABANE,
                "土井": Store.HIGASHIGUCHI, "楯": Store.NISHIGUCHI,
                "春山": Store.OMIYA, "大類": Store.OMIYA, "黒澤": Store.OMIYA,
                "長尾": Store.SUZURAN, "野澤": Store.SUZURAN, "岩野": Store.SUZURAN}
        assigned = {(name, day): store for day in open_days for name, store in base.items()
                    if not (neighboring_shortages and name == "田中" and day != 5)}
        staff = shift_active_employees(2026, 11)
        requests = {e.name: [day for day in range(1, 31) if (e.name, day) not in assigned]
                    for e in staff if e.name != "山本"}
        requests["山本"] = [5] if off else []
        required = [{"employee": name, "day": day, "store": store, "severity": "ERROR"}
                    for (name, day), store in assigned.items()]
        diagnostics = {}
        with patch.dict(rules.MONTHLY_YAMAMOTO_POLICIES, {
            (2026, 11): {"max_days": maximum, "max_consecutive": 2},
        }, clear=True):
            shift = generate_shift(2026, 11, requests, [], [], operation_modes=modes,
                required_assignments=required, disable_month_edge_rules=True,
                strict_warning_constraints=False, time_limit_seconds=5,
                verbose=False, status_out=diagnostics)
            self.assertIsNotNone(shift, diagnostics)
            result = ValidationResult()
            _check_yamamoto_monthly_max(shift, result)
            self.assertEqual(result.issues, [])
        for (name, day), store in assigned.items():
            self.assertEqual(shift.get_assignment(name, day).store, store)
        return shift, diagnostics

    def test_real_generator_adds_yamamoto_only_on_requested_event(self):
        shift, info = self.generate_fixture()
        self.assertTrue(event_satisfied(shift, EVENT))
        self.assertTrue(info["staffing_event_goals"][EVENT.id])
        self.assertEqual([a.day for a in shift.assignments if a.employee == "山本" and a.store != Store.OFF], [5])

    def test_real_generator_respects_changed_staffing_caps_and_disabled_goal(self):
        akabane_limit = replace(rules.STORE_STAFFING_LIMITS[Store.AKABANE], max_total=3)
        daily_limit = replace(rules.GLOBAL_DAILY_STAFFING_LIMIT, max_total=11)
        for context in (
            patch.dict(rules.STORE_STAFFING_LIMITS, {Store.AKABANE: akabane_limit}),
            patch("prototype.generator.GLOBAL_DAILY_STAFFING_LIMIT", daily_limit),
            patch.object(rules, "DISABLED_STAFFING_EVENTS", {EVENT.id}),
        ):
            with context:
                shift, info = self.generate_fixture()
                self.assertFalse(event_satisfied(shift, EVENT))
                self.assertFalse(info.get("staffing_event_goals", {}).get(EVENT.id))
                self.assertFalse(any(a.employee == "山本" and a.store != Store.OFF for a in shift.assignments))

    def test_real_generator_preserves_leave_monthly_and_consecutive_caps(self):
        for args in ({"off": True}, {"maximum": 0}, {"neighboring_shortages": True}):
            shift, info = self.generate_fixture(**args)
            self.assertFalse(event_satisfied(shift, EVENT))
            self.assertFalse(info["staffing_event_goals"][EVENT.id])
            if args.get("off"):
                self.assertEqual(shift.get_assignment("山本", 5).store, Store.OFF)


if __name__ == "__main__":
    unittest.main()
