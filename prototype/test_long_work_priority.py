"""5連勤の回避を大宮3名体制より強く評価し、絶対条件は変えない。"""
import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.generator import OMIYA_SHORTAGE_PENALTY
from prototype.models import Employee, MonthlyShift, ShiftAssignment, Store
from prototype.rule_config import RuleConfig
from prototype.rule_book import build_month
from prototype.validator import ValidationResult, _check_consecutive_work
from prototype.work_recovery import (
    LONG_WORK_AVOIDANCE_PENALTY, LONG_WORK_AVOIDANCE_DESCRIPTION,
    LONG_WORK_AVOIDANCE_TITLE, add_long_work_indicators, work_recovery_applies,
)


class LongWorkPriorityTest(unittest.TestCase):
    def solve_pattern(self, pattern, days=None):
        model = cp_model.CpModel()
        working = {d: model.NewBoolVar(f'work_{d}') for d in pattern}
        for day, value in pattern.items():
            model.Add(working[day] == int(value))
        indicators = add_long_work_indicators(model, working, days or max(pattern), 'staff')
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        return [d for d, term in indicators.items() if solver.Value(term)]

    def test_patterns_and_month_boundary_use_known_days_only(self):
        self.assertEqual(self.solve_pattern({d: d <= 4 for d in range(1, 8)}), [])
        self.assertEqual(self.solve_pattern({d: d <= 5 for d in range(1, 8)}), [5])
        self.assertEqual(self.solve_pattern({d: d <= 6 for d in range(1, 8)}), [5, 6])
        self.assertEqual(self.solve_pattern({d: d <= 1 for d in range(-3, 4)}), [1])
        self.assertEqual(self.solve_pattern({1: True, 2: False}), [])
        self.assertEqual(self.solve_pattern({d: d in set(range(1, 6)) | set(range(9, 14))
                                            for d in range(1, 15)}), [5, 13])

    def test_one_five_day_run_is_worse_than_one_omiya_two_person_day(self):
        def solve(force_five=False, requested_off=False):
            model = cp_model.CpModel()
            keep_five = model.NewBoolVar('keep_five')
            # 出勤日数は双方5日。5日目を7日目と交換する2案を比較する。
            work = {1: 1, 2: 1, 3: 1, 4: 1, 5: keep_five, 6: 0, 7: 1 - keep_five}
            indicators = add_long_work_indicators(model, work, 7, 'staff')
            two_person_day = 1 - keep_five
            if force_five:
                model.Add(keep_five == 1)
            if requested_off:
                model.Add(work[7] == 0)
            model.Minimize(LONG_WORK_AVOIDANCE_PENALTY * sum(indicators.values())
                           + OMIYA_SHORTAGE_PENALTY * two_person_day)
            solver = cp_model.CpSolver()
            self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
            return solver.Value(keep_five)
        self.assertEqual(solve(), 0)
        self.assertEqual(solve(force_five=True), 1)
        self.assertEqual(solve(requested_off=True), 1)
        self.assertGreater(LONG_WORK_AVOIDANCE_PENALTY, OMIYA_SHORTAGE_PENALTY)

    def test_all_regular_staff_not_only_two_names_or_december(self):
        for name in ('黒澤', '野澤', '今津', '南', '別のスタッフ'):
            for year, month in ((2026, 12), (2027, 1), (2027, 6)):
                self.assertTrue(work_recovery_applies(Employee(name=name), year, month))
        self.assertFalse(work_recovery_applies(Employee(name='補助', is_auxiliary=True), 2026, 12))

    def test_five_still_allowed_six_still_error(self):
        staff = Employee(name='黒澤')
        for days, error in ((5, False), (6, True)):
            shift = MonthlyShift(2027, 2, assignments=[
                ShiftAssignment('黒澤', d, Store.OMIYA if 10 <= d < 10 + days else Store.OFF)
                for d in range(1, 29)])
            result = ValidationResult()
            with patch('prototype.validator._validation_employees', return_value=[staff]):
                _check_consecutive_work(shift, result, 28, [], max_consec=5)
            self.assertEqual(any(i.severity == 'ERROR' for i in result.issues), error)

    def test_actual_generator_does_not_ban_forced_five_day_work(self):
        from prototype.test_december_2026_policy import DecemberGenerationTest
        shift, diagnostics = DecemberGenerationTest().fixture(list(range(8, 13)), event=False, yama_off=True)
        self.assertIsNotNone(shift, diagnostics)
        for name in ('黒澤', '野澤', '今津', '南'):
            self.assertIn(name, diagnostics['long_work_priority']['five_day_window_ends'])
        self.assertEqual(diagnostics['long_work_priority']['five_day_window_ends']['黒澤'], [12])
        self.assertEqual(diagnostics['long_work_priority']['hard_max_consecutive'], 5)

    def test_settings_book_ledger_and_ai_show_shared_fixed_goal(self):
        from prototype.shift_chat import SYSTEM_PROMPT
        path = Path(__file__).resolve().parents[1] / 'app/app.py'
        functions = {'load_rule_ledger_v1', 'build_numeric_ledger_rows_from_parameters',
                     'build_effective_rule_visibility_rows'}
        tree = ast.parse(path.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / 'ledger.json'
            old = {'rules': [{'分類': '参考', 'ルール': '担当者のメモ', '内容': '変更しない'}]}
            ledger_path.write_text(json.dumps(old, ensure_ascii=False))
            before = ledger_path.read_bytes()
            scope = {'RULE_LEDGER_FILE': ledger_path, 'json': json}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
            ledger = scope['load_rule_ledger_v1']()
            self.assertIn(old['rules'][0], ledger['rules'])
            self.assertEqual(ledger_path.read_bytes(), before)
            self.assertEqual(sum(r['ルール'] == LONG_WORK_AVOIDANCE_TITLE for r in ledger['rules']), 1)
            rows, _ = scope['build_effective_rule_visibility_rows'](2027, 1, {})
            goal = next(r for r in rows if r['ルール'] == LONG_WORK_AVOIDANCE_TITLE)
            self.assertEqual(goal['適用範囲'], '全月固定')
            self.assertEqual(goal['現在有効な内容'], LONG_WORK_AVOIDANCE_DESCRIPTION)
            self.assertTrue(any(r['項目'] == LONG_WORK_AVOIDANCE_TITLE
                                for r in scope['build_numeric_ledger_rows_from_parameters']({})))
        config = RuleConfig()
        saved = deepcopy(rules.load_monthly_exceptions_raw())
        for year, month in ((2026, 12), (2027, 1)):
            book = build_month(year, month, config, [], ledger)
            self.assertEqual(sum(c['title'] == LONG_WORK_AVOIDANCE_TITLE for c in book['basic']), 1)
            self.assertFalse(any(c['title'] == LONG_WORK_AVOIDANCE_TITLE for c in book['monthly']))
            self.assertTrue(any(LONG_WORK_AVOIDANCE_DESCRIPTION in text
                                for text in rules.active_code_managed_monthly_rules(year, month)))
        self.assertEqual(saved, rules.load_monthly_exceptions_raw())
        self.assertIn(LONG_WORK_AVOIDANCE_DESCRIPTION, SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
