"""10月25日承認ルールと11月限定設定。実データや外部APIには書き込まない。"""
import json
import ast
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.consecutive_counts import consecutive_count_label
from prototype.eco_support import (
    SKILL_UPDATE_KEY, SOLO_EXCEPTION_CATEGORY, OMIYA_PAIR_CATEGORY,
)
from prototype.employee_config import employee_from_dict, employee_to_dict
from prototype.employees import ALL_EMPLOYEES
from prototype.generator import (
    _add_eco_support_pairing_constraints, _add_omiya_anchor_pair_indicators,
    _solve_support_priority,
    generate_shift,
)
from prototype.models import Employee, MonthlyShift, ShiftAssignment, Skill, Store, Affinity, OperationMode
from prototype.monthly_store_update import prepare_november_store_update, NOVEMBER_UPDATE_ID
from prototype.shift_readjuster import new_protected_issues
from prototype.submission_loader import (
    parse_natural_language_note, SubmissionData, _apply_parsed_note_to_submission_data,
)
from prototype.validator import (
    ValidationResult, _check_eco_support_pairing, _check_omiya_anchor_pair,
    _check_consecutive_off, _check_store_capacity,
)


def solver(seconds):
    result = cp_model.CpSolver()
    result.parameters.max_time_in_seconds = seconds
    result.parameters.num_search_workers = 1
    return result


class FreeNoteUpdatesTest(unittest.TestCase):
    def test_ai_cannot_turn_no_preference_into_prohibition(self):
        from prototype.note_interpreter import validate_note_interpretation
        condition = dict(kind="off_streak_count", days=[], value=0, length=2,
                         store=None, comparison="min", evidence="連休希望しない")
        payload = {"conditions": [condition], "review_messages": []}
        result = validate_note_interpretation(payload, "連休希望しない", 2026, 11)
        self.assertEqual(result.corrected_text, "連休の希望なし。")
        condition.update(kind="max_off_streak", value=1, length=None, comparison=None)
        with self.assertRaisesRegex(ValueError, "連休禁止"):
            validate_note_interpretation(payload, "連休希望しない", 2026, 11)

    def test_bare_date_store_variants(self):
        for text in ("18日赤羽", "１８日　赤羽", "18日は赤羽駅前店", "18日赤羽希望"):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 11)
                self.assertEqual(parsed.work_requests, [(18, Store.AKABANE)])
        for text in ("18日赤羽不可", "18日赤羽ではない", "18日赤羽で子どもの行事", "田中は18日赤羽", "31日赤羽"):
            with self.subTest(text=text):
                self.assertFalse(parse_natural_language_note(text, 2026, 11).work_requests)

    def test_no_preference_is_not_ban(self):
        for text in ("連休希望しない", "連休不要", "連休の希望なし", "連休は希望しません"):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 11)
                self.assertTrue(parsed.has_constraints)
                self.assertIsNone(parsed.max_consecutive_off_days)
                self.assertIsNone(parsed.preferred_consecutive_off_days)
                self.assertEqual(parsed.consecutive_count_rules[0]["comparison"], "min")
                self.assertEqual(parsed.consecutive_count_rules[0]["count"], 0)
                self.assertIn("禁止しない", consecutive_count_label(parsed.consecutive_count_rules[0]))

    def test_explicit_ban(self):
        for text in ("連休なし", "2連休以上なし", "2連休以上は不可", "２連休不可"):
            with self.subTest(text=text):
                parsed = parse_natural_language_note(text, 2026, 11)
                self.assertEqual(parsed.max_consecutive_off_days, 1)
                self.assertFalse(parsed.consecutive_count_rules)

    def test_submission_and_correction_preserve_absolute_leave(self):
        data = SubmissionData(year=2026, month=11)
        data.off_requests = {"田中": [18]}
        parsed = parse_natural_language_note("18日赤羽\n連休希望しない", 2026, 11)
        _apply_parsed_note_to_submission_data(data, "田中", parsed, sync_remote=False)
        self.assertEqual(data.off_requests["田中"], [18])
        self.assertFalse(data.preferred_work_requests)
        self.assertEqual(data.consecutive_count_rules[0]["employee"], "田中")

    def test_no_preference_removes_minimum_warning_but_allows_two_days(self):
        employee = Employee(name="テスト", skill=Skill.ECO)
        parsed = parse_natural_language_note("連休希望しない", 2026, 11)
        counts = [dict(r, employee="テスト") for r in parsed.consecutive_count_rules]
        for off_days in ([2], [2, 3]):
            shift = MonthlyShift(year=2026, month=11, assignments=[
                ShiftAssignment("テスト", day, Store.OFF if day in off_days else Store.AKABANE)
                for day in range(1, 7)])
            result = ValidationResult()
            with patch("prototype.validator._validation_employees", return_value=[employee]):
                _check_consecutive_off(shift, result, 6, {}, consecutive_count_rules=counts)
            self.assertEqual(result.issues, [])

    def test_explicit_ban_validated_and_requested_leave_wins(self):
        employee = Employee(name="テスト", skill=Skill.ECO)
        shift = MonthlyShift(year=2026, month=11, assignments=[
            ShiftAssignment("テスト", day, Store.OFF if day in (2, 3) else Store.AKABANE)
            for day in range(1, 6)])
        for off_requests, expected in (({}, 1), ({"テスト": [2, 3]}, 0)):
            result = ValidationResult()
            with patch("prototype.validator._validation_employees", return_value=[employee]):
                _check_consecutive_off(shift, result, 5, off_requests, {"テスト": 1})
            self.assertEqual(len(result.issues), expected)
            self.assertFalse(any(i.category == "2連休不足" for i in result.issues))


