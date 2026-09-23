from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from prototype.models import Store
from prototype.note_interpreter import (
    NoteInterpretation, interpret_note, validate_note_interpretation,
)
from prototype.submission_loader import (
    _load_latest_note_adjustments, load_submissions_for_month,
    parse_natural_language_note, preview_note_adjustment,
)


ORIGINAL = "五連勤可能 二連休憩不可 1.2.3.4日全て出勤"
CORRECTION = "連勤上限: 5連勤まで / 連休上限: 1連休まで\n1,2,3,4日は出勤"


def condition(kind, evidence="原文", **kwargs):
    return dict(dict(kind=kind, days=[], value=None, store=None, length=None,
                     comparison=None, evidence=evidence), **kwargs)


def display_source():
    path = Path(__file__).resolve().parents[1] / "app" / "app.py"
    names = {"summarize_natural_language_note_for_review", "_unique_label_list",
             "build_note_reflection_review", "parsed_note_summary_to_labels",
             "format_note_adjustment_status", "render_note_adjustment_editor"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    return "\n".join(ast.unparse(node) for node in functions)


class NoteParsingTest(unittest.TestCase):
    def test_reported_original_and_correction(self):
        for text in (ORIGINAL, CORRECTION):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(parsed.max_consecutive_work_days, 5)
                self.assertEqual(parsed.max_consecutive_off_days, 1)
                self.assertEqual(parsed.work_requests, [(d, None) for d in range(1, 5)])
                self.assertIsNone(parsed.requested_holiday_days)

    def test_date_lists_and_widths(self):
        for text in (
            "１,２，3、４日は出勤", "1日・2日・3日・4日は勤務希望",
            "1.2.3.4日全て出勤", "1,2,3,4日勤務希望", "10月1日,2日,3日,4日は出勤",
        ):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 10)
                self.assertEqual(parsed.work_requests, [(d, None) for d in range(1, 5)])
                self.assertIsNone(parsed.requested_holiday_days)

    def test_other_months_and_negative_permissions_are_not_work(self):
        for text in ("11月2日は出勤", "11月1,2,3,4日は出勤", "4日は出勤不可",
                     "4日は出勤しない", "18日は出勤となってもOK"):
            with self.subTest(text=text):
                self.assertEqual(parse_natural_language_note(text, 2026, 10).work_requests, [])

    def test_off_and_work_sentences_do_not_pollute_each_other(self):
        for text in ("5日休み。6日出勤希望", "5日休み、6日は出勤希望", "5日休み 6日は出勤"):
            p = parse_natural_language_note(text, 2026, 10)
            self.assertEqual(p.off_requests, [5])
            self.assertEqual(p.work_requests, [(6, None)])
            self.assertIsNone(p.requested_holiday_days)

    def test_direct_store_and_choice_preserved(self):
        p = parse_natural_language_note("4日は大宮駅前に出勤", 2026, 10)
        self.assertEqual(p.work_requests, [(4, Store.OMIYA)])
        p = parse_natural_language_note("1日・2日・3日・4日のどれか1日出勤希望", 2026, 10)
        self.assertEqual(p.work_requests, [])
        self.assertEqual(p.work_groups, [([1, 2, 3, 4], 1, None)])

    def test_total_work_days_stay_counts(self):
        for text in ("合計12日勤務希望です。", "12日間勤務希望", "12日勤務希望"):
            p = parse_natural_language_note(text, 2026, 10)
            self.assertEqual(p.requested_holiday_days, 19)
            self.assertEqual(p.work_requests, [])

    def test_preview_and_real_loader_use_same_correction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            month_dir = root / "backups" / "2026-10"
            month_dir.mkdir(parents=True)
            (month_dir / "preferences_test.json").write_text(json.dumps({
                "author": "田中", "saved_at": "2026-09-23T10:00:00+09:00",
                "off_requests": {"田中": [8]}, "paid_leave_days": 1,
                "natural_language_notes": {"田中": "最大5連勤まで許容。休み合計9日。"},
            }, ensure_ascii=False), encoding="utf-8")
            with patch("prototype.submission_loader.BACKUP_DIR", root / "backups"), \
                 patch("prototype.submission_loader.PROJECT_ROOT", root), \
                 patch("prototype.github_backup.sync_preferences_from_github"), \
                 patch("prototype.submission_loader.is_submission_in_window", return_value=True):
                for status, expected_days, limit, holidays in (
                    ("確認済み", [1, 2, 3, 4], 5, 10),
                    ("補正のみ反映", [1, 2, 3, 4], None, 10),
                    ("要確認", [], 5, 9), ("反映しない", [], None, None),
                ):
                    adjustment = {"status": status, "corrected_text": "1,2,3,4日は出勤。休み合計10日。"}
                    with self.subTest(status=status), patch(
                        "prototype.submission_loader._load_latest_note_adjustments", return_value={"田中": adjustment},
                    ):
                        data = load_submissions_for_month(2026, 10, ["田中"])
                    self.assertEqual(data.off_requests, {"田中": [8]})
                    self.assertEqual(data.preferred_work_requests, [("田中", d, None) for d in expected_days])
                    self.assertEqual(data.max_consecutive_work_days.get("田中"), limit)
                    self.assertEqual(data.requested_holiday_days.get("田中"), holidays)
                    actual = data.parsed_note_summaries.get("田中", {}).copy()
                    preview = preview_note_adjustment("最大5連勤まで許容。休み合計9日。", adjustment, 2026, 10)
                    actual.pop("sources", None)
                    preview.pop("sources", None)
                    self.assertEqual(actual, preview)

    def test_disk_save_reload_delete_and_month_isolation(self):
        path = Path(__file__).resolve().parents[1] / "app" / "app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"load_note_adjustment_data", "save_note_adjustment_data", "upsert_note_adjustment", "delete_note_adjustment"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            target = config / "natural_language_adjustments.json"
            scope = {"json": json, "Path": Path, "CONFIG_DIR": config, "NOTE_ADJUSTMENT_FILE": target,
                     "now_jst": lambda: datetime.fromisoformat("2026-09-23T10:00:00+09:00")}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
            month_dir = root / "backups" / "2026-10"
            month_dir.mkdir(parents=True)
            (month_dir / "preferences_test.json").write_text(json.dumps({
                "author": "田中", "saved_at": "2026-09-23T10:00:00+09:00",
                "off_requests": {"田中": [4]}, "natural_language_notes": {"田中": "最大6連勤まで許容"},
            }, ensure_ascii=False), encoding="utf-8")
            with patch("prototype.submission_loader.BACKUP_DIR", root / "backups"), \
                 patch("prototype.submission_loader.PROJECT_ROOT", root), \
                 patch("prototype.submission_loader.NOTE_ADJUSTMENT_FILE", target), \
                 patch("prototype.github_backup.sync_preferences_from_github"), \
                 patch("prototype.github_backup.sync_latest_config_from_github"), \
                 patch("prototype.github_backup.push_config_to_github"), \
                 patch("prototype.submission_loader.is_submission_in_window", return_value=True):
                scope["upsert_note_adjustment"](2026, 10, "田中", "補正のみ反映", CORRECTION, "AI整理: テスト")
                self.assertEqual(_load_latest_note_adjustments(2026, 10)["田中"]["memo"], "AI整理: テスト")
                self.assertEqual(_load_latest_note_adjustments(2026, 11), {})
                data = load_submissions_for_month(2026, 10, ["田中"])
                self.assertEqual(data.max_consecutive_work_days["田中"], 5)
                self.assertEqual(data.preferred_work_requests, [("田中", d, None) for d in (1, 2, 3)])
                self.assertEqual(data.off_requests["田中"], [4])
                self.assertEqual(data.parsed_note_summaries["田中"]["blocked_work_days"], [4])
                self.assertEqual([item["day"] for item in data.parsed_note_summaries["田中"]["work_requests"]], [1, 2, 3])
                scope["delete_note_adjustment"](2026, 10, "田中")
                self.assertEqual(_load_latest_note_adjustments(2026, 10), {})
                restored = load_submissions_for_month(2026, 10, ["田中"])
                self.assertEqual(restored.max_consecutive_work_days["田中"], 6)
                self.assertEqual(restored.off_requests["田中"], [4])


