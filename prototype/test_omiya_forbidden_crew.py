"""大宮だけの禁止3名編成を、生成・検証・再調整・表示で一致させる。"""
import ast
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.employees import shift_active_employees
from prototype.generator import generate_shift, _add_omiya_forbidden_crew_constraints
from prototype.models import MonthlyShift, ShiftAssignment, Store, OperationMode
from prototype.rule_book import build_month
from prototype.rule_config import RuleConfig
from prototype.shift_chat import ShiftChatEngine, PendingShiftChange, SYSTEM_PROMPT
from prototype.shift_readjuster import new_protected_issues
from prototype.validator import ValidationResult, _check_forbidden_same_store_pairings, validate


class OmiyaForbiddenCrewTest(unittest.TestCase):
    def test_generator_and_validator_agree_for_every_crew_subset(self):
        names = (*rules.OMIYA_FORBIDDEN_ONLY_CREW, "黒澤")
        for flags in product((False, True), repeat=len(names)):
            for store in (Store.OMIYA, Store.AKABANE, Store.SUZURAN):
                with self.subTest(flags=flags, store=store):
                    model = cp_model.CpModel()
                    x = {n: {1: {Store.OMIYA: model.NewBoolVar(n)}} for n in names}
                    for name, present in zip(names, flags):
                        model.Add(x[name][1][Store.OMIYA] == int(present and store == Store.OMIYA))
                    _add_omiya_forbidden_crew_constraints(model, x, [1], list(names))
                    blocked = cp_model.CpSolver().Solve(model) == cp_model.INFEASIBLE
                    shift = MonthlyShift(2027, 1, assignments=[
                        ShiftAssignment(n, 1, store) for n, flag in zip(names, flags) if flag])
                    result = ValidationResult()
                    _check_forbidden_same_store_pairings(shift, result, 1)
                    errors = [i for i in result.issues if i.category == rules.OMIYA_FORBIDDEN_CREW_TITLE]
                    expected = store == Store.OMIYA and flags == (True, True, True, False)
                    self.assertEqual(blocked, expected)
                    self.assertEqual(bool(errors), expected)
                    if errors:
                        self.assertEqual(errors[0].severity, "ERROR")
        model = cp_model.CpModel()
        _add_omiya_forbidden_crew_constraints(model, {}, [1], [])
        self.assertEqual(cp_model.CpSolver().Solve(model), cp_model.OPTIMAL)

    def test_all_months_and_modes_even_when_anchor_requirement_is_relaxed(self):
        with patch.object(rules, "OMIYA_ANCHOR_RELAXED_MONTHS", tuple((2027, m) for m in range(1, 13))):
            for month in range(1, 13):
                for mode in (OperationMode.NORMAL, OperationMode.REDUCED, OperationMode.MINIMUM):
                    shift = MonthlyShift(2027, month, assignments=[
                        ShiftAssignment(n, 12, Store.OMIYA) for n in rules.OMIYA_FORBIDDEN_ONLY_CREW])
                    shift.operation_modes = {12: mode}
                    result = validate(shift)
                    errors = [i for i in result.issues if i.category == rules.OMIYA_FORBIDDEN_CREW_TITLE]
                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0].day, 12)

    def test_fourth_person_is_still_rejected_by_existing_capacity_rule(self):
        shift = MonthlyShift(2027, 1, assignments=[ShiftAssignment(n, 12, Store.OMIYA)
            for n in (*rules.OMIYA_FORBIDDEN_ONLY_CREW, "黒澤")])
        errors = validate(shift).issues
        self.assertTrue(any(i.category == "店舗人数上限" and i.day == 12 and i.severity == "ERROR"
                            and "大宮駅前店" in i.message for i in errors))

    def test_actual_generation_rejects_forced_trio_but_accepts_replacement(self):
        for forbidden in (False, True):
            base = {"今津": Store.AKABANE, "板倉": Store.AKABANE,
                    "土井": Store.HIGASHIGUCHI, "楯": Store.NISHIGUCHI,
                    "鈴木": Store.OMIYA, "田中": Store.OMIYA,
                    "岩野" if forbidden else "黒澤": Store.OMIYA,
                    "長尾": Store.SUZURAN, "野澤": Store.SUZURAN, "大類": Store.SUZURAN}
            modes = {d: OperationMode.NORMAL if d == 25 else OperationMode.CLOSED for d in range(1, 32)}
            requests = {e.name: [d for d in modes if d != 25 or e.name not in base]
                        for e in shift_active_employees(2026, 12) if e.name not in ("顧問", "山本")}
            required = [{"employee": n, "day": 25, "store": s, "severity": "ERROR"} for n, s in base.items()]
            diagnostics = {}
            with patch.object(rules, "OMIYA_ANCHOR_RELAXED_MONTHS", ((2026, 12),)):
                shift = generate_shift(2026, 12, requests, [], [], required_assignments=required,
                    operation_modes=modes, disable_month_edge_rules=True, strict_warning_constraints=False,
                    time_limit_seconds=5, verbose=False, status_out=diagnostics)
            self.assertEqual(shift is None, forbidden, diagnostics)
            self.assertEqual(diagnostics['omiya_forbidden_crew_rule'], rules.OMIYA_FORBIDDEN_CREW_DESCRIPTION)
            if shift:
                for name, off_days in requests.items():
                    for day in off_days:
                        self.assertEqual(shift.get_assignment(name, day).store, Store.OFF)

    def test_ai_preview_and_readjuster_reject_new_violation(self):
        shift = MonthlyShift(2027, 1, assignments=[
            ShiftAssignment(n, 12, store) for n, store in (
                ("鈴木", Store.OMIYA), ("田中", Store.OMIYA),
                ("黒澤", Store.OMIYA), ("岩野", Store.SUZURAN))])
        engine = ShiftChatEngine(shift, provider="local")
        engine.pending_changes = [PendingShiftChange("黒澤", 12, Store.SUZURAN),
                                  PendingShiftChange("岩野", 12, Store.OMIYA)]
        report = engine.inspect_pending_changes()
        self.assertIn(rules.OMIYA_FORBIDDEN_CREW_TITLE, [i.category for i in report.new_errors])
        trial = engine._apply_pending_to_shift()
        protected = new_protected_issues(validate(shift), validate(trial))
        self.assertIn(rules.OMIYA_FORBIDDEN_CREW_TITLE, [i.category for i in protected])
        self.assertIn("反映を停止", engine.apply_pending_changes(allow_new_warnings=True))
        self.assertEqual(shift.get_assignment("黒澤", 12).store, Store.OMIYA)
        self.assertIn(rules.OMIYA_FORBIDDEN_CREW_DESCRIPTION, SYSTEM_PROMPT)

    def test_settings_book_and_ledger_without_overwriting_saved_rules(self):
        path = Path(__file__).resolve().parents[1] / 'app/app.py'
        functions = {'load_rule_ledger_v1', 'build_effective_rule_visibility_rows'}
        nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in functions]
        saved = deepcopy(rules.load_monthly_exceptions_raw())
        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / 'ledger.json'
            old = {'rules': [{'分類': '参考', 'ルール': '担当者のメモ', '内容': '変更しない'}]}
            ledger_path.write_text(json.dumps(old, ensure_ascii=False))
            original = ledger_path.read_bytes()
            scope = {'RULE_LEDGER_FILE': ledger_path, 'json': json}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
            ledger = scope['load_rule_ledger_v1']()
            self.assertIn(old['rules'][0], ledger['rules'])
            self.assertEqual(ledger_path.read_bytes(), original)
            self.assertEqual(sum(r['ルール'] == rules.OMIYA_FORBIDDEN_CREW_TITLE for r in ledger['rules']), 1)
            for year, month in ((2026, 12), (2027, 1)):
                rows, _ = scope['build_effective_rule_visibility_rows'](year, month, {})
                entry = next(r for r in rows if r['ルール'] == rules.OMIYA_FORBIDDEN_CREW_TITLE)
                self.assertEqual(entry['適用範囲'], '全月固定')
                self.assertEqual(entry['強さ'], '絶対条件 / ERROR')
                book = build_month(year, month, RuleConfig(), [], ledger)
                self.assertEqual(sum(c['title'] == rules.OMIYA_FORBIDDEN_CREW_TITLE for c in book['store']), 1)
                self.assertFalse(any(c['title'] == rules.OMIYA_FORBIDDEN_CREW_TITLE for c in book['monthly']))
                self.assertTrue(any(rules.OMIYA_FORBIDDEN_CREW_DESCRIPTION in t
                                    for t in rules.active_code_managed_monthly_rules(year, month)))
                self.assertFalse(any(rules.OMIYA_FORBIDDEN_CREW_DESCRIPTION in t
                                     for t in rules.active_monthly_exception_descriptions(year, month)))
        self.assertEqual(saved, rules.load_monthly_exceptions_raw())


if __name__ == '__main__':
    unittest.main()
