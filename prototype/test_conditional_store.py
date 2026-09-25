from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from calendar import monthrange
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype.conditional_store import (
    conditional_store_labels, conditional_store_mismatches, conditional_store_penalties,
    normalize_conditional_store_requests,
)
from prototype.consecutive_counts import consecutive_count_label
from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.note_interpreter import validate_note_interpretation
from prototype.submission_loader import (
    SubmissionData, _apply_parsed_note_to_submission_data, load_submissions_for_month,
    parse_natural_language_note, preview_note_adjustment,
)
from prototype.test_note_interpreter import condition, display_source
from prototype.validator import ValidationResult, _check_conditional_store_requests


ORIGINAL = "5か6どちらか休み希望 出勤の場合5.6は赤羽希望"
EXPECTED = [(5, Store.AKABANE), (6, Store.AKABANE)]
APP = Path(__file__).resolve().parents[1] / "app" / "app.py"


def app_scope(*names):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = dict(re=re, Store=Store, json=json, monthrange=monthrange,
                 MonthlyShift=MonthlyShift, ShiftAssignment=ShiftAssignment,
                 consecutive_count_label=consecutive_count_label)
    exec("from __future__ import annotations\n" + display_source(), scope)
    exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=nodes, type_ignores=[])), scope)
    return scope


class ConditionalStoreParsingTest(unittest.TestCase):
    def test_reported_text_and_common_variants(self):
        for text in (
            ORIGINAL,
            "５か６どちらか休み希望 出勤の場合５．６は赤羽希望",
            "5日か6日のどちらか1日休み希望。5日・6日は出勤する場合は赤羽駅前店希望。",
            "5か6どちらか休み希望\n出勤の場合5.6は赤羽希望",
            "5日か6日のどちらか休み希望で、出勤になった場合には赤羽駅前店を希望",
            "5か6どちらか休み希望。出勤の場合は赤羽希望",
            "5か6どちらか休み希望。5日・6日は勤務なら赤羽希望",
        ):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(parsed.flexible_off, [([5, 6], 1)])
                self.assertEqual(parsed.conditional_store_requests, EXPECTED)
                self.assertEqual(parsed.work_requests, [])
                self.assertEqual(parsed.work_groups, [])
                self.assertEqual(parsed.off_requests, [])
                self.assertEqual(parsed.review_messages, [])

    def test_single_date_conditional_and_ordinary_work_stay_distinct(self):
        parsed = parse_natural_language_note("2日 出勤の場合、赤羽出勤希望。有給2日利用で合計8日休み希望。", 2026, 10)
        self.assertEqual(parsed.conditional_store_requests, [(2, Store.AKABANE)])
        self.assertEqual(parsed.work_requests, [])
        self.assertEqual(parsed.requested_holiday_days, 8)
        self.assertEqual(parsed.paid_leave_days, 2)
        parsed = parse_natural_language_note(ORIGINAL + "。7日は大宮出勤希望。", 2026, 10)
        self.assertEqual(parsed.work_requests, [(7, Store.OMIYA)])
        self.assertEqual(parsed.conditional_store_requests, EXPECTED)

    def test_ambiguous_or_invalid_conditional_is_not_an_attendance_request(self):
        for text in (
            "5日は出勤の場合赤羽以外希望", "5日は出勤の場合赤羽か大宮希望",
            "出勤の場合赤羽希望", "11月5日は出勤の場合赤羽希望",
            "32日は出勤の場合赤羽希望", "5日は出勤の場合未定",
            "5日は出勤の場合赤羽希望。5日は出勤の場合大宮希望。",
        ):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(parsed.work_requests, [])
                self.assertEqual(parsed.conditional_store_requests, [])
                self.assertTrue(parsed.review_messages)

    def test_correction_modes_and_absolute_off_are_preserved(self):
        correction = "6日は出勤する場合は大宮駅前店希望。"
        for status, stores, choices in (
            ("確認済み", [(5, "AKABANE"), (6, "OMIYA")], True),
            ("補正のみ反映", [(6, "OMIYA")], False),
            ("要確認", [(5, "AKABANE"), (6, "AKABANE")], True),
            ("反映しない", [], False),
        ):
            with self.subTest(status=status):
                summary = preview_note_adjustment(ORIGINAL, dict(status=status, corrected_text=correction), 2026, 10)
                self.assertEqual([(i["day"], i["store"]) for i in summary.get("conditional_store_requests", [])], stores)
                self.assertEqual(bool(summary.get("flexible_off")), choices)
                self.assertFalse(summary.get("work_requests"))
        summary = preview_note_adjustment(ORIGINAL, {}, 2026, 10, off_days=[5])
        self.assertEqual(summary["conditional_store_requests"], [{"day": 6, "store": "AKABANE"}])

    def test_ai_round_trip_and_reject_attendance_upgrade(self):
        payload = dict(conditions=[
            condition("choice_off", ORIGINAL, days=[5, 6], value=1),
            condition("conditional_store", ORIGINAL, days=[5, 6], store="AKABANE"),
        ], review_messages=[])
        interpretation = validate_note_interpretation(payload, ORIGINAL, 2026, 10)
        parsed = parse_natural_language_note(interpretation.corrected_text, 2026, 10)
        self.assertEqual(parsed.conditional_store_requests, EXPECTED)
        self.assertEqual(parsed.work_requests, [])
        self.assertEqual(parsed.flexible_off, [([5, 6], 1)])
        payload["conditions"][1]["kind"] = "work_dates"
        with self.assertRaisesRegex(ValueError, "出勤希望に変わって"):
            validate_note_interpretation(payload, ORIGINAL, 2026, 10)


