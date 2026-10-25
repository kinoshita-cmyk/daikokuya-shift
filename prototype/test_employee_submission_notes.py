"""希望条件と非公開の事情を分離した提出フロー。実データには書き込まない。"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from prototype.backup import ShiftBackup
from prototype.employees import ALL_EMPLOYEES
from prototype.rule_book import build_month
from prototype.rule_config import RuleConfig
from prototype.submission_loader import load_submissions_for_month
from prototype.submission_review_ui import build_review_rows


ROOT = Path(__file__).resolve().parents[1]
CONDITION = " １６日・１７日のどちらか1日休み希望\n有給2日を含め、合計10日休み希望\n"
PERSONAL = "子どもの運動会があるためです。\n経営者への相談：25日休み希望という意味ではありません。"


def employee_form_script(backup_dir, show_personal=True):
    """本体の実際の入力・確認・完了ブロックをAppTestでも使う。"""
    tree = ast.parse((ROOT / "app" / "app.py").read_text(encoding="utf-8"))
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Compare)
                  and any(isinstance(c, ast.Constant) and c.value == "👤 従業員ビュー" for c in n.test.comparators))
    start = next(i for i, n in enumerate(branch.body)
                 if isinstance(n, ast.ImportFrom) and n.module == "prototype.employees")
    funcs = {"summarize_natural_language_note_for_review", "format_day_list", "render_scrollable_review_table"}
    definitions = "\n".join(ast.unparse(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in funcs)
    header = f'''
import re
import streamlit as st
from pathlib import Path
from datetime import date, datetime
from calendar import monthrange
from html import escape
from prototype.backup import ShiftBackup as RealBackup
from prototype.employees import get_employee
from prototype.submission_window import format_timestamp_jst
from prototype.consecutive_counts import consecutive_count_label
from prototype.rules import get_monthly_work_target
class ShiftBackup(RealBackup):
    def __init__(self): super().__init__(Path({str(backup_dir)!r}))
    def _write_json_atomic(self, path, data):
        if st.session_state.get("fail_save"): raise OSError("test-only failure")
        super()._write_json_atomic(path, data)
def now_jst(): return datetime(2026, 10, 25, 12)
def _startup_log(message): pass
selected = "今津"
target_year, target_month = 2026, 11
today = date(2026, 10, 25)
can_view_employee_personal_notes = {show_personal!r}
'''
    return header + definitions + "\n" + "\n".join(ast.unparse(n) for n in branch.body[start:])


class EmployeeSubmissionNotesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.backup = ShiftBackup(self.path / "backups")
        self.employee = next(e for e in ALL_EMPLOYEES if e.name == "今津")
        for target, value in (
            ("prototype.github_backup.sync_preferences_from_github", None),
            ("prototype.github_backup.push_preference_to_github", (True, "test")),
            ("prototype.github_backup.sync_latest_config_from_github", None),
            ("prototype.employees.get_employee", self.employee),
            ("prototype.backup.is_submission_in_window", True),
            ("prototype.submission_loader.is_submission_in_window", True),
        ):
            context = patch(target, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        for target, value in (
            ("prototype.backup.PROJECT_ROOT", self.path),
            ("prototype.submission_loader.PROJECT_ROOT", self.path),
            ("prototype.submission_loader.BACKUP_DIR", self.backup.backup_dir),
            ("prototype.submission_loader.NOTE_ADJUSTMENT_FILE", self.path / "missing.json"),
        ):
            context = patch(target, value)
            context.start()
            self.addCleanup(context.stop)

    def save(self, condition=CONDITION, personal=PERSONAL, **kwargs):
        return self.backup.save_preferences(
            2026, 11, {"今津": [14]}, [], [], {"今津": condition}, author="今津",
            personal_notes={"今津": personal}, paid_leave_days=2,
            monthly_target_workdays=22, base_holidays=8, **kwargs,
        )

    def app(self, show_personal=True):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string(employee_form_script(self.backup.backup_dir, show_personal)).run()
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(b for b in app.button if b.label == label).click().run()
        self.assertFalse(app.exception)

    def set_texts(self, app, condition=CONDITION, personal=PERSONAL):
        app.text_area(key="free_text_今津_2026_11").set_value(condition)
        app.text_area(key="personal_text_今津_2026_11").set_value(personal)

    def test_atomic_save_includes_conditions_context_and_leave(self):
        path = self.save()
        payload = self.backup.load_preferences(path)
        self.assertEqual(payload["natural_language_notes"]["今津"], CONDITION)
        self.assertEqual(payload["personal_notes"]["今津"], PERSONAL)
        self.assertEqual(payload["paid_leave_days"], 2)
        self.assertEqual(payload["monthly_target_workdays"], 22)
        self.assertFalse(list(path.parent.glob("*.tmp")))
        item = self.backup.get_submission_status(2026, 11, ["今津"])["submitted"][0]
        self.assertEqual(item["note"], CONDITION)
        self.assertEqual(item["personal_note"], PERSONAL)

    def test_legacy_mixed_text_remains_intact(self):
        mixed = CONDITION + PERSONAL
        path = self.backup.save_preferences(2026, 11, {}, [], [], {"今津": mixed}, "今津")
        self.assertNotIn("personal_notes", self.backup.load_preferences(path))
        app = self.app()
        self.assertIn(mixed.strip(), [t.value for t in app.text])
        self.click(app, "内容を修正して再提出する")
        self.assertEqual(app.text_area(key="free_text_今津_2026_11").value, mixed)
        self.assertEqual(app.text_area(key="personal_text_今津_2026_11").value, "")

    def test_personal_context_is_never_parsed_or_published(self):
        self.save(personal="private_reason 25日休み希望。有給30日使用。")
        sub = load_submissions_for_month(2026, 11, ["今津"], sync_remote=False)
        self.assertNotIn(25, sub.off_requests.get("今津", []))
        self.assertEqual(sub.paid_leave_days["今津"], 2)
        self.assertNotIn("private_reason", json.dumps(asdict(sub), ensure_ascii=False, default=str))
        book = build_month(2026, 11, RuleConfig(), [self.employee], {}, sub)
        self.assertNotIn("private_reason", json.dumps(book, ensure_ascii=False))

    def test_personal_only_submission_does_not_create_parse_warning(self):
        self.save(condition="")
        app = self.app()
        self.click(app, "内容を修正して再提出する")
        self.click(app, "確認画面へ進む")
        self.assertIn(PERSONAL, [t.value for t in app.text])
        self.assertNotIn("自動で条件化", " ".join(t.value for t in app.text))
        self.assertFalse(any("読み取れた希望条件" in t.value for t in app.markdown))

    def test_review_back_submit_reopen_and_reedit_keep_both_texts(self):
        app = self.app()
        self.set_texts(app)
        self.click(app, "確認画面へ進む")
        self.assertIn(CONDITION.strip(), [t.value for t in app.text])
        self.assertIn(PERSONAL, [t.value for t in app.text])
        self.assertTrue(any("合計10日" in t.value for t in app.text))
        self.click(app, "入力に戻る")
        self.assertEqual(app.text_area(key="free_text_今津_2026_11").value, CONDITION)
        self.assertEqual(app.text_area(key="personal_text_今津_2026_11").value, PERSONAL)
        self.click(app, "確認画面へ進む")
        self.click(app, "この内容で提出する")
        self.assertIn(PERSONAL, [t.value for t in app.text])
        self.click(app, "内容を修正して再提出する")
        self.assertEqual(app.text_area(key="personal_text_今津_2026_11").value, PERSONAL)
        reopened = self.app()
        self.assertIn(CONDITION.strip(), [t.value for t in reopened.text])
        self.assertIn(PERSONAL, [t.value for t in reopened.text])

    def test_resubmission_can_clear_personal_context(self):
        self.save()
        app = self.app()
        self.click(app, "内容を修正して再提出する")
        self.set_texts(app, personal="")
        self.click(app, "確認画面へ進む")
        self.click(app, "この内容で提出する")
        item = self.backup.get_submission_status(2026, 11, ["今津"])["submitted"][0]
        self.assertEqual(item["personal_note"], "")
        self.assertEqual(item["note"], CONDITION)

    def test_unknown_condition_remains_submittable_without_false_success(self):
        app = self.app()
        self.set_texts(app, condition="月末あたりに特別な配置を相談したいです。")
        self.click(app, "確認画面へ進む")
        self.assertTrue(any("確認が必要な点" in t.value for t in app.markdown))
        self.assertTrue(any("このまま提出できます" in t.value for t in app.info))
        self.assertTrue(any("シフト確定ではありません" in t.value for t in app.caption))
        self.click(app, "この内容で提出する")
        self.assertTrue(any("希望を受け付けました" in s.value for s in app.success))

    def test_save_failure_keeps_review_instead_of_reporting_completion(self):
        app = self.app()
        self.set_texts(app)
        self.click(app, "確認画面へ進む")
        app.session_state["fail_save"] = True
        self.click(app, "この内容で提出する")
        self.assertTrue(any("保存できませんでした" in e.value for e in app.error))
        self.assertFalse(app.success)
        self.assertIn(PERSONAL, [t.value for t in app.text])
        self.assertFalse(list(self.backup.backup_dir.rglob("preferences_*.json")))

    def test_shift_manager_preview_hides_and_preserves_private_context(self):
        self.save()
        app = self.app(show_personal=False)
        self.assertNotIn(PERSONAL, [t.value for t in app.text])
        self.click(app, "内容を修正して再提出する")
        self.assertEqual(len(app.text_area), 1)
        app.text_area[0].set_value("月12日勤務希望")
        self.click(app, "確認画面へ進む")
        self.assertNotIn(PERSONAL, [t.value for t in app.text])
        self.click(app, "この内容で提出する")
        item = self.backup.get_submission_status(2026, 11, ["今津"])["submitted"][0]
        self.assertEqual(item["personal_note"], PERSONAL)

    def test_management_context_is_separate_and_manager_only(self):
        reflection = Mock(return_value={"notes": [], "source_label": "自動反映のみ"})
        item = {"employee": "今津", "note": CONDITION, "personal_note": PERSONAL}
        for visible in (False, True):
            rows, details = build_review_rows(
                ["今津"], [item], {}, {"今津": {}}, 2026, 11, reflection,
                lambda summary: ["希望条件"], str, include_personal_notes=visible,
            )
            self.assertEqual(details["今津"]["personal_note"], PERSONAL if visible else "")
            self.assertEqual(PERSONAL.splitlines()[-1] in json.dumps(details, ensure_ascii=False), visible)
            self.assertEqual("事情・相談" in rows[0], visible)
            self.assertNotIn(PERSONAL, json.dumps(rows, ensure_ascii=False))
        self.assertEqual(reflection.call_args.args[0], CONDITION)
        self.assertEqual(item["personal_note"], PERSONAL)


if __name__ == "__main__":
    unittest.main()
