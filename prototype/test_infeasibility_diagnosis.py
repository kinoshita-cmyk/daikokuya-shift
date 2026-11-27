"""診断中の設定保護・複数条件の衝突・時間切れの区別を確認する。"""
import ast
from copy import deepcopy
from functools import wraps
from pathlib import Path
import unittest
from unittest.mock import patch

from ortools.sat.python import cp_model

from prototype import rules
from prototype.employees import shift_active_employees
from prototype.generator import generate_shift
from prototype.infeasibility_diagnosis import (
    probe_rule_relaxations, run_generation_attempt, summarize_probe_results,
)
from prototype.models import OperationMode, Store


class InfeasibilityDiagnosisTest(unittest.TestCase):
    def base(self):
        return {"year": 2026, "month": 12, "off_requests": {"本人": [8]},
                "work_requests": [("南", 2, None)], "prev_month": [],
                "holiday_overrides": {"本人": 9}, "flexible_off": [("本人", [1, 2], 1)],
                "max_consec_override": 5, "advisor_max_days": 3,
                "required_assignments": [{"employee": "本人", "day": 3, "severity": "ERROR"}],
                "strict_warning_constraints": True, "status_out": {"old": True}}

    def solver_stub(self, outcome, calls):
        @wraps(generate_shift)
        def fake(**kwargs):
            calls.append(deepcopy(kwargs))
            status = outcome(kwargs) if callable(outcome) else outcome
            kwargs["status_out"].update(status=status, wall_time_seconds=0.01)
            return object() if status in ("FEASIBLE", "OPTIMAL") else None
        return fake

    def test_pair_conflict_is_found_without_weakening_leave_or_mutating_input(self):
        base = self.base()
        before = deepcopy(base)
        calls = []

        def outcome(kwargs):
            # 片方だけを外しても成立しない2つの独立した矛盾を実ソルバーで確認する。
            model = cp_model.CpModel()
            for active in (bool(kwargs.get("required_assignments")),
                           "suzuran_backup" not in kwargs.get("_diagnostic_relaxations", set())):
                value = model.NewBoolVar("conflict")
                model.Add(value == 0)
                if active:
                    model.Add(value == 1)
            solver = cp_model.CpSolver()
            return solver.StatusName(solver.Solve(model))

        with patch("prototype.generator.generate_shift", self.solver_stub(outcome, calls)):
            rows = probe_rule_relaxations(base)
        solved = [row for row in rows if row["判定"] == "OPTIMAL"]
        self.assertEqual(len(solved), 1)
        self.assertEqual(solved[0]["試算区分"], "2条件の組み合わせ")
        self.assertIn("長尾不在時", solved[0]["外した条件"])
        self.assertIn("月別の日付指定配置", solved[0]["外した条件"])
        self.assertEqual(base, before)
        for call in calls:
            for key in ("off_requests", "work_requests", "prev_month", "holiday_overrides",
                        "flexible_off", "max_consec_override", "advisor_max_days"):
                self.assertEqual(call[key], base[key], key)
            self.assertFalse(call["strict_warning_constraints"])
        self.assertNotIn("この条件は原因ではない", str(rows))
        self.assertIn("自動適用はしていません", summarize_probe_results(rows))

    def test_existing_feasible_conditions_do_not_trigger_relaxations(self):
        calls = []
        with patch("prototype.generator.generate_shift", self.solver_stub("FEASIBLE", calls)):
            rows = probe_rule_relaxations(self.base())
        self.assertEqual(len(calls), 1)
        self.assertNotIn("_diagnostic_relaxations", calls[0])
        self.assertIn("ルール変更は不要", summarize_probe_results(rows))

    def test_unknown_and_invalid_are_not_reported_as_infeasible(self):
        for status in ("UNKNOWN", "MODEL_INVALID"):
            calls = []
            with patch("prototype.generator.generate_shift", self.solver_stub(status, calls)):
                rows = probe_rule_relaxations(self.base(), max_pair_probes=0)
            if status == "MODEL_INVALID":
                self.assertEqual(len(calls), 1)
                self.assertIn("プログラム", summarize_probe_results(rows))
            else:
                self.assertTrue(all(row["判定"] == "UNKNOWN" for row in rows))
                self.assertIn("成立不能の証明ではありません", summarize_probe_results(rows))

    def test_program_exception_is_not_a_rule_conflict(self):
        @wraps(generate_shift)
        def broken(**kwargs):
            raise ValueError("bad model")
        with patch("prototype.generator.generate_shift", broken):
            rows = probe_rule_relaxations(self.base())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["判定"], "ERROR")
        self.assertIn("プログラム", summarize_probe_results(rows))

    def test_generation_attempts_keep_original_and_final_results(self):
        attempts, kwargs = [], self.base()
        counter = iter(("INFEASIBLE", "UNKNOWN", "FEASIBLE"))
        fake = self.solver_stub(lambda kw: next(counter), [])
        for label in ("通常", "警告条件だけ緩和", "緊急"):
            run_generation_attempt(fake, kwargs, attempts, label)
        self.assertEqual([row["状態"] for row in attempts], ["INFEASIBLE", "UNKNOWN", "FEASIBLE"])
        self.assertEqual(kwargs["status_out"]["status"], "FEASIBLE")
        self.assertNotIn("old", kwargs["status_out"])
        self.assertNotIn("off_requests", str(attempts))

    def test_time_budget_can_stop_without_running_trials(self):
        calls = []
        with patch("prototype.generator.generate_shift", self.solver_stub("INFEASIBLE", calls)):
            rows = probe_rule_relaxations(self.base(), total_time_limit=0)
        self.assertEqual(rows, [])
        self.assertEqual(calls, [])

    def test_management_screen_displays_distinct_failure_reasons(self):
        from streamlit.testing.v1 import AppTest

        source = (Path(__file__).resolve().parents[1] / "app/app.py").read_text(encoding="utf-8")
        node = next(node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                    and node.test.id == "_attempts")
        block = ast.get_source_segment(source, node)
        # get_source_segmentの先頭行以外のインデントを揃える。
        lines = block.splitlines()
        block = "\n".join([lines[0], *[line[node.col_offset:] for line in lines[1:]]])
        script = """
import streamlit as st
import pandas as pd
_gen_status = "partial"
_attempts = [
    {"段階": "通常条件", "状態": "INFEASIBLE", "経過秒": 0.1, "上限秒": 600, "顧問上限": 3},
    {"段階": "緊急試算", "状態": "UNKNOWN", "経過秒": 20, "上限秒": 20, "顧問上限": 6},
]
""" + block
        app = AppTest.from_string(script).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value["判定"].tolist(), ["条件が両立しない", "時間内に判定できず"])

    def test_outdated_generator_does_not_silently_skip_new_diagnostics(self):
        def old_generator(year, month, off_requests, work_requests, prev_month,
                          required_assignments=None, disable_month_edge_rules=False,
                          status_out=None, time_limit_seconds=20, strict_warning_constraints=True, verbose=False):
            status_out["status"] = "INFEASIBLE"
            return None
        with patch("prototype.generator.generate_shift", old_generator):
            rows = probe_rule_relaxations(self.base(), max_pair_probes=0)
        suzuran = next(row for row in rows if "長尾不在時" in row["外した条件"])
        self.assertEqual(suzuran["判定"], "ERROR")

    def test_diagnostic_flags_require_explicit_reporting_and_known_names(self):
        base = {"year": 2026, "month": 12, "off_requests": {}, "work_requests": [], "prev_month": []}
        with self.assertRaises(ValueError):
            generate_shift(**base, _diagnostic_relaxations={"suzuran_backup"})
        with self.assertRaises(ValueError):
            generate_shift(**base, status_out={}, _diagnostic_relaxations={"all_rules"})

    def test_actual_suzuran_conflict_is_reversible_and_does_not_change_saved_rules(self):
        base = {"鈴木": Store.AKABANE, "田中": Store.AKABANE, "板倉": Store.AKABANE,
                "土井": Store.HIGASHIGUCHI, "楯": Store.NISHIGUCHI,
                "春山": Store.OMIYA, "下地": Store.OMIYA, "大類": Store.OMIYA,
                "今津": Store.SUZURAN, "野澤": Store.SUZURAN, "岩野": Store.SUZURAN}
        staff = shift_active_employees(2026, 12)
        off = {e.name: [day for day in range(1, 32) if day != 5 or e.name not in base] for e in staff}
        kwargs = dict(year=2026, month=12, off_requests=off, work_requests=[], prev_month=[],
                      operation_modes={day: OperationMode.NORMAL if day == 5 else OperationMode.CLOSED
                                       for day in range(1, 32)},
                      required_assignments=[{"employee": name, "day": 5, "store": store, "severity": "ERROR"}
                                            for name, store in base.items()],
                      disable_month_edge_rules=True, strict_warning_constraints=False,
                      time_limit_seconds=3, verbose=False)
        original = deepcopy(rules.load_monthly_exceptions_raw())
        for flags, expected in ((set(), False), ({"suzuran_backup"}, True), (set(), False)):
            status = {}
            shift = generate_shift(**kwargs, _diagnostic_relaxations=flags, status_out=status)
            self.assertEqual(shift is not None, expected, status)
            self.assertEqual(status["diagnostic_relaxations"], sorted(flags))
            if shift:
                self.assertEqual(shift.get_assignment("長尾", 5).store, Store.OFF)
                for name, store in base.items():
                    self.assertEqual(shift.get_assignment(name, 5).store, store)
        self.assertEqual(rules.load_monthly_exceptions_raw(), original)


if __name__ == "__main__":
    unittest.main()
