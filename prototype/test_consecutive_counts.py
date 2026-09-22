from __future__ import annotations

import ast
import json
import tempfile
import unittest
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype.consecutive_counts import (
    add_consecutive_count_constraints, consecutive_count_label,
    previous_run_length,
)
from prototype.models import MonthlyShift, PreviousMonthCarryover, ShiftAssignment, Store
from prototype.submission_loader import (
    SubmissionData, _apply_parsed_note_to_submission_data,
    load_submissions_for_month, parse_natural_language_note,
)
from prototype.validator import ValidationResult, _check_consecutive_counts, validate
from prototype.shift_readjuster import new_protected_issues, validate_with_context
from prototype.shift_chat import ShiftChatEngine


def rule(kind="off", days=2, count=1, comparison="exact", employee="田中"):
    return dict(kind=kind, days=days, count=count, comparison=comparison, employee=employee)


def make_shift(off_days, year=2026, month=10):
    return MonthlyShift(year=year, month=month, assignments=[
        ShiftAssignment("田中", day, Store.OFF if day in off_days else Store.AKABANE)
        for day in range(1, monthrange(year, month)[1] + 1)
    ])


class ConsecutiveCountParsingTest(unittest.TestCase):
    def test_original_and_wording_variants(self):
        for text in (
            "2連休1回のみ\n5連勤1回のみ",
            "2連休を1回のみ。5連勤を1回のみ",
            "２連休を１回だけ、５連勤は月に１回",
            "二連休を一回だけ、五連勤を一回のみ",
            "2連休は月内ちょうど1回でお願いします。5連勤を月に1回にしてください",
            "2連休 月1回希望です\n5連勤 月間1回希望します",
            "2連休は1回のみです。5連勤も1回だけを希望します",
            "月に1回だけの2連休を希望します。月1回のみの5連勤を希望します",
            "2連休を1回にしてほしいです。5連勤も1回にしてほしいです",
        ):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(parsed.consecutive_count_rules, [
                    {k: v for k, v in rule().items() if k != "employee"},
                    {k: v for k, v in rule("work", 5).items() if k != "employee"},
                ])
                self.assertIsNone(parsed.preferred_consecutive_off_days)
                self.assertEqual(parsed.off_requests, [])
                self.assertEqual(parsed.work_requests, [])
                self.assertEqual(parsed.review_messages, [])

    def test_upper_and_lower_bounds_are_distinct(self):
        for phrase, expected in (
            ("1回まで", "max"), ("最大1回", "max"), ("1回以内", "max"),
            ("1回以下", "max"), ("多くても1回", "max"),
            ("最低1回", "min"), ("1回以上", "min"), ("少なくとも1回", "min"),
        ):
            with self.subTest(phrase=phrase):
                p = parse_natural_language_note(f"5連勤は{phrase}", 2026, 10)
                self.assertEqual(p.consecutive_count_rules[0]["comparison"], expected)
                self.assertEqual(p.review_messages, [])

    def test_unread_count_is_reviewed_and_not_a_preference(self):
        for text in (
            "2連休は1〜2回程度", "できれば2連休1回", "2連休は1回もいらない",
            "2連休は1回以上は不可", "2連休は最大1回以上", "2連休1回だけは避けたい",
        ):
            with self.subTest(text=text):
                p = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(p.consecutive_count_rules, [])
                self.assertIsNone(p.preferred_consecutive_off_days)
                self.assertTrue(p.review_messages)

    def test_contradictory_exact_counts_are_not_silently_overwritten(self):
        p = parse_natural_language_note("2連休は1回のみ。2連休は2回のみ", 2026, 10)
        self.assertEqual(p.consecutive_count_rules, [])
        self.assertTrue(p.review_messages)

    def test_other_supported_conditions_survive(self):
        p = parse_natural_language_note(
            "2連休を1回のみ 有給1日。合計9日休み希望。5日は休み希望。最大5連勤まで許容。",
            2026, 10,
        )
        self.assertEqual(p.paid_leave_days, 1)
        self.assertEqual(p.requested_holiday_days, 9)
        self.assertEqual(p.off_requests, [5])
        self.assertEqual(p.max_consecutive_work_days, 5)
        self.assertEqual(len(p.consecutive_count_rules), 1)

    def test_plain_preference_keeps_previous_behavior(self):
        p = parse_natural_language_note("2連休希望", 2026, 10)
        self.assertEqual(p.preferred_consecutive_off_days, 2)
        self.assertEqual(p.consecutive_count_rules, [])

    def test_correction_replaces_only_the_matching_count(self):
        data = SubmissionData(2026, 10)
        for text in ("2連休1回のみ。5連勤1回のみ", "2連休は2回のみ"):
            _apply_parsed_note_to_submission_data(
                data, "田中", parse_natural_language_note(text, 2026, 10),
            )
        self.assertEqual(data.consecutive_count_rules, [rule("work", 5), rule(count=2)])
        self.assertEqual(data.preferred_consecutive_off, [])

    def test_exact_correction_removes_the_old_preference(self):
        data = SubmissionData(2026, 10)
        for text in ("2連休希望", "2連休1回のみ"):
            _apply_parsed_note_to_submission_data(
                data, "田中", parse_natural_language_note(text, 2026, 10),
            )
        self.assertEqual(data.preferred_consecutive_off, [])
        self.assertIsNone(data.parsed_note_summaries["田中"]["preferred_consecutive_off_days"])

    def test_loader_statuses_and_month_isolation(self):
        original = "2連休1回のみ\n5連勤1回のみ"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            month_dir = root / "backups" / "2026-10"
            month_dir.mkdir(parents=True)
            (month_dir / "preferences_test.json").write_text(json.dumps({
                "author": "田中", "saved_at": "2026-09-23T10:00:00+09:00",
                "off_requests": {"田中": [8]}, "paid_leave_days": 1,
                "natural_language_notes": {"田中": original},
            }, ensure_ascii=False), encoding="utf-8")
            with patch("prototype.submission_loader.BACKUP_DIR", root / "backups"), \
                 patch("prototype.submission_loader.PROJECT_ROOT", root), \
                 patch("prototype.github_backup.sync_preferences_from_github"), \
                 patch("prototype.submission_loader.is_submission_in_window", return_value=True):
                for status, counts in (
                    ("", [rule(), rule("work", 5)]),
                    ("要確認", [rule(), rule("work", 5)]),
                    ("確認済み", [rule("work", 5), rule(count=2)]),
                    ("補正のみ反映", [rule(count=2)]),
                    ("反映しない", []),
                ):
                    with self.subTest(status=status), patch(
                        "prototype.submission_loader._load_latest_note_adjustments",
                        return_value={"田中": {"status": status, "corrected_text": "2連休2回のみ"}},
                    ):
                        data = load_submissions_for_month(2026, 10, ["田中"])
                        self.assertEqual(data.consecutive_count_rules, counts)
                        self.assertEqual(data.off_requests, {"田中": [8]})
                        self.assertEqual(data.natural_language_notes["田中"], original)
                        self.assertEqual(data.paid_leave_days, {"田中": 1})
                        self.assertEqual(data.preferred_consecutive_off, [])
                self.assertEqual(load_submissions_for_month(2026, 11, ["田中"]).consecutive_count_rules, [])