class SupportUpdatesTest(unittest.TestCase):
    def model(self, name="田中", store=Store.AKABANE, independent=None):
        model = cp_model.CpModel()
        support = Employee(name=name, skill=Skill.ECO_SUPPORT)
        eco = Employee(name="独立エコ", skill=Skill.ECO)
        x = {e.name: {1: {store: model.NewBoolVar(e.name)}} for e in (support, eco)}
        exceptions = _add_eco_support_pairing_constraints(
            model, x, [1], [store], [eco], [support], allow_shortage_exceptions=True)
        model.Add(x[name][1][store] == 1)
        if independent is not None:
            model.Add(x[eco.name][1][store] == independent)
        # あえて単独を有利にする強大なソフト目標。通常解があれば採用しない。
        objective = -1000000 * x[eco.name][1][store]
        model.Maximize(objective)
        return model, objective, exceptions, x

    def test_skills_migrate_once_and_respect_later_admin_edits(self):
        for name in ("大類", "田中"):
            raw = {"name": name, "skill": "チケット", "full_name": "保持する氏名",
                   "affinities": {"AKABANE": "弱"}}
            employee = employee_from_dict(raw)
            self.assertEqual(employee.skill, Skill.ECO_SUPPORT)
            self.assertEqual(employee.full_name, raw["full_name"])
            self.assertFalse(employee.is_eco_core)
            saved = employee_to_dict(employee)
            self.assertTrue(saved[SKILL_UPDATE_KEY])
            saved["skill"] = "エコ"
            self.assertEqual(employee_from_dict(saved).skill, Skill.ECO)
        self.assertEqual(employee_from_dict({"name": "他", "skill": "チケット"}).skill, Skill.TICKET)

    def test_available_independent_eco_always_preferred_to_soft_goals(self):
        model, obj, exceptions, x = self.model()
        result, status, info = _solve_support_priority(model, obj, exceptions, solver, 3, allow_fallback=True)
        self.assertIn(status, (cp_model.FEASIBLE, cp_model.OPTIMAL))
        self.assertEqual(result.Value(x["独立エコ"][1][Store.AKABANE]), 1)
        self.assertFalse(info["fallback_used"])

    def test_fallback_minimizes_exception_days_before_other_goals(self):
        model = cp_model.CpModel()
        eco = Employee(name="独立エコ", skill=Skill.ECO)
        support = Employee(name="田中", skill=Skill.ECO_SUPPORT)
        x = {e.name: {d: {Store.AKABANE: model.NewBoolVar(f"{e.name}_{d}")}
                      for d in (1, 2)} for e in (eco, support)}
        for d in (1, 2):
            model.Add(x[support.name][d][Store.AKABANE] == 1)
        independent_days = sum(x[eco.name][d][Store.AKABANE] for d in (1, 2))
        model.Add(independent_days <= 1)
        exceptions = _add_eco_support_pairing_constraints(
            model, x, [1, 2], [Store.AKABANE], [eco], [support], allow_shortage_exceptions=True)
        objective = -1000000 * independent_days
        model.Maximize(objective)
        result, status, info = _solve_support_priority(model, objective, exceptions, solver, 3, allow_fallback=True)
        self.assertEqual(status, cp_model.OPTIMAL)
        self.assertTrue(info["fallback_used"])
        self.assertTrue(info["minimum_proven"])
        self.assertEqual(info["exception_count"], 1)
        self.assertEqual(sum(result.Value(v) for v in exceptions.values()), 1)

    def test_only_approved_stores_allow_shortage_fallback(self):
        for name in ("大類", "田中", "他"):
            for store in (Store.AKABANE, Store.SUZURAN, Store.OMIYA, Store.NISHIGUCHI, Store.HIGASHIGUCHI):
                with self.subTest(name=name, store=store):
                    model, obj, exceptions, _ = self.model(name, store, independent=0)
                    _, status, info = _solve_support_priority(model, obj, exceptions, solver, 3, allow_fallback=True)
                    allowed = (name == "大類" and store == Store.SUZURAN) or (name == "田中" and store in (Store.AKABANE, Store.SUZURAN))
                    self.assertEqual(status == cp_model.OPTIMAL, allowed)
                    self.assertEqual(info["fallback_used"], allowed)
                    if allowed:
                        self.assertEqual(info["normal_status"], "INFEASIBLE")
                        self.assertTrue(info["minimum_proven"])

    def test_warning_constraints_cannot_trigger_exception(self):
        model, obj, exceptions, _ = self.model(independent=0)
        _, status, info = _solve_support_priority(model, obj, exceptions, solver, 3, allow_fallback=False)
        self.assertEqual(status, cp_model.INFEASIBLE)
        self.assertFalse(info["fallback_used"])

    def test_timeout_never_enables_exception(self):
        model, obj, exceptions, _ = self.model()
        fake = Mock()
        fake.Solve.return_value = cp_model.UNKNOWN
        fake.StatusName.return_value = "UNKNOWN"
        factory = Mock(return_value=fake)
        _, status, info = _solve_support_priority(model, obj, exceptions, factory, 3, allow_fallback=True)
        self.assertEqual(status, cp_model.UNKNOWN)
        self.assertFalse(info["fallback_used"])
        factory.assert_called_once()

    def test_validator_and_readjustment_protect_new_exceptions(self):
        support = Employee(name="田中", skill=Skill.ECO_SUPPORT)
        for store, severity in ((Store.AKABANE, "WARNING"), (Store.OMIYA, "ERROR")):
            shift = MonthlyShift(year=2026, month=11, assignments=[ShiftAssignment("田中", 1, store)])
            result = ValidationResult()
            with patch("prototype.validator.get_employee", return_value=support):
                _check_eco_support_pairing(shift, result, 1)
            self.assertEqual(result.issues[0].severity, severity)
            self.assertEqual(len(new_protected_issues(ValidationResult(), result)), 1)

    def test_exception_does_not_allow_one_person_store(self):
        support = Employee(name="田中", skill=Skill.ECO_SUPPORT)
        shift = MonthlyShift(year=2026, month=11, assignments=[ShiftAssignment("田中", 1, Store.SUZURAN)])
        result = ValidationResult()
        with patch("prototype.validator.get_employee", return_value=support):
            _check_store_capacity(shift, result, 1, allow_omiya_short=True)
        self.assertTrue(any(i.severity == "ERROR" and "すずらん" in i.message for i in result.issues))

    def test_real_generator_fallback_keeps_staffing_and_requested_leave(self):
        from prototype.employees import shift_active_employees
        day = 3
        modes = {d: OperationMode.CLOSED for d in range(1, 31)}
        modes[day] = OperationMode.NORMAL
        staff = shift_active_employees(2026, 11)
        for shortage in (False, True):
            available = ({"下地", "土井", "楯", "板倉", "岩野", "黒澤", "野澤", "南", "大塚", "田中", "大類"}
                         if shortage else {"下地", "春山", "今津", "土井", "楯", "長尾", "板倉", "岩野", "黒澤", "野澤", "田中", "大類"})
            requests = {
                e.name: [d for d in modes if d != day or e.name not in available]
                for e in staff
            }
            diagnostics = {}
            shift = generate_shift(
                2026, 11, requests, [("南", day, None)], [], operation_modes=modes,
                required_assignments=[
                    {"employee": "田中", "day": day, "store": Store.AKABANE, "severity": "ERROR"},
                    {"employee": "大類", "day": day, "store": Store.SUZURAN, "severity": "ERROR"},
                ], disable_month_edge_rules=True, strict_warning_constraints=False,
                time_limit_seconds=5, verbose=False, status_out=diagnostics,
            )
            self.assertIsNotNone(shift, diagnostics)
            self.assertEqual(diagnostics["eco_support_search"]["fallback_used"], shortage)
            self.assertEqual(len(diagnostics["eco_support_solo_days"]), 2 if shortage else 0)
            for name, days in requests.items():
                self.assertTrue(all(shift.get_assignment(name, d).store == Store.OFF for d in days))
            result = ValidationResult()
            _check_store_capacity(shift, result, day, allow_omiya_short=True)
            _check_eco_support_pairing(shift, result, day)
            self.assertFalse(any(i.severity == "ERROR" for i in result.issues), result.issues)


