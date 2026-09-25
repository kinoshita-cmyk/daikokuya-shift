"""管理者画面と月限定区分の回帰テスト。保存先はすべて一時ディレクトリ。"""
import ast
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from prototype import rules
from prototype.models import Store, Affinity
from prototype.monthly_store_update import prepare_store_update, push_verified_monthly_settings, UPDATE_ID, STORE_CHANGES


ROOT = Path(__file__).resolve().parents[1]


def app_function(name):
    source = (ROOT / "app" / "app.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


def employees():
    return [SimpleNamespace(name=name, is_auxiliary=False, affinities={
        Store.AKABANE: Affinity.MEDIUM,
        Store.SUZURAN: Affinity.MEDIUM,
        Store.OMIYA: Affinity.WEAK,
        Store.HIGASHIGUCHI: Affinity.NONE,
        Store.NISHIGUCHI: Affinity.NONE,
    }) for name in STORE_CHANGES]


class StoreUpdateTest(unittest.TestCase):
    def test_history_only_backup_success_is_not_reported_as_durable(self):
        data = {"applied_updates": [UPDATE_ID]}
        with patch("prototype.github_backup.push_config_to_github", return_value=(True, "history only")), \
             patch("prototype.github_backup.fetch_config_from_github", return_value=(True, {}, "old latest")):
            self.assertFalse(push_verified_monthly_settings(data)[0])
        with patch("prototype.github_backup.push_config_to_github", return_value=(True, "ok")), \
             patch("prototype.github_backup.fetch_config_from_github", return_value=(True, data, "latest")):
            self.assertTrue(push_verified_monthly_settings(data)[0])

    def test_only_requested_month_and_staff_changed_and_no_mutation(self):
        data = {"employee_store_overrides": {
            "2026-09": {"鈴木": {"primary_store": "AKABANE"}},
            "2026-10": {"他スタッフ": {"primary_store": "OMIYA"}},
            "2026-11": {"田中": {"primary_store": "AKABANE"}},
        }, "operation_modes": {"2026-10": {"3": "省人員"}}}
        before = deepcopy(data)
        result, changed = prepare_store_update(data, employees())
        self.assertTrue(changed)
        self.assertEqual(data, before)
        for month in ("2026-09", "2026-11"):
            self.assertEqual(result["employee_store_overrides"][month], data["employee_store_overrides"][month])
        self.assertEqual(result["operation_modes"], data["operation_modes"])
        october = result["employee_store_overrides"]["2026-10"]
        self.assertEqual(october["他スタッフ"], {"primary_store": "OMIYA"})
        self.assertEqual(october["鈴木"]["support_stores"], ["OMIYA"])
        self.assertEqual(october["田中"]["primary_store"], "SUZURAN")
        self.assertEqual(october["牧野"]["normal_stores"], ["SUZURAN", "OMIYA"])
        self.assertEqual(rules.validate_monthly_exceptions_data(result)[0], [])

    def test_deleted_override_not_recreated_on_restart(self):
        result, _ = prepare_store_update({}, employees())
        del result["employee_store_overrides"]["2026-10"]["鈴木"]
        repeated, changed = prepare_store_update(result, employees())
        self.assertFalse(changed)
        self.assertEqual(repeated, result)

    def test_conflicting_master_does_not_weaken_forbidden_store(self):
        staff = employees()
        staff[0].affinities[Store.SUZURAN] = Affinity.NONE
        with self.assertRaises(ValueError):
            prepare_store_update({}, staff)

    def test_saved_conditions_used_by_common_affinity_and_no_month_leak(self):
        with TemporaryDirectory() as temp, patch.object(rules, "MONTHLY_EXCEPTIONS_FILE", Path(temp) / "monthly.json"):
            result, _ = prepare_store_update({}, employees())
            self.assertTrue(rules.save_monthly_exceptions(result)[0])
            for employee in employees():
                primary, normal = STORE_CHANGES[employee.name]
                affinities = rules.effective_employee_store_affinities(employee, 2026, 10)
                self.assertEqual(affinities[primary], Affinity.STRONG)
                for store in normal:
                    self.assertEqual(affinities[store], Affinity.MEDIUM)
                self.assertEqual(affinities[Store.NISHIGUCHI], Affinity.NONE)
                for month in (9, 11):
                    self.assertEqual(rules.effective_employee_store_affinities(employee, 2026, month), employee.affinities)
            lines = rules.active_monthly_exception_descriptions(2026, 10)
            self.assertEqual(len(lines), 3)
            self.assertFalse(any(line.startswith("固定") for line in lines))
            self.assertEqual(rules.active_monthly_exception_descriptions(2026, 11), [])
        rules.reload_monthly_exceptions()

    def test_full_name_default_and_old_alias_both_supported(self):
        from prototype.employees import ALL_EMPLOYEES
        employee = next(e for e in ALL_EMPLOYEES if e.name == "春山")
        self.assertEqual(employee.full_name, "春山廣植")
        self.assertEqual(rules.get_monthly_work_target("春山廣植", 10), rules.get_monthly_work_target("春山", 10))


class MonthlyPanelTest(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(rules, "MONTHLY_EXCEPTIONS_FILE", Path(self.temp.name) / "monthly.json")
        self.patch.start()
        self.addCleanup(self.restore)
        self.fetch_patch = patch("prototype.github_backup.fetch_config_from_github",
                                 side_effect=lambda name: (True, rules.load_monthly_exceptions_raw(), "ok"))
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        rules.save_monthly_exceptions({
            "employee_store_overrides": {
                "2026-09": {"鈴木": {"primary_store": "OMIYA"}},
            }, "operation_modes": {"2026-09": {"2": "省人員"}},
        })
        self.script = '''
import streamlit as st
import json
from calendar import monthrange
from prototype.submission_window import now_jst
from prototype.models import Store, Affinity
from prototype.test_manager_conditions_ui import employees
def shift_active_employees(): return employees()
def get_employee(name): return next(e for e in employees() if e.name == name)
def format_timestamp_jst(value): return str(value)
''' + app_function("render_monthly_exceptions_panel") + '''
render_monthly_exceptions_panel(st.session_state.get("year", 2026), st.session_state.get("month", 10), section=st.session_state.get("section", "店舗区分・研修など"))
'''
        self.AppTest = AppTest

    def restore(self):
        self.patch.stop()
        rules.reload_monthly_exceptions()

    @staticmethod
    def element(elements, label):
        return next(item for item in elements if item.label == label)

    def test_month_scoped_edit_confirm_save_delete_preserves_other_month(self):
        app = self.AppTest.from_string(self.script).run()
        self.assertFalse(app.exception)
        self.assertEqual(self.element(app.selectbox, "追加・変更する条件").value, "👤 月限定の店舗区分（主担当・通常担当・応援巡回担当）")
        self.element(app.selectbox, "この月の主担当").set_value("SUZURAN")
        self.element(app.multiselect, "この月の通常担当").set_value(["AKABANE"])
        self.element(app.button, "この内容で追加・上書き").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn("2026-10", rules.load_monthly_exceptions_raw()["employee_store_overrides"])
        text = " ".join(m.value for m in app.markdown)
        self.assertIn("2026-10: 鈴木", text)
        with patch("prototype.github_backup.push_config_to_github", return_value=(True, "test")):
            self.element(app.button, "✅ 内容を確認して保存").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.success)
        data = rules.load_monthly_exceptions_raw()
        self.assertEqual(data["employee_store_overrides"]["2026-10"]["鈴木"]["primary_store"], "SUZURAN")
        app.button("mx_del_employee_override_2026-10_鈴木").click().run()
        with patch("prototype.github_backup.push_config_to_github", return_value=(True, "test")):
            self.element(app.button, "✅ 内容を確認して保存").click().run()
        self.assertFalse(app.exception)
        data = rules.load_monthly_exceptions_raw()
        self.assertNotIn("2026-10", data["employee_store_overrides"])
        self.assertEqual(data["employee_store_overrides"]["2026-09"]["鈴木"]["primary_store"], "OMIYA")
        self.assertEqual(self.element(app.selectbox, "この月の主担当").value, "")

    def test_switch_month_drops_unsaved_values_and_pending_confirmation(self):
        app = self.AppTest.from_string(self.script).run()
        self.element(app.selectbox, "この月の主担当").set_value("SUZURAN")
        self.element(app.multiselect, "この月の通常担当").set_value(["AKABANE"])
        self.element(app.button, "この内容で追加・上書き").click().run()
        app.session_state["month"] = 11
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.element(app.selectbox, "この月の主担当").value, "")
        self.assertFalse(any(b.label == "✅ 内容を確認して保存" for b in app.button))

    def test_operation_mode_uses_selected_month_only(self):
        app = self.AppTest.from_string(self.script)
        app.session_state["section"] = "営業体制"
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(any(s.label == "この月の主担当" for s in app.selectbox))
        self.element(app.multiselect, "対象の日（複数選択可）").set_value([3])
        self.element(app.button, "営業モードを設定").click().run()
        with patch("prototype.github_backup.push_config_to_github", return_value=(True, "test")):
            self.element(app.button, "✅ 内容を確認して保存").click().run()
        self.assertFalse(app.exception)
        modes = rules.load_monthly_exceptions_raw()["operation_modes"]
        self.assertEqual(modes["2026-10"], {"3": "省人員"})
        self.assertEqual(modes["2026-09"], {"2": "省人員"})

    def test_boot_restores_remote_before_update_and_backs_up_marker(self):
        from prototype.submission_window import timestamp_sort_key
        scope = dict(timestamp_sort_key=timestamp_sort_key, format_timestamp_jst=str,
                     get_all_employees_including_retired=employees)
        exec(app_function("_restore_monthly_exceptions_on_boot"), scope)
        remote = {"updated_at": "2099-01-01T00:00:00+09:00",
                  "employee_store_overrides": {"2026-11": {"鈴木": {"primary_store": "OMIYA"}}},
                  "operation_modes": {"2026-10": {"4": "省人員"}}}
        fetches = []
        def fetch(name):
            fetches.append(name)
            return True, remote if len(fetches) == 1 else rules.load_monthly_exceptions_raw(), "ok"
        with patch("prototype.github_backup.fetch_config_from_github", side_effect=fetch), \
             patch("prototype.github_backup.push_config_to_github", return_value=(True, "ok")) as push:
            status = scope["_restore_monthly_exceptions_on_boot"]()
        self.assertIn("適用済み", status)
        saved = rules.load_monthly_exceptions_raw()
        self.assertIn(UPDATE_ID, saved["applied_updates"])
        self.assertEqual(saved["employee_store_overrides"]["2026-11"], remote["employee_store_overrides"]["2026-11"])
        self.assertEqual(saved["operation_modes"], remote["operation_modes"])
        self.assertEqual(push.call_args.args[1]["applied_updates"], [UPDATE_ID])
        del saved["employee_store_overrides"]["2026-10"]["鈴木"]
        rules.save_monthly_exceptions(saved)
        remote = rules.load_monthly_exceptions_raw()
        with patch("prototype.github_backup.fetch_config_from_github", return_value=(True, remote, "ok")), \
             patch("prototype.github_backup.push_config_to_github") as push:
            scope["_restore_monthly_exceptions_on_boot"]()
            push.assert_not_called()
        self.assertNotIn("鈴木", rules.load_monthly_exceptions_raw()["employee_store_overrides"]["2026-10"])

    def test_boot_network_failure_does_not_overwrite_unknown_remote_settings(self):
        scope = dict(get_all_employees_including_retired=employees)
        exec(app_function("_restore_monthly_exceptions_on_boot"), scope)
        before = rules.load_monthly_exceptions_raw()
        with patch("prototype.github_backup.fetch_config_from_github", return_value=(False, {}, "offline")), \
             patch("prototype.github_backup.push_config_to_github") as push:
            result = scope["_restore_monthly_exceptions_on_boot"]()
            push.assert_not_called()
        self.assertIn("保留", result)
        self.assertEqual(before, rules.load_monthly_exceptions_raw())


