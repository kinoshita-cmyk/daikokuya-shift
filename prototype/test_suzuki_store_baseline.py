import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototype import rules
from prototype.employee_config import (
    EmployeeConfigManager, SUZUKI_STORE_UPDATE_KEY, employee_from_dict, employee_to_dict,
)
from prototype.employees import ALL_EMPLOYEES
from prototype.models import Affinity, Store
from prototype.rule_book import effective_staff


class SuzukiStoreBaselineTests(unittest.TestCase):
    def old_record(self):
        employee = next(e for e in ALL_EMPLOYEES if e.name == "鈴木")
        record = employee_to_dict(employee)
        record.pop(SUZUKI_STORE_UPDATE_KEY)
        record["affinities"]["SUZURAN"] = "中"
        record["notes"] = "通常対応可: 赤羽・すずらん。応援・巡回可: 大宮。絶対配置不可: 東口・西口。"
        return record

    def assert_baseline(self, employee):
        self.assertEqual(employee.affinities, {
            Store.SUZURAN: Affinity.STRONG, Store.AKABANE: Affinity.MEDIUM,
            Store.OMIYA: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE, Store.NISHIGUCHI: Affinity.NONE,
        })

    def test_fallback_defaults_and_saved_old_master_agree(self):
        default = next(e for e in ALL_EMPLOYEES if e.name == "鈴木")
        restored = employee_from_dict(self.old_record())
        self.assert_baseline(default)
        self.assert_baseline(restored)
        self.assertEqual(default.notes, restored.notes)

    def test_migration_preserves_other_fields_and_input(self):
        record = self.old_record()
        record["notes"] = "管理者の独自メモ"
        record["annual_target_days"] = 260
        before = copy.deepcopy(record)
        restored = employee_from_dict(record)
        self.assertEqual(record, before)
        saved = employee_to_dict(restored)
        for key, value in record.items():
            if key != "affinities":
                self.assertEqual(saved[key], value, key)
        self.assertTrue(saved[SUZUKI_STORE_UPDATE_KEY])

    def test_other_employees_are_not_changed(self):
        record = self.old_record()
        record["name"] = "別のスタッフ"
        restored = employee_from_dict(record)
        self.assertEqual(restored.affinities[Store.SUZURAN], Affinity.MEDIUM)
        self.assertEqual(restored.notes, record["notes"])

    def test_later_admin_edits_survive_roundtrip(self):
        saved = employee_to_dict(employee_from_dict(self.old_record()))
        saved["affinities"]["AKABANE"] = "強"
        saved["affinities"]["SUZURAN"] = "中"
        for _ in range(2):
            restored = employee_from_dict(saved)
            self.assertEqual(restored.affinities[Store.AKABANE], Affinity.STRONG)
            self.assertEqual(restored.affinities[Store.SUZURAN], Affinity.MEDIUM)
            saved = employee_to_dict(restored)

    def test_settings_manager_load_save_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "employees.json")
            path.write_text(json.dumps({"employees": [self.old_record()]}), encoding="utf-8")
            with patch("prototype.github_backup.sync_latest_config_from_github"), \
                 patch("prototype.github_backup.push_config_to_github", return_value=(True, "test")):
                manager = EmployeeConfigManager(Path(directory))
                self.assert_baseline(manager.load_all()[0])
                manager.update_employee("鈴木", {"notes": "更新メモ"}, actor="test")
                saved = json.loads(path.read_text(encoding="utf-8"))["employees"][0]
                self.assertTrue(saved[SUZUKI_STORE_UPDATE_KEY])
                self.assert_baseline(manager.load_all()[0])

    def test_generator_and_book_use_permanent_baseline_across_months(self):
        employee = employee_from_dict(self.old_record())
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {}, clear=True):
            for year, month in ((2026, 10), (2026, 11), (2026, 12), (2027, 1)):
                effective = rules.effective_employee_store_affinities(employee, year, month)
                self.assertEqual(effective, employee.affinities)
                book = effective_staff(employee, year, month)
                self.assertEqual(book["roles"]["主担当"], ["大宮すずらん通り店"])
                self.assertEqual(book["roles"]["通常担当"], ["赤羽駅前店"])
                self.assertEqual(book["scope"], "全月共通")
                self.assertIsNone(book["baseline"])

    def test_explicit_monthly_exception_still_takes_priority(self):
        employee = employee_from_dict(self.old_record())
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {(2026, 11): {
            "鈴木": {"primary_store": Store.AKABANE, "normal_stores": (Store.SUZURAN,),
                     "support_stores": (Store.OMIYA,)},
        }}, clear=True):
            november = effective_staff(employee, 2026, 11)
            december = effective_staff(employee, 2026, 12)
        self.assertEqual(november["roles"]["主担当"], ["赤羽駅前店"])
        self.assertEqual(november["baseline"]["主担当"], ["大宮すずらん通り店"])
        self.assertEqual(december["roles"]["主担当"], ["大宮すずらん通り店"])
        self.assert_baseline(employee)

    def test_ledger_matches_permanent_baseline(self):
        path = Path(__file__).resolve().parents[1] / "config" / "rule_ledger_v1_0.json"
        ledger = json.loads(path.read_text(encoding="utf-8"))
        row = next(row for row in ledger["employee_store_suitability"] if row["氏名"] == "鈴木")
        self.assertEqual(row["主担当"], "すずらん")
        self.assertEqual(row["通常対応可"], "赤羽")
        self.assertEqual(row["応援・巡回可"], "大宮")
        self.assertEqual(row["絶対配置不可"], "東口、西口")


if __name__ == "__main__":
    unittest.main()