class VisibilityTest(unittest.TestCase):
    def test_settings_and_book_include_new_rules(self):
        from prototype.rule_book import build_month
        from prototype.rule_config import RuleConfig
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "app/app.py").read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "build_effective_rule_visibility_rows"]
        scope = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
        rows, _ = scope["build_effective_rule_visibility_rows"](2026, 11, {})
        text = json.dumps(rows, ensure_ascii=False)
        self.assertIn("同伴", text)
        self.assertIn("下地・春山だけ", text)
        ledger = json.loads((root / "config/rule_ledger_v1_0.json").read_text())
        book = build_month(2026, 11, RuleConfig(), list(ALL_EMPLOYEES), ledger, None)
        text = json.dumps(book, ensure_ascii=False)
        self.assertIn("不足時", text)
        self.assertIn("3名体制", text)


class OmiyaPairTest(unittest.TestCase):
    def test_two_only_not_three_or_different_pair(self):
        for names, expected in ((["下地", "春山"], 1), (["下地", "春山", "黒澤"], 0), (["下地", "黒澤"], 0)):
            model = cp_model.CpModel()
            x = {name: {1: {Store.OMIYA: model.NewBoolVar(name)}} for name in ("下地", "春山", "黒澤")}
            for name in x:
                model.Add(x[name][1][Store.OMIYA] == int(name in names))
            terms = _add_omiya_anchor_pair_indicators(model, x, [1], list(x))
            result = solver(3)
            self.assertEqual(result.Solve(model), cp_model.OPTIMAL)
            self.assertEqual(result.Value(terms[0]), expected)
            shift = MonthlyShift(year=2026, month=11, assignments=[ShiftAssignment(name, 1, Store.OMIYA) for name in names])
            validation = ValidationResult()
            _check_omiya_anchor_pair(shift, validation, 1)
            self.assertEqual(len(validation.issues), expected)
            if expected:
                self.assertEqual(validation.issues[0].category, OMIYA_PAIR_CATEGORY)
                self.assertEqual(validation.issues[0].severity, "WARNING")


