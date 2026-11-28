"""臨時応援を既存設定を壊さず追加し、配置可能日を全経路で守る。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from prototype import employees, rules
from prototype.capacity_balance import compute_monthly_capacity_balance
from prototype.employee_config import EmployeeConfigManager, employee_to_dict
from prototype.excel_exporter import export_shift_to_excel, export_column_widths
from prototype.export_config import export_column_order
from prototype.models import MonthlyShift, ShiftAssignment, Store, Skill, OperationMode
from prototype.pdf_exporter import export_shift_to_pdf
from prototype.rule_book import monthly_cards
from prototype.shift_chat import ShiftChatEngine, PendingShiftChange
from prototype.validator import validate


class DecemberTemporaryStaffTest(unittest.TestCase):
    def setUp(self):
        self.staff = deepcopy(employees.ALL_EMPLOYEES)
        self.kasuya = next(e for e in self.staff if e.name == '粕谷')
        for target, value in (
            ('prototype.employees._dynamic_employee_map', {e.name: e for e in self.staff}),
            ('prototype.employee_config.get_all_employees_including_retired', self.staff),
        ):
            context = patch(target, return_value=value)
            context.start()
            self.addCleanup(context.stop)

    def test_ticket_three_stores_only_and_no_full_time_target(self):
        self.assertEqual(self.kasuya.skill, Skill.TICKET)
        self.assertIsNone(self.kasuya.annual_target_days)
        self.assertIsNone(self.kasuya.hired_at)
        self.assertTrue(self.kasuya.only_on_request_days)
        self.assertEqual({s for s, a in self.kasuya.affinities.items() if a.value != '不可'},
                         {Store.OMIYA, Store.SUZURAN, Store.AKABANE})
        self.assertEqual(employees.limited_staff_work_days('粕谷', 2026, 12), {13, 19, 26})

    def test_only_target_month_roster_and_export_columns(self):
        for year, month, included in ((2026, 11, False), (2026, 12, True), (2027, 1, False), (2027, 12, False)):
            self.assertEqual('粕谷' in {e.name for e in employees.shift_active_employees(year, month)}, included)
            self.assertEqual('粕谷' in export_column_order(year, month), included)
        self.assertIn('粕谷', export_column_order(2027, 1, [ShiftAssignment('粕谷', 13, Store.OMIYA)]))

    def test_existing_master_kept_and_addition_idempotent_without_disk_write(self):
        with tempfile.TemporaryDirectory() as td, \
             patch('prototype.github_backup.sync_latest_config_from_github'), \
             patch('prototype.github_backup.push_config_to_github', return_value=(True, 'test')):
            manager = EmployeeConfigManager(Path(td))
            original = {'version': 1, 'updated_at': '2026-11-27',
                        'employees': [employee_to_dict(e) for e in self.staff if e.name != '粕谷']}
            original['employees'][0]['notes'] = '管理者の最新設定を保持'
            manager.config_file.write_text(json.dumps(original, ensure_ascii=False))
            before = manager.config_file.read_bytes()
            for _ in range(2):
                loaded = manager.load_all()
                self.assertEqual(sum(e.name == '粕谷' for e in loaded), 1)
                self.assertEqual(loaded[0].notes, '管理者の最新設定を保持')
                self.assertEqual(manager.config_file.read_bytes(), before)
            manager._save_all(loaded, 'test')
            loaded = manager.load_all()
            self.assertEqual(sum(e.name == '粕谷' for e in loaded), 1)
            # 保存済みの変更・削除を、再起動で元に戻さない。
            loaded[-1].notes = '担当者の追記'
            manager._save_all(loaded, 'test')
            self.assertEqual(manager.load_all()[-1].notes, '担当者の追記')
            manager._save_all([e for e in loaded if e.name != '粕谷'], 'test')
            self.assertNotIn('粕谷', [e.name for e in manager.load_all()])

    def test_validator_and_ai_reject_other_days_and_months(self):
        for year, month, day, allowed in ((2026, 12, 13, True), (2026, 12, 19, True),
                                         (2026, 12, 26, True), (2026, 12, 14, False),
                                         (2027, 1, 13, False)):
            shift = MonthlyShift(year, month, assignments=[ShiftAssignment('粕谷', day, Store.OMIYA)])
            issues = validate(shift).issues
            self.assertEqual(any(i.category == '出勤可能日外' for i in issues), not allowed)
            self.assertFalse(any(i.employee == '粕谷' and i.category in
                                 ('3連休', '2連休過多', '2連休不足', '月間勤務日数不足') for i in issues))
            engine = ShiftChatEngine(shift, provider='local')
            messages = engine._structural_change_violation_messages([PendingShiftChange('粕谷', day, Store.OMIYA)])
            self.assertEqual(bool(messages), not allowed)
            self.assertFalse(engine._structural_change_violation_messages([PendingShiftChange('粕谷', day, Store.OFF)]))
        bad = MonthlyShift(2026, 12, assignments=[ShiftAssignment('粕谷', 13, Store.NISHIGUCHI)])
        self.assertTrue(any(i.employee == '粕谷' and i.category == '絶対配置不可' for i in validate(bad).issues))

    def test_book_and_supply_share_three_day_limit(self):
        before = deepcopy(rules.load_monthly_exceptions_raw())
        cards = [c for c in monthly_cards(2026, 12) if c['id'] == 'monthly:limited-staff:粕谷']
        self.assertEqual(len(cards), 1)
        self.assertIn('13・19・26', cards[0]['text'])
        self.assertEqual(cards[0]['scope'], '2026-12')
        self.assertFalse(any('粕谷' in c['title'] for c in monthly_cards(2027, 1)))
        summary = compute_monthly_capacity_balance(2026, 12, [self.kasuya], {})
        self.assertEqual(summary['supply_total'], 3)
        self.assertEqual(summary['eco_supply'], 0)
        self.assertEqual(before, rules.load_monthly_exceptions_raw())

    def test_excel_pdf_include_new_column(self):
        previous_names = export_column_order(2026, 11)
        current_names = export_column_order(2026, 12)
        def widths_by_name(names):
            widths = export_column_widths(names)
            return {name: widths[openpyxl.utils.get_column_letter(4 + i)] for i, name in enumerate(names)}
        previous_widths, current_widths = widths_by_name(previous_names), widths_by_name(current_names)
        for name in previous_names:
            self.assertEqual(previous_widths[name], current_widths[name])
        self.assertEqual(current_widths['粕谷'], 13.0)
        shift = MonthlyShift(2026, 12, assignments=[ShiftAssignment('粕谷', 13, Store.OMIYA)])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'shift.xlsx'
            export_shift_to_excel(shift, path)
            book = openpyxl.load_workbook(path)
            self.assertIn('粕谷', [c.value for c in book.active[7]])
            book.close()
            pdf = Path(td) / 'shift.pdf'
            export_shift_to_pdf(shift, pdf)
            self.assertGreater(pdf.stat().st_size, 1000)

    def test_actual_generator_with_and_without_requested_leave(self):
        from prototype.generator import generate_shift
        for blocked, forced_day, forced_store in ((False, None, None), (True, None, None),
                                                  (False, 15, Store.OMIYA),
                                                  (False, 13, Store.NISHIGUCHI)):
            open_days = {13, 19, 26} | ({forced_day} if forced_day else set())
            modes = {d: OperationMode.NORMAL if d in open_days else OperationMode.CLOSED
                     for d in range(1, 32)}
            base = {'今津': Store.AKABANE, '板倉': Store.AKABANE, '田中': Store.AKABANE,
                    '土井': Store.HIGASHIGUCHI, '楯': Store.NISHIGUCHI,
                    '春山': Store.OMIYA, '黒澤': Store.OMIYA,
                    '長尾': Store.SUZURAN, '鈴木': Store.SUZURAN, '岩野': Store.SUZURAN}
            requests = {e.name: [d for d in modes if d not in open_days or e.name not in base]
                        for e in employees.shift_active_employees(2026, 12) if e.name != '粕谷'}
            requests['粕谷'] = [13, 19, 26] if blocked else []
            requests['山本'] = list(modes)
            required = [dict(employee=n, day=d, store=s, severity='ERROR')
                        for d in open_days for n, s in base.items()]
            if forced_day:
                required.append(dict(employee='粕谷', day=forced_day, store=forced_store, severity='ERROR'))
            if blocked:
                # 本人×を優先し、別のチケット担当で埋める。
                requests['大類'] = [d for d in modes if d not in (13, 19, 26)]
                required += [dict(employee='大類', day=d, store=Store.OMIYA, severity='ERROR')
                             for d in (13, 19, 26)]
            status = {}
            shift = generate_shift(2026, 12, requests, [('粕谷', 14, None)], [],
                operation_modes=modes, required_assignments=required,
                disable_month_edge_rules=True, strict_warning_constraints=False,
                advisor_max_days=0, time_limit_seconds=5, verbose=False, status_out=status)
            if forced_day:
                self.assertIsNone(shift, status)
                continue
            self.assertIsNotNone(shift, status)
            actual = {a.day for a in shift.assignments if a.employee == '粕谷' and a.store != Store.OFF}
            self.assertEqual(actual, set() if blocked else {13, 19, 26})
            for a in shift.assignments:
                if a.employee == '粕谷' and a.store != Store.OFF:
                    self.assertIn(a.store, (Store.OMIYA, Store.SUZURAN, Store.AKABANE))


if __name__ == '__main__':
    unittest.main()
