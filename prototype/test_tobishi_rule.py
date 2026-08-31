from __future__ import annotations

import unittest
import json
from pathlib import Path
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype.generator import (
    TOBISHI_ECO_CORE_PENALTY,
    TOBISHI_OTHER_EMPLOYEE_PENALTY,
    _add_tobishi_pattern_indicators,
)
from prototype.employee_config import employee_from_dict
from prototype.models import Employee, MonthlyShift, ShiftAssignment, Store
from prototype.validator import ValidationResult, _check_tobishi_work_patterns


class TobishiRuleTest(unittest.TestCase):
    def test_employee_config_marks_only_the_six_eco_core_members(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "employees.json"
        with open(config_path, encoding="utf-8") as config_file:
            employees = json.load(config_file)["employees"]
        actual = {
            employee["name"]
            for employee in employees
            if employee.get("is_eco_core", False)
        }
        self.assertEqual(
            actual,
            {"今津", "土井", "楯", "春山", "下地", "長尾"},
        )

    def test_legacy_employee_config_migrates_core_members(self) -> None:
        self.assertTrue(employee_from_dict({"name": "今津"}).is_eco_core)
        self.assertFalse(employee_from_dict({"name": "鈴木"}).is_eco_core)
        self.assertFalse(
            employee_from_dict({"name": "今津", "is_eco_core": False}).is_eco_core
        )

    @staticmethod
    def _indicator_count(pattern: str) -> int:
        model = cp_model.CpModel()
        off_by_day = {
            day: model.NewBoolVar(f"off_{day}")
            for day in range(1, len(pattern) + 1)
        }
        for day, state in enumerate(pattern, start=1):
            model.Add(off_by_day[day] == (1 if state == "O" else 0))
        isolated_work = _add_tobishi_pattern_indicators(
            model,
            "テスト",
            off_by_day,
            len(pattern),
        )
        solver = cp_model.CpSolver()
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise AssertionError("テスト用パターンを解けませんでした")
        return sum(solver.Value(term) for term in isolated_work)

    @staticmethod
    def _shift(pattern: str) -> MonthlyShift:
        return MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                ShiftAssignment(
                    employee="テスト",
                    day=day,
                    store=Store.OFF if state == "O" else Store.OMIYA,
                )
                for day, state in enumerate(pattern, start=1)
            ],
        )

    def test_only_off_work_off_is_tobishi(self) -> None:
        # 出・出・休・出・休・出・出
        self.assertEqual(self._indicator_count("WWOWOWW"), 1)

    def test_work_off_work_is_not_tobishi(self) -> None:
        # 出・出・休・出・出・出・出
        self.assertEqual(self._indicator_count("WWOWWWW"), 0)

    def test_two_consecutive_work_days_are_not_tobishi(self) -> None:
        # 出・休・出・出・休・出・出
        self.assertEqual(self._indicator_count("WOWWOWW"), 0)

    def test_grouped_pattern_has_no_tobishi(self) -> None:
        # 出・出・出・休・休・出・出
        self.assertEqual(self._indicator_count("WWW OOWW".replace(" ", "")), 0)

    def test_core_penalty_is_stronger_than_other_staff_penalty(self) -> None:
        self.assertGreater(
            TOBISHI_ECO_CORE_PENALTY,
            TOBISHI_OTHER_EMPLOYEE_PENALTY,
        )
        self.assertGreater(TOBISHI_OTHER_EMPLOYEE_PENALTY, 0)

    def test_validator_warns_once_for_isolated_work(self) -> None:
        employee = Employee(name="テスト", is_eco_core=True)
        result = ValidationResult()
        with patch(
            "prototype.validator._validation_employees",
            return_value=[employee],
        ):
            _check_tobishi_work_patterns(
                self._shift("WWOWOWW"), result, 7,
            )
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, "WARNING")
        self.assertEqual(result.issues[0].category, "飛び石勤務")
        self.assertIn("4日", result.issues[0].message)

    def test_validator_does_not_warn_for_single_off_only(self) -> None:
        employee = Employee(name="テスト", is_eco_core=True)
        result = ValidationResult()
        with patch(
            "prototype.validator._validation_employees",
            return_value=[employee],
        ):
            _check_tobishi_work_patterns(
                self._shift("WWOWWWW"), result, 7,
            )
        self.assertEqual(result.issues, [])

    def test_validator_does_not_warn_for_non_core_employee(self) -> None:
        employee = Employee(name="テスト", is_eco_core=False)
        result = ValidationResult()
        with patch(
            "prototype.validator._validation_employees",
            return_value=[employee],
        ):
            _check_tobishi_work_patterns(
                self._shift("WWOWOWW"), result, 7,
            )
        self.assertEqual(result.issues, [])


if __name__ == "__main__":
    unittest.main()