class NovemberStoreUpdateTest(unittest.TestCase):
    def setUp(self):
        names = ("OMIYA_ANCHOR_RELAXED_MONTHS", "MONTHLY_AVOID_SAME_OFF_RULES",
                 "MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES", "MONTHLY_OPERATION_MODES",
                 "TANAKA_PAIR_TRAINING_RULES", "MONTHLY_TRAINING_PLANS",
                 "MONTHLY_EMPLOYEE_STORE_OVERRIDES", "MONTHLY_YAMAMOTO_POLICIES",
                 "MONTHLY_EXCEPTIONS_STATUS")
        self.saved_globals = {name: deepcopy(getattr(rules, name)) for name in names}

    def tearDown(self):
        for name, value in self.saved_globals.items():
            setattr(rules, name, value)

    def test_month_scope_and_repeat_preserve_edits(self):
        original = {"employee_store_overrides": {"2026-10": {"田中": {"primary_store": "SUZURAN"}},
                    "2026-12": {"岩野": {"primary_store": "AKABANE"}}}, "operation_modes": {"2026-11": {"1": "通常"}}}
        before = deepcopy(original)
        updated, changed = prepare_november_store_update(original, ALL_EMPLOYEES)
        self.assertTrue(changed)
        self.assertEqual(original, before)
        self.assertEqual(updated["operation_modes"], original["operation_modes"])
        self.assertEqual(rules.validate_monthly_exceptions_data(updated)[0], [])
        for month in ("2026-10", "2026-12"):
            self.assertEqual(updated["employee_store_overrides"][month], original["employee_store_overrides"][month])
        with patch.object(rules, "MONTHLY_EMPLOYEE_STORE_OVERRIDES", {}):
            with tempfile.TemporaryDirectory() as td, patch.object(rules, "MONTHLY_EXCEPTIONS_FILE", Path(td) / "rules.json"):
                rules.MONTHLY_EXCEPTIONS_FILE.write_text(json.dumps(updated), encoding="utf-8")
                rules.reload_monthly_exceptions()
                tanaka = next(e for e in ALL_EMPLOYEES if e.name == "田中")
                effective = rules.effective_employee_store_affinities(tanaka, 2026, 11)
                for store in (Store.AKABANE, Store.OMIYA, Store.SUZURAN):
                    self.assertEqual(effective[store], Affinity.MEDIUM)
                self.assertEqual(effective[Store.NISHIGUCHI], Affinity.NONE)
                self.assertEqual(rules.effective_employee_store_affinities(tanaka, 2026, 12), tanaka.affinities)
                self.assertEqual(rules.monthly_employee_store_override(2026, 11, "岩野")["primary_store"], Store.SUZURAN)
        del updated["employee_store_overrides"]["2026-11"]["田中"]
        repeated, changed = prepare_november_store_update(updated, ALL_EMPLOYEES)
        self.assertFalse(changed)
        self.assertEqual(repeated, updated)
        self.assertIn(NOVEMBER_UPDATE_ID, repeated["applied_updates"])


if __name__ == "__main__":
    unittest.main()