class SubmissionReviewTest(unittest.TestCase):
    def test_single_view_original_effective_preview_save_and_reset(self):
        from streamlit.testing.v1 import AppTest
        from prototype.test_note_interpreter import display_source
        script = '''
import json, os, re
import streamlit as st
from prototype.models import Store
from prototype.consecutive_counts import consecutive_count_label
from prototype.submission_loader import preview_note_adjustment
from prototype.submission_review_ui import build_review_rows, render_submission_review
def get_openai_api_key(): return None
def get_anthropic_api_key(): return None
def get_openai_model(): return "unused"
def upsert_note_adjustment(y,m,e,status,text,memo):
    st.session_state["saved"] = {e: dict(status=status, corrected_text=text, memo=memo)}
def delete_note_adjustment(y,m,e): st.session_state["saved"] = {}
''' + display_source() + '''
saved = st.session_state.get("saved", {})
original = "合計12日勤務希望。"
summaries = {"テスト": preview_note_adjustment(original, saved.get("テスト", {}), 2026, 10, [1])}
rows, details = build_review_rows(["テスト", "未提出"], [dict(employee="テスト", note=original, off_request_days=[1])], saved, summaries, 2026, 10, build_note_reflection_review, parsed_note_summary_to_labels, lambda days: "・".join(map(str, days)))
render_submission_review(rows, details, 2026, 10, render_note_adjustment_editor)
'''
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertIn("合計12日勤務希望", " ".join(t.value for t in app.text))
        self.assertIn("月12日", str(app.dataframe[0].value))
        app.text_area[0].set_value("合計13日勤務希望。")
        MonthlyPanelTest.element(app.button, "管理者補正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertIn("月13日", " ".join(t.value for t in app.text))
        self.assertIn("月13日", str(app.dataframe[0].value))
        MonthlyPanelTest.element(app.button, "管理者補正を削除・リセット").click().run()
        self.assertIn("月12日", " ".join(t.value for t in app.text))
        MonthlyPanelTest.element(app.selectbox, "確認・補正するスタッフ").set_value("未提出").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area[0].value, "")

    def test_read_failure_not_claimed_as_no_conditions(self):
        from prototype.submission_review_ui import build_review_rows
        reflection = lambda *args: dict(notes=[], final_labels=["fake"], original_auto_labels=[], source_label="自動反映のみ")
        rows, details = build_review_rows(["テスト"], [dict(employee="テスト", note="1日休み")], {}, None, 2026, 10, reflection, lambda s: [], str)
        self.assertEqual(rows[0]["確認"], "要確認")
        self.assertFalse(details["テスト"]["available"])
        self.assertIn("取得", rows[0]["生成に使う自由記載条件"])


if __name__ == "__main__":
    unittest.main()