class ConsecutiveCountEngineTest(unittest.TestCase):
    def solve_fixed(self, shift, rules, previous=None):
        model = cp_model.CpModel()
        days = monthrange(shift.year, shift.month)[1]
        off = {"田中": {d: model.NewBoolVar(f"off_{d}") for d in range(1, days + 1)}}
        for day, variable in off["田中"].items():
            model.Add(variable == int(shift.get_assignment("田中", day).store == Store.OFF))
        add_consecutive_count_constraints(model, off, rules, shift.year, shift.month, previous)
        result = ValidationResult()
        _check_consecutive_counts(shift, result, rules, previous)
        status = cp_model.CpSolver().Solve(model)
        feasible = status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
        self.assertEqual(feasible, not result.has_errors)
        return feasible, result

    def test_exact_counts_are_enforced_in_both_directions(self):
        for off_days, expected in (({3, 4}, True), ({3}, False), ({3, 4, 9, 10}, False)):
            with self.subTest(off_days=off_days):
                self.assertEqual(self.solve_fixed(make_shift(off_days), [rule()])[0], expected)

    def test_five_day_run_not_a_sliding_window_inside_six(self):
        for work_days, expected in ((set(range(1, 6)), True), (set(range(1, 7)), False)):
            off_days = set(range(1, 32)) - work_days
            self.assertEqual(self.solve_fixed(make_shift(off_days), [rule("work", 5)])[0], expected)

    def test_long_holiday_is_not_two_or_more_two_day_breaks(self):
        self.assertFalse(self.solve_fixed(make_shift({3, 4, 5}), [rule()])[0])

    def test_upper_and_lower_count_constraints(self):
        for comparison, count, off_days, expected in (
            ("max", 1, {3}, True), ("max", 1, {3, 4, 8, 9}, False),
            ("min", 1, {3}, False), ("min", 1, {3, 4, 8, 9}, True),
            ("exact", 0, {3}, True), ("exact", 0, {3, 4}, False),
        ):
            with self.subTest(comparison=comparison, count=count, off_days=off_days):
                self.assertEqual(self.solve_fixed(make_shift(off_days), [rule(count=count, comparison=comparison)])[0], expected)

    def test_both_month_edges_are_counted(self):
        self.assertTrue(self.solve_fixed(make_shift({1, 2, 30, 31}), [rule(count=2)])[0])

    def test_carryover_counts_a_whole_work_run_once(self):
        previous = [PreviousMonthCarryover("田中", [29, 30], [28])]
        for days_worked, expected in ((3, True), (5, False)):
            shift = make_shift(set(range(days_worked + 1, 32)))
            self.assertEqual(self.solve_fixed(shift, [rule("work", 5)], previous)[0], expected)

    def test_carryover_off_and_january_boundary(self):
        previous = [PreviousMonthCarryover("田中", [], [31])]
        self.assertEqual(previous_run_length(previous, 2027, 1, "田中", "off"), 1)
        shift = make_shift({1}, 2027, 1)
        self.assertTrue(self.solve_fixed(shift, [rule()], previous)[0])

    def test_solver_constructs_both_requested_counts(self):
        model = cp_model.CpModel()
        off = {"田中": {d: model.NewBoolVar(f"off_{d}") for d in range(1, 32)}}
        rules = [rule(), rule("work", 5)]
        add_consecutive_count_constraints(model, off, rules, 2026, 10)
        model.Add(sum(off["田中"].values()) == 8)
        for day in (8, 16):
            model.Add(off["田中"][day] == 1)
        for day in range(1, 27):
            model.Add(sum(off["田中"][d] for d in range(day, day + 6)) >= 1)
        for day in range(1, 30):
            model.Add(sum(off["田中"][d] for d in range(day, day + 3)) <= 2)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5
        self.assertIn(solver.Solve(model), (cp_model.FEASIBLE, cp_model.OPTIMAL))
        off_days = {d for d, variable in off["田中"].items() if solver.Value(variable)}
        shift = make_shift(off_days)
        self.assertTrue({8, 16}.issubset(off_days))
        self.assertEqual(len(off_days), 8)
        self.assertTrue(self.solve_fixed(shift, rules)[0])

    def test_hard_off_requests_are_not_relaxed_for_count(self):
        # 2連休が2つ確定している場合、「1回のみ」との両立は不可。
        shift = make_shift({3, 4, 10, 11})
        self.assertFalse(self.solve_fixed(shift, [rule()])[0])

    def test_public_validator_and_readjustment_receive_counts(self):
        shift = make_shift({3, 4, 10, 11})
        context = {"consecutive_count_rules": [rule()]}
        for result in (validate(shift, **context), validate_with_context(shift, context)):
            issues = [i for i in result.issues if i.category == "連休回数"]
            self.assertEqual(len(issues), 1)
            self.assertIn("実際2回", issues[0].message)
            self.assertEqual(issues[0].severity, "ERROR")

    def test_chat_rejects_breaking_the_requested_count(self):
        shift = make_shift({3, 4})
        engine = ShiftChatEngine(shift, provider="local", validation_inputs={"consecutive_count_rules": [rule()]})
        result = engine._validate_shift_with_context(make_shift({3, 4, 10, 11}))
        self.assertTrue(any(i.category == "連休回数" for i in result.issues))

    def test_existing_count_error_does_not_hide_a_different_count_error(self):
        rules = [rule(), rule(days=3)]
        before, after = ValidationResult(), ValidationResult()
        _check_consecutive_counts(make_shift({3, 4, 5}), before, rules)
        _check_consecutive_counts(make_shift({3, 4}), after, rules)
        self.assertEqual(before.error_count, after.error_count)
        self.assertEqual(len(new_protected_issues(before, after)), 1)


class ConsecutiveCountDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Streamlit起動や本番データ保存をせず、画面の純粋な表示関数だけを実行する。
        app_path = Path(__file__).resolve().parents[1] / "app" / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        names = {"summarize_natural_language_note_for_review", "_unique_label_list",
                 "build_note_reflection_review", "parsed_note_summary_to_labels",
                 "get_validation_context_for_shift", "save_validation_context",
                 "restore_validation_context_for_month", "build_generation_metadata"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        scope = {"re": __import__("re"), "Store": Store, "consecutive_count_label": consecutive_count_label}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(app_path), "exec"), scope)
        cls.scope = scope

    def test_restore_reloads_counts_and_keeps_months_separate(self):
        data = SubmissionData(2026, 10)
        _apply_parsed_note_to_submission_data(
            data, "田中", parse_natural_language_note("2連休1回のみ\n5連勤1回のみ", 2026, 10),
        )
        scope = self.scope
        st = SimpleNamespace(session_state={})
        with patch.dict(scope, {
            "st": st, "monthrange": monthrange,
            "shift_submission_employee_names": lambda: ["田中"],
            "system_monthly_preferred_work_requests": lambda *_: [],
            "combined_paid_leave_days": lambda paid, *_: paid,
            "load_locked_previous_month_carryover": lambda *_: SimpleNamespace(carryover=[]),
            "active_monthly_store_count_rules": lambda *_: [],
            "active_monthly_required_assignment_rules": lambda *_: [],
        }), patch("prototype.submission_loader.load_submissions_for_month", return_value=data):
            restored = scope["restore_validation_context_for_month"](2026, 10, SimpleNamespace(parameters={}))
            self.assertEqual(restored["consecutive_count_rules"], [rule(), rule("work", 5)])
            context = scope["get_validation_context_for_shift"](make_shift({3, 4}))
            self.assertEqual(context["consecutive_count_rules"], restored["consecutive_count_rules"])
            next_month = scope["get_validation_context_for_shift"](make_shift({3, 4}, 2026, 11))
            self.assertEqual(next_month["consecutive_count_rules"], [])

    def test_generation_metadata_preserves_counts_in_json(self):
        counts = [rule(), rule("work", 5)]
        with patch.dict(self.scope, {"now_jst": lambda: datetime(2026, 9, 23, tzinfo=timezone.utc)}):
            metadata = self.scope["build_generation_metadata"]("test", {
                "year": 2026, "month": 10, "consecutive_count_rules": counts,
            })
        restored = json.loads(json.dumps(metadata, ensure_ascii=False))
        self.assertEqual(restored["input_summary"]["consecutive_count_rules"], counts)

    def test_display_shows_counts_instead_of_preference(self):
        result = self.scope["summarize_natural_language_note_for_review"]("2連休1回のみ\n5連勤1回のみ", 2026, 10)
        self.assertEqual(result["status"], "反映済み")
        self.assertEqual(result["auto_labels"], [consecutive_count_label(rule()), consecutive_count_label(rule("work", 5))])

    def test_count_limit_is_not_an_ambiguous_date_range(self):
        result = self.scope["summarize_natural_language_note_for_review"]("5連勤は1回まで", 2026, 10)
        self.assertEqual(result["status"], "反映済み")

    def test_unread_part_is_visible_even_when_paid_leave_was_read(self):
        result = self.scope["summarize_natural_language_note_for_review"]("2連休は1-2回程度。有給1日", 2026, 10)
        self.assertEqual(result["status"], "要確認")
        self.assertTrue(any("回数指定" in label for label in result["review_labels"]))
        self.assertEqual(result["auto_labels"], ["希望有給日数: 1日"])

    def test_review_and_actual_conditions_agree_after_correction(self):
        original, correction = "2連休1回のみ。5連勤1回のみ", "2連休2回のみ"
        data = SubmissionData(2026, 10)
        for text in (original, correction):
            _apply_parsed_note_to_submission_data(data, "田中", parse_natural_language_note(text, 2026, 10))
        reflection = self.scope["build_note_reflection_review"](
            original, {"status": "確認済み", "corrected_text": correction}, 2026, 10,
        )
        actual = self.scope["parsed_note_summary_to_labels"](data.parsed_note_summaries["田中"])
        self.assertEqual(reflection["final_labels"], actual)
        self.assertEqual(actual, [consecutive_count_label(rule("work", 5)), consecutive_count_label(rule(count=2))])


if __name__ == "__main__":
    unittest.main()