class ConditionalStoreSolverTest(unittest.TestCase):
    def model(self):
        model = cp_model.CpModel()
        stores = [Store.AKABANE, Store.OMIYA]
        x = {"岩野": {d: {s: model.NewBoolVar(f"{d}_{s.name}") for s in stores} for d in (5, 6)}}
        off = {d: model.NewBoolVar(f"off_{d}") for d in (5, 6)}
        for d in (5, 6):
            model.Add(sum(x["岩野"][d].values()) + off[d] == 1)
        model.Add(sum(off.values()) >= 1)
        penalty = sum(conditional_store_penalties(x, [("岩野", d, s) for d, s in EXPECTED], stores))
        model.Maximize(-penalty)
        return model, x, off

    def test_off_and_requested_store_have_equal_score_and_other_store_has_cost(self):
        for store, expected_score in ((Store.OFF, 0), (Store.AKABANE, 0), (Store.OMIYA, -130)):
            with self.subTest(store=store):
                model, x, off = self.model()
                model.Add(off[5] == 1)
                model.Add((off[6] if store == Store.OFF else x["岩野"][6][store]) == 1)
                solver = cp_model.CpSolver()
                self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
                self.assertEqual(solver.ObjectiveValue(), expected_score)

    def test_choice_off_is_hard_but_store_is_a_preference(self):
        model, x, off = self.model()
        model.Add(sum(off.values()) == 1)
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(sum(solver.Value(x["岩野"][d][Store.AKABANE]) for d in (5, 6)), 1)
        model.Add(sum(off.values()) == 0)
        self.assertEqual(solver.Solve(model), cp_model.INFEASIBLE)

    def test_serialized_requests_and_absolute_off_filter(self):
        requests = [["岩野", 5, "AKABANE"], ["岩野", 6, "AKABANE"], ["岩野", 32, "AKABANE"]]
        self.assertEqual(normalize_conditional_store_requests(requests, 2026, 10, {"岩野": [5]}),
                         [("岩野", 6, Store.AKABANE)])

    def test_validation_only_reports_work_at_another_store(self):
        requests = [("岩野", d, Store.AKABANE) for d in (5, 6)]
        for store, count in ((Store.OFF, 0), (Store.AKABANE, 0), (Store.OMIYA, 1)):
            shift = MonthlyShift(2026, 10, [ShiftAssignment("岩野", 5, Store.OFF), ShiftAssignment("岩野", 6, store)])
            result = ValidationResult()
            _check_conditional_store_requests(shift, result, requests, {})
            self.assertEqual(len(result.issues), count)
            self.assertEqual(result.error_count, 0)
        self.assertEqual(conditional_store_mismatches(shift, requests, {"岩野": [6]}), [])


