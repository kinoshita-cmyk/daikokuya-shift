import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototype import rules
from prototype.employee_config import (
    EmployeeConfigManager, TANAKA_STORE_UPDATE_KEY, employee_from_dict, employee_to_dict,
)
from prototype.employees import ALL_EMPLOYEES
from prototype.models import Affinity, Store, Skill
from prototype.rule_book import effective_staff


class TanakaStoreBaselineTests(unittest.TestCase):
    def old_record(self):
        record = employee_to_dict(next(e for e in ALL_EMPLOYEES if e.name == "田中"))
        record.pop(TANAKA_STORE_UPDATE_KEY)
        record["home_store"] = "AKABANE"
        record["affinities"]["AKABANE"] = "強"
        record["affinities"]["OMIYA"] = "弱"
        record["notes"] = "主担当: 赤羽。通常対応可: すずらん。応援・巡回可: 大宮。絶対配置不可: 東口・西口。"
        return record

    def assert_baseline(self, employee):
        self.assertEqual(employee.affinities, {
            Store.AKABANE: Affinity.MEDIUM, Store.OMIYA: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.MEDIUM, Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        })
        self.assertIsNone(employee.home_store)
        self.assertEqual(employee.skill, Skill.ECO_SUPPORT)

    def test_defaults_and_old_backup_restore_agree(self):
        default = next(e for e in ALL_EMPLOYEES if e.name == "田中")
        restored = employee_from_dict(self.old_record())
        self.assert_baseline(default)
        self.assert_baseline(restored)
        self.assertEqual(default.notes, restored.notes)

    def test_other_fields_custom_notes_and_input_are_preserved(self):
        old = self.old_record()
        old["notes"] = "管理者が追記したメモ"
        old["annual_target_days"] = 260
        before = copy.deepcopy(old)
        saved = employee_to_dict(employee_from_dict(old))
        self.assertEqual(old, before)
        for key, value in old.items():
            if key not in ("home_store", "affinities"):
                self.assertEqual(saved[key], value, key)
        self.assertTrue(saved[TANAKA_STORE_UPDATE_KEY])

    def test_later_admin_edits_are_not_reset(self):
        saved = employee_to_dict(employee_from_dict(self.old_record()))
        saved["home_store"] = "SUZURAN"
        saved["affinities"]["SUZURAN"] = "強"
        saved["affinities"]["OMIYA"] = "弱"
        for _ in range(2):
            restored = employee_from_dict(saved)
            self.assertEqual(restored.home_store, Store.SUZURAN)
            self.assertEqual(restored.affinities[Store.SUZURAN], Affinity.STRONG)
            self.assertEqual(restored.affinities[Store.OMIYA], Affinity.WEAK)
            saved = employee_to_dict(restored)

    def test_other_staff_are_unchanged(self):
        record = self.old_record()
        record["name"] = "他のスタッフ"
        restored = employee_from_dict(record)
        self.assertEqual(restored.home_store, Store.AKABANE)
        self.assertEqual(restored.affinities[Store.AKABANE], Affinity.STRONG)
        self.assertEqual(restored.affinities[Store.OMIYA], Affinity.WEAK)

    def test_settings_load_save_reload_and_backup_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "employees.json")
            path.write_text(json.dumps({"employees": [self.old_record()]}), encoding="utf-8")
            with patch("prototype.github_backup.sync_latest_config_from_github"), \
                 patch("prototype.github_backup.push_config_to_github", return_value=(True, "test")):
                manager = EmployeeConfigManager(Path(directory))
                self.assert_baseline(manager.load_all()[0])
                self.assertTrue(manager.update_employee("田中", {"notes": "追記"}, actor="test"))
                self.assert_baseline(manager.load_all()[0])
                self.assertTrue(json.loads(path.read_text())["employees"][0][TANAKA_STORE_UPDATE_KEY])

    def test_generator_and_book_use_all_month_baseline(self):
        employee = employee_from_dict(self.old_record())
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, {}, clear=True):
            for year, month in ((2026, 12), (2027, 1), (2027, 2)):
                self.assertEqual(rules.effective_employee_store_affinities(employee, year, month), employee.affinities)
                book = effective_staff(employee, year, month)
                self.assertEqual(book["roles"]["主担当"], [])
                self.assertEqual(set(book["roles"]["通常担当"]), {"赤羽駅前店", "大宮すずらん通り店", "大宮駅前店"})
                self.assertEqual(book["roles"]["応援・巡回"], [])
                self.assertEqual(book["scope"], "全月共通")

    def test_existing_december_exception_is_preserved_and_does_not_leak(self):
        employee = employee_from_dict(self.old_record())
        monthly = {(2026, 12): {"田中": {
            "primary_store": Store.SUZURAN, "normal_stores": (Store.AKABANE,),
            "support_stores": (Store.OMIYA,),
        }}}
        before = copy.deepcopy(monthly)
        with patch.dict(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, monthly, clear=True):
            december = effective_staff(employee, 2026, 12)
            january = effective_staff(employee, 2027, 1)
            self.assertEqual(rules.MONTHLY_EMPLOYEE_STORE_OVERRIDES, before)
        self.assertEqual(december["roles"]["主担当"], ["大宮すずらん通り店"])
        self.assertEqual(december["baseline"]["主担当"], [])
        self.assertEqual(january["roles"]["主担当"], [])
        self.assertIsNone(january["baseline"])
        self.assert_baseline(employee)

    def test_ledger_matches(self):
        ledger = json.loads((Path(__file__).resolve().parents[1] / "config/rule_ledger_v1_0.json").read_text())
        row = next(r for r in ledger["employee_store_suitability"] if r["氏名"] == "田中")
        self.assertEqual(row["主担当"], "")
        self.assertEqual(row["通常対応可"], "赤羽、すずらん、大宮")
        self.assertEqual(row["応援・巡回可"], "")
        self.assertEqual(row["絶対配置不可"], "東口、西口")


if __name__ == "__main__":
    unittest.main()
