import copy
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, Mock

from prototype import rules
from prototype.employees import ALL_EMPLOYEES
from prototype.models import Store, Affinity
from prototype.rule_book import build_month, build_snapshot, custom_cards, effective_staff, submission_cards, digest
from prototype.rule_config import RuleConfig, CustomRule
from prototype.submission_loader import SubmissionData, load_submissions_for_month
from prototype.rule_book_sync import publish_snapshot, source_signature


class RuleBookTests(unittest.TestCase):
    def setUp(self):
        self.config = RuleConfig()
        self.employee = copy.deepcopy(next(e for e in ALL_EMPLOYEES if e.name == "今津"))
        self.ledger = {"rules":[
            {"分類":"絶対条件","ルール":"最大連勤","内容":"古い値"},
            {"分類":"強い目標","ルール":"2連休","内容":"古い値"},
            {"分類":"月別例外","ルール":"古い7月例外","内容":"引き継がない"},
        ]}

    def month(self, month):
        return build_month(2026, month, self.config, [self.employee], self.ledger)

    def test_current_parameters_replace_old_ledger_for_future_months(self):
        self.config.parameters["max_consec_work"] = 6
        for month in (10, 11, 12):
            book = self.month(month)
            self.assertIn("6日", book["basic"][0]["text"])
            self.assertNotIn("古い値", json.dumps(book, ensure_ascii=False))
            self.assertNotIn("古い7月例外", json.dumps(book, ensure_ascii=False))

    def test_monthly_store_override_is_not_inherited_or_mutated(self):
        original = copy.deepcopy(self.employee.affinities)
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {
            (2026, 10): {"今津":{"primary_store":Store.SUZURAN,"normal_stores":(Store.AKABANE,), "support_stores":(), "remove_support_stores":()}},
        }, clear=True):
            october = effective_staff(self.employee, 2026, 10)
            november = effective_staff(self.employee, 2026, 11)
        self.assertEqual(october["roles"]["主担当"], ["大宮すずらん通り店"])
        self.assertIsNotNone(october["baseline"])
        self.assertIsNone(november["baseline"])
        self.assertEqual(november["roles"]["主担当"], ["赤羽駅前店"])
        self.assertEqual(self.employee.affinities, original)

    def test_forbidden_affinity_is_not_relaxed_by_book(self):
        self.employee.affinities[Store.SUZURAN] = Affinity.NONE
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {
            (2026, 10): {"今津":{"primary_store":Store.SUZURAN}},
        }, clear=True):
            result = effective_staff(self.employee, 2026, 10)
        self.assertIn("大宮すずらん通り店", result["roles"]["絶対配置不可"])

    def test_custom_rules_scope_disabled_and_note_only(self):
        self.config.custom_rules = [
            CustomRule("oct", "10月配置", "", target_year=2026,target_month=10,rule_type="required_assignment",employee="今津",day=4,stores=["AKABANE"],severity="ERROR"),
            CustomRule("off", "無効", "", enabled=False),
            CustomRule("base", "原則メモ", "メモ",rule_type="note"),
        ]
        oct_rules = custom_cards(self.config, 2026, 10)
        self.assertEqual(len(oct_rules), 2)
        self.assertEqual(oct_rules[0]["strength"], "絶対")
        self.assertIn("赤羽駅前店", oct_rules[0]["text"])
        self.assertEqual([r["id"] for r in custom_cards(self.config, 2026, 11)], ["custom:base"])
        self.assertIn("自動生成の制約にはなりません", oct_rules[1]["detail"])

    def test_private_raw_notes_never_exported_conditional_work_is_explicit(self):
        sub = SubmissionData(2026, 10, submitted_employees=["今津"],
            natural_language_notes={"今津":"private_original_complaint"},
            admin_note_adjustments={"今津":"private_correction"},
            off_requests={"今津":[14]},
            conditional_store_requests=[("今津",5,Store.AKABANE)],
            consecutive_count_rules=[{"employee":"今津","kind":"off","days":2,"count":1,"comparison":"exact"}])
        rows = submission_cards(sub, [self.employee])
        encoded = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("private_", encoded)
        self.assertIn("出勤する場合の希望店舗", encoded)
        self.assertIn("2連休を月内ちょうど1回", encoded)

    def test_snapshot_refuses_broken_config(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "rule_config.json").write_text("{broken")
            with self.assertRaises(json.JSONDecodeError):
                build_snapshot(date(2026,10,20),config_dir=td,include_submissions=False)

    def test_unsubmitted_employee_admin_conditions_are_included(self):
        sub = SubmissionData(2026, 10, paid_leave_days={"今津":2},
                             requested_holiday_days={"今津":10})
        rows = submission_cards(sub, [self.employee])
        self.assertEqual(len(rows), 1)
        self.assertIn("管理者調整込み", rows[0]["text"])
        self.assertIn("10日", rows[0]["text"])

    def test_month_rollover_triggers_sync_without_file_changes(self):
        with patch("prototype.rule_book_sync.now_jst", return_value=datetime(2026,10,31,23,59)):
            october = source_signature()
        with patch("prototype.rule_book_sync.now_jst", return_value=datetime(2026,11,1)):
            november = source_signature()
        self.assertNotEqual(october, november)

    def test_monthly_cards_match_shared_rule_descriptions(self):
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {
            (2026, 10): {"今津":{"primary_store":Store.AKABANE}},
        }, clear=True):
            book = self.month(10)
        self.assertEqual(book["monthly"][0]["id"], "monthly:store:今津")
        self.assertIn("赤羽駅前店", book["monthly"][0]["text"])
        self.assertIn("今津", book["monthly"][0]["title"])

    def test_loader_read_only_does_not_sync_or_write(self):
        with tempfile.TemporaryDirectory() as td:
            backup = Path(td)/"2026-10"
            backup.mkdir()
            payload = {"author":"今津","saved_at":"2026-09-23T10:00:00+09:00",
                       "off_requests":{"今津":[1]}, "natural_language_notes":{"今津":"2連休1回のみ"}}
            path = backup/"preferences_test.json"
            path.write_text(json.dumps(payload),encoding="utf-8")
            before = path.read_bytes()
            with patch("prototype.submission_loader.BACKUP_DIR", Path(td)), \
                 patch("prototype.submission_loader.PROJECT_ROOT", Path(td)), \
                 patch("prototype.submission_loader.NOTE_ADJUSTMENT_FILE", Path(td)/"missing.json"), \
                 patch("prototype.github_backup.sync_preferences_from_github") as prefs_sync, \
                 patch("prototype.github_backup.sync_latest_config_from_github") as config_sync:
                sub = load_submissions_for_month(2026,10,["今津"],sync_remote=False)
            prefs_sync.assert_not_called()
            config_sync.assert_not_called()
            self.assertEqual(path.read_bytes(),before)
            self.assertIn("今津",sub.submitted_employees)

    def test_publish_verifies_acknowledgement_and_does_not_follow_redirect(self):
        snapshot = {"content_hash":"abc"}
        response = Mock(status_code=200)
        response.json.return_value={"content_hash":"abc","synced_at":"now"}
        post = Mock(return_value=response)
        self.assertEqual(publish_snapshot(snapshot,"not-real-token",post=post),"now")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        response.json.return_value={"content_hash":"wrong"}
        with self.assertRaises(RuntimeError):
            publish_snapshot(snapshot,"not-real-token",post=post)

    def test_publish_error_does_not_disclose_response_or_token(self):
        response = Mock(status_code=503,text="secret_details")
        with self.assertRaises(RuntimeError) as error:
            publish_snapshot({},"secret_token",post=Mock(return_value=response))
        self.assertNotIn("secret",str(error.exception))

    def test_hash_ignores_dictionary_order(self):
        self.assertEqual(digest({"b":2,"a":1}),digest({"a":1,"b":2}))


if __name__ == "__main__":
    unittest.main()