class ConditionalStorePipelineTest(unittest.TestCase):
    def test_manager_screen_shows_conditional_preference_without_work_request(self):
        from streamlit.testing.v1 import AppTest
        script = '''from __future__ import annotations
import re
import streamlit as st
from prototype.models import Store
from prototype.consecutive_counts import consecutive_count_label
from prototype.submission_loader import preview_note_adjustment
from prototype.submission_review_ui import build_review_rows, render_submission_review
''' + display_source() + f'''
note = {ORIGINAL!r}
summary = preview_note_adjustment(note, {{}}, 2026, 10)
rows, details = build_review_rows(
    ["岩野"], [dict(employee="岩野", note=note, off_request_days=[14,15,27,31],
                    flexible_off_days=[5,6], work_request_days=[], paid_leave_days=2)],
    {{}}, {{"岩野": summary}}, 2026, 10, build_note_reflection_review,
    parsed_note_summary_to_labels, lambda days: "、".join(map(str, days)) or "なし",
)
render_submission_review(rows, details, 2026, 10, lambda *args: None)
'''
        at = AppTest.from_string(script).run()
        self.assertEqual(len(at.exception), 0)
        row = at.dataframe[0].value.iloc[0]
        self.assertEqual(row["出勤希望"], "なし")
        self.assertEqual(row["確認"], "確認事項なし")
        self.assertIn("出勤する場合の店舗希望: 5日、6日は赤羽駅前店", row["生成に使う自由記載条件"])

    def test_loader_and_visible_submission_table_do_not_create_work_days(self):
        scope = app_scope("enrich_submission_days_from_files", "_extract_preference_days", "_extract_marked_day_preferences", "_safe_preference_days")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            month_dir = root / "backups" / "2026-10"
            month_dir.mkdir(parents=True)
            filename = "preferences_test.json"
            (month_dir / filename).write_text(json.dumps({
                "author": "岩野", "saved_at": "2026-09-26T09:00:00+09:00",
                "off_requests": {"岩野": [14, 15, 27, 31]}, "paid_leave_days": 2,
                "flexible_off": [["岩野", [5, 6], 1]], "work_requests": [],
                "natural_language_notes": {"岩野": ORIGINAL},
            }, ensure_ascii=False), encoding="utf-8")
            with patch("prototype.submission_loader.BACKUP_DIR", root / "backups"), \
                 patch("prototype.submission_loader.PROJECT_ROOT", root), \
                 patch("prototype.submission_loader._load_latest_note_adjustments", return_value={}), \
                 patch("prototype.github_backup.sync_preferences_from_github"), \
                 patch("prototype.submission_loader.is_submission_in_window", return_value=True):
                data = load_submissions_for_month(2026, 10, ["岩野"])
            self.assertEqual(data.conditional_store_requests, [("岩野", d, s) for d, s in EXPECTED])
            self.assertEqual(data.preferred_work_requests, [])
            self.assertEqual(data.work_requests, [])
            status = scope["enrich_submission_days_from_files"](SimpleNamespace(backup_dir=root / "backups"), 2026, 10,
                        {"submitted": [dict(employee="岩野", file=filename)]})
            self.assertEqual(status["submitted"][0]["work_request_days"], [])
            labels = scope["parsed_note_summary_to_labels"](data.parsed_note_summaries["岩野"])
            self.assertIn(conditional_store_labels(EXPECTED)[0], labels)
            self.assertFalse(any(label.startswith("出勤希望:") for label in labels))
            self.assertEqual(scope["summarize_natural_language_note_for_review"](ORIGINAL, 2026, 10)["auto_labels"], labels)

    def test_restoration_keeps_conditional_requests_and_months_separate(self):
        scope = app_scope("restore_validation_context_for_month", "save_validation_context", "get_validation_context_for_shift")
        data = SubmissionData(2026, 10)
        _apply_parsed_note_to_submission_data(data, "岩野", parse_natural_language_note(ORIGINAL, 2026, 10))
        scope.update(st=SimpleNamespace(session_state={}), shift_submission_employee_names=lambda: ["岩野"],
                     system_monthly_preferred_work_requests=lambda *_: [], combined_paid_leave_days=lambda *_: {},
                     load_locked_previous_month_carryover=lambda *_: SimpleNamespace(carryover=[]),
                     active_monthly_store_count_rules=lambda *_: [], active_monthly_required_assignment_rules=lambda *_: [])
        with patch("prototype.submission_loader.load_submissions_for_month", return_value=data):
            restored = scope["restore_validation_context_for_month"](2026, 10, SimpleNamespace(parameters={}))
        self.assertEqual(restored["conditional_store_requests"], data.conditional_store_requests)
        self.assertEqual(restored["preferred_work_requests"], [])
        self.assertEqual(scope["get_validation_context_for_shift"](MonthlyShift(2026, 10))["conditional_store_requests"], data.conditional_store_requests)
        self.assertEqual(scope["get_validation_context_for_shift"](MonthlyShift(2026, 11))["conditional_store_requests"], [])

    def test_readjustment_and_ai_use_same_validation_context(self):
        from prototype.shift_readjuster import _validation_kwargs
        from prototype.shift_chat import ShiftChatEngine
        context = {"conditional_store_requests": [("岩野", 6, Store.AKABANE)]}
        self.assertEqual(_validation_kwargs(context, 5)["conditional_store_requests"], context["conditional_store_requests"])
        engine = ShiftChatEngine.__new__(ShiftChatEngine)
        engine.validation_inputs = context
        # 検証呼出しの引数だけを確認し、実シフトやAPIには触れない。
        engine.max_consec = 5
        with patch("prototype.shift_chat.validate", return_value=ValidationResult()) as validate_mock:
            engine._validate_shift_with_context(MonthlyShift(2026, 10))
        self.assertEqual(validate_mock.call_args.kwargs["conditional_store_requests"], context["conditional_store_requests"])

    def test_incomplete_draft_does_not_assign_conditional_work(self):
        scope = app_scope("build_incomplete_manual_draft", "store_from_rule_value")
        data = SubmissionData(2026, 10)
        _apply_parsed_note_to_submission_data(data, "岩野", parse_natural_language_note(ORIGINAL, 2026, 10))
        with patch("prototype.rules.month_edge_forced_assignments", return_value=([], [])):
            draft = scope["build_incomplete_manual_draft"](2026, 10, {}, {}, data.work_requests, data.preferred_work_requests, [])
        self.assertIsNone(draft.get_assignment("岩野", 5))
        self.assertIsNone(draft.get_assignment("岩野", 6))


if __name__ == "__main__":
    unittest.main()