class NoteAITest(unittest.TestCase):
    def test_ai_reported_example_round_trip(self):
        result = validate_note_interpretation({"conditions": [
            condition("max_work_streak", "五連勤可能", value=5),
            condition("max_off_streak", "二連休憩不可", value=1),
            condition("work_dates", "1.2.3.4日全て出勤", days=[1, 2, 3, 4]),
        ], "review_messages": ["『二連休憩不可』を『2連休不可』として整理しました。"]}, ORIGINAL, 2026, 10)
        self.assertEqual(asdict(parse_natural_language_note(result.corrected_text, 2026, 10)),
                         asdict(parse_natural_language_note(ORIGINAL, 2026, 10)))
        self.assertEqual(len(result.evidence), 3)

    def test_supported_condition_round_trips(self):
        for item in (
            condition("off_dates", days=[2, 4]), condition("work_dates", days=[3, 5], store="OMIYA"),
            condition("choice_off", days=[2, 5, 12], value=1),
            condition("choice_work", days=[2, 5, 12], value=1, store="HIGASHIGUCHI"),
            condition("paid_leave_days", value=2), condition("holiday_days", value=10),
            condition("paid_leave_days", value=0), condition("holiday_days", value=0),
            condition("work_day_count", value=12), condition("preferred_off_streak", value=2),
            condition("off_streak_count", value=1, length=2, comparison="exact"),
            condition("work_streak_count", value=1, length=5, comparison="max"),
        ):
            with self.subTest(kind=item["kind"]):
                self.assertTrue(validate_note_interpretation(
                    {"conditions": [item], "review_messages": []}, "原文", 2026, 10,
                ).corrected_text)

    def test_invalid_or_invented_results_are_rejected(self):
        invalid = [
            condition("work_dates", days=[32]), condition("work_dates", days=[True]),
            condition("work_dates", days=[2], store="SECRET"),
            condition("work_dates", "原文にない根拠", days=[2]),
            condition("max_work_streak", value=0), condition("holiday_days", value=100),
            condition("choice_off", days=[2, 3], value=3),
            dict(condition("work_dates", days=[2]), command="execute"),
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate_note_interpretation({"conditions": [item], "review_messages": []}, "原文", 2026, 10)

    def test_conflicting_and_unsupported_merged_conditions_are_rejected(self):
        for items in (
            [condition("work_dates", days=[2]), condition("off_dates", days=[2])],
            [condition("holiday_days", value=10), condition("work_day_count", value=23)],
            [condition("max_off_streak", value=1), condition("preferred_off_streak", value=3)],
            [condition("holiday_days", value=2), condition("paid_leave_days", value=3)],
        ):
            with self.assertRaises(ValueError):
                validate_note_interpretation({"conditions": items, "review_messages": []}, "原文", 2026, 10)

    def test_ambiguous_note_can_return_only_review(self):
        p = validate_note_interpretation({"conditions": [], "review_messages": ["12日か12日間か確認が必要です。"]},
                                         "12日くらいで", 2026, 10)
        self.assertFalse(p.corrected_text)
        self.assertTrue(p.review_messages)

    def test_openai_uses_schema_no_tools_no_schedule_and_no_storage(self):
        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.responses.create.return_value = SimpleNamespace(status="completed", output_text=json.dumps({
            "conditions": [condition("work_dates", "1日は出勤", days=[1])], "review_messages": [],
        }))
        with patch("openai.OpenAI", factory):
            result = interpret_note("1日は出勤", 2026, 10, provider="openai", api_key="test", model="configured-model")
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "configured-model")
        self.assertFalse(kwargs["store"])
        self.assertTrue(kwargs["text"]["format"]["strict"])
        self.assertNotIn("tools", kwargs)
        self.assertEqual(set(json.loads(kwargs["input"])), {"year", "month", "text", "stores"})
        self.assertEqual(parse_natural_language_note(result.corrected_text, 2026, 10).work_requests, [(1, None)])

    def test_incomplete_response_and_api_failures_do_not_return_partial_rules(self):
        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.responses.create.return_value = SimpleNamespace(status="incomplete", output_text="{}")
        with patch("openai.OpenAI", factory), self.assertRaises(ValueError):
            interpret_note("原文", 2026, 10, provider="openai", api_key="test", model="test")
        client.responses.create.side_effect = RuntimeError("secret-personal-data")
        with patch("openai.OpenAI", factory), self.assertRaises(ValueError) as caught:
            interpret_note("原文", 2026, 10, provider="openai", api_key="test", model="test")
        self.assertNotIn("secret-personal-data", str(caught.exception))

    def test_anthropic_uses_validated_single_tool_result(self):
        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.messages.create.return_value = SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", name="interpret_shift_note", input={
                "conditions": [condition("work_dates", "1日は出勤", days=[1])], "review_messages": [],
            })
        ])
        with patch("anthropic.Anthropic", factory):
            result = interpret_note("1日は出勤", 2026, 10, provider="anthropic", api_key="test", model="configured-model")
        self.assertTrue(result.corrected_text)
        self.assertEqual(client.messages.create.call_args.kwargs["model"], "configured-model")


class NoteEditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit.testing.v1 import AppTest
        cls.AppTest = AppTest
        cls.script = '''
import json, os, re
import streamlit as st
from prototype.models import Store
from prototype.consecutive_counts import consecutive_count_label
def get_openai_api_key(): return "test" if st.session_state.get("has_ai") else None
def get_anthropic_api_key(): return None
def get_openai_model(): return "test-model"
def upsert_note_adjustment(year, month, employee, status, text, memo):
    st.session_state["existing"] = dict(status=status, corrected_text=text, memo=memo)
    st.session_state["saves"] = st.session_state.get("saves", 0) + 1
def delete_note_adjustment(*args):
    st.session_state["existing"] = {}
''' + display_source() + f'''
render_note_adjustment_editor({ORIGINAL!r}, st.session_state.get("existing", {{}}), 2026, 10, "テスト")
'''

    def app(self, ai=False):
        app = self.AppTest.from_string(self.script)
        app.session_state["has_ai"] = ai
        return app.run()

    @staticmethod
    def button(app, label):
        return next(b for b in app.button if b.label == label)

    def test_append_preview_save_and_reset(self):
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox[1].value, "確認済み")
        app.text_area[0].set_value("6日は出勤。連勤上限: 6連勤まで")
        self.button(app, "読取・最終条件を確認").click().run()
        text = " ".join(m.value for m in app.markdown)
        self.assertIn("6連勤まで", text)
        final = next(m.value for m in app.markdown if "原文と補正を合わせた最終条件:" in m.value)
        self.assertIn("1日出勤希望", final)
        self.assertNotIn("5連勤まで", final)
        self.assertFalse(app.warning)
        self.button(app, "管理者補正を保存").click().run()
        self.assertEqual(app.session_state["existing"]["status"], "確認済み")
        self.button(app, "管理者補正を削除・リセット").click().run()
        self.assertFalse(app.session_state["existing"])
        self.assertEqual(app.text_area[0].value, "")
        self.assertFalse(app.exception)

    def test_unread_active_correction_rejected_but_draft_allowed(self):
        app = self.app()
        app.text_area[0].set_value("後で相談したいです")
        self.button(app, "管理者補正を保存").click().run()
        self.assertTrue(app.error)
        self.assertNotIn("existing", app.session_state)
        app.selectbox[1].set_value("要確認")
        self.button(app, "管理者補正を保存").click().run()
        self.assertEqual(app.session_state["existing"]["status"], "要確認")

    def test_preview_separates_work_wishes_blocked_by_off_requests(self):
        scope = {"re": __import__("re"), "Store": Store,
                 "consecutive_count_label": __import__("prototype.consecutive_counts", fromlist=["consecutive_count_label"]).consecutive_count_label}
        exec(display_source(), scope)
        review = scope["build_note_reflection_review"](
            ORIGINAL, {"status": "補正のみ反映", "corrected_text": CORRECTION}, 2026, 10, [4],
        )
        self.assertNotIn("4日出勤希望", " / ".join(review["final_labels"]))
        self.assertIn("4日の出勤希望は×休みと重なる", " / ".join(review["notes"]))

    def test_ai_requires_consent_review_and_explicit_save(self):
        app = self.app(ai=True)
        self.assertTrue(self.button(app, "AIで文章を整理").disabled)
        app.checkbox[0].check().run()
        self.assertFalse(self.button(app, "AIで文章を整理").disabled)
        result = NoteInterpretation("連勤上限: 5連勤まで。\n1日は出勤希望。", ["訂正あり"], ["五連勤可能"])
        with patch("prototype.note_interpreter.interpret_note", return_value=result) as mocked:
            self.button(app, "AIで文章を整理").click().run()
        self.assertEqual(mocked.call_args.args[0], ORIGINAL)
        self.assertNotIn("existing", app.session_state)
        self.assertTrue(self.button(app, "この案を補正欄に入れる").disabled)
        app.checkbox[1].check().run()
        self.button(app, "この案を補正欄に入れる").click().run()
        self.assertEqual(app.text_area[0].value, result.corrected_text)
        self.assertEqual(app.selectbox[1].value, "補正のみ反映")
        self.assertNotIn("existing", app.session_state)
        self.button(app, "管理者補正を保存").click().run()
        self.assertIn("AI整理: openai", app.session_state["existing"]["memo"])
        self.assertEqual(app.session_state["saves"], 1)
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
