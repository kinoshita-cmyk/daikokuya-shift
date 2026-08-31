from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.shift_chat import (
    PendingSafetyReport,
    PendingShiftChange,
    ShiftChatEngine,
)
from prototype.validator import Issue, ValidationResult


class _FakeResponses:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(
                id="response-1",
                output=[SimpleNamespace(
                    type="function_call",
                    name="validate_current",
                    arguments=json.dumps({}),
                    call_id="call-1",
                )],
                output_text="",
            )
        return SimpleNamespace(
            id="response-2",
            output=[],
            output_text="確認結果です。",
        )


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


class _ScriptedResponses:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ShiftChatOpenAITest(unittest.TestCase):
    @staticmethod
    def _safe_report() -> PendingSafetyReport:
        return PendingSafetyReport([], [], [], [])

    def _engine(self) -> tuple[ShiftChatEngine, _FakeOpenAIClient]:
        fake_client = _FakeOpenAIClient()
        shift = MonthlyShift(
            year=2026,
            month=9,
            assignments=[ShiftAssignment("山本", 1, Store.AKABANE)],
        )
        with patch("prototype.shift_chat.OpenAI", return_value=fake_client):
            engine = ShiftChatEngine(
                shift,
                api_key="test-key",
                provider="openai",
                model="test-model",
            )
        return engine, fake_client

    def test_openai_responses_function_call_is_executed(self) -> None:
        engine, fake_client = self._engine()
        with patch.object(engine, "_execute_tool", return_value="検証OK") as execute:
            result = engine.chat("確認して")

        self.assertEqual(result, "確認結果です。")
        execute.assert_called_once_with("validate_current", {})
        self.assertEqual(fake_client.responses.calls[0]["model"], "test-model")
        self.assertTrue(fake_client.responses.calls[0]["tools"])
        self.assertEqual(
            fake_client.responses.calls[1]["input"][0]["type"],
            "function_call_output",
        )

    def test_pending_none_store_removes_assignment_only_after_apply(self) -> None:
        engine, _ = self._engine()
        engine.pending_changes.append(PendingShiftChange("山本", 1, None))

        self.assertIsNotNone(engine.shift.get_assignment("山本", 1))
        self.assertIsNone(engine.get_preview_shift().get_assignment("山本", 1))
        with patch.object(
            engine, "inspect_pending_changes", return_value=self._safe_report()
        ):
            engine.apply_pending_changes()
        self.assertIsNone(engine.shift.get_assignment("山本", 1))

    def test_redo_restores_preview_before_reapplying(self) -> None:
        engine, _ = self._engine()
        engine.pending_changes.append(PendingShiftChange(
            "山本", 1, Store.OMIYA
        ))
        with patch.object(
            engine, "inspect_pending_changes", return_value=self._safe_report()
        ):
            engine.apply_pending_changes()
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.OMIYA,
        )

        undo_message = engine.undo_last_apply()
        self.assertIn("青枠プレビュー", undo_message)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.AKABANE,
        )
        self.assertEqual(engine.get_pending_change_count(), 1)
        self.assertEqual(
            engine.get_preview_shift().get_assignment("山本", 1).store,
            Store.OMIYA,
        )

        back_message = engine.undo_last_apply()
        self.assertIn("修正前", back_message)
        self.assertEqual(engine.get_pending_change_count(), 0)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.AKABANE,
        )

        message = engine.redo_last_apply()

        self.assertIn("プレビューを復元", message)
        self.assertEqual(engine.get_pending_change_count(), 1)
        self.assertTrue(engine.redo_preview_active)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.AKABANE,
        )
        self.assertEqual(
            engine.get_preview_shift().get_assignment("山本", 1).store,
            Store.OMIYA,
        )

        with patch.object(
            engine, "inspect_pending_changes", return_value=self._safe_report()
        ):
            engine.apply_pending_changes()
        self.assertFalse(engine.redo_preview_active)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.OMIYA,
        )

    def test_local_provider_keeps_rule_based_adjustments_available(self) -> None:
        shift = MonthlyShift(year=2026, month=9)
        engine = ShiftChatEngine(shift, provider="local")
        self.assertIn("APIキー", engine.chat("店舗人数を調整して"))

    def test_tobishi_request_uses_exact_engine_without_model_judgment(self) -> None:
        engine, fake_client = self._engine()
        with patch.object(
            engine,
            "_tool_optimize_tobishi",
            return_value="再最適化結果",
        ) as optimize:
            result = engine.chat("今津と春山の飛び石勤務を減らして")

        self.assertEqual(result, "再最適化結果")
        optimize.assert_called_once_with(
            employees=["今津", "春山"],
            max_swaps=6,
        )
        self.assertEqual(fake_client.responses.calls, [])

    def test_malformed_ai_tool_arguments_do_not_crash(self) -> None:
        engine, _ = self._engine()

        result = engine._execute_tool("change_single_assignment", {})

        self.assertIn("ツール入力エラー", result)
        self.assertEqual(engine.get_pending_change_count(), 0)

    def test_unknown_employee_and_out_of_month_day_are_rejected(self) -> None:
        engine, _ = self._engine()

        unknown = engine._tool_change_single_assignment(
            "存在しない人", 1, "AKABANE"
        )
        invalid_day = engine._tool_change_single_assignment(
            "山本", 31, "AKABANE"
        )

        self.assertIn("従業員マスタに存在しない", unknown)
        self.assertIn("9月に存在しない", invalid_day)
        self.assertEqual(engine.get_pending_change_count(), 0)

    def test_absolute_off_request_is_rejected_before_preview(self) -> None:
        engine, _ = self._engine()
        engine.set_validation_context({"off_requests": {"山本": [2]}})

        result = engine._tool_change_single_assignment(
            "山本", 2, "AKABANE"
        )

        self.assertIn("本人の×休み希望", result)
        self.assertEqual(engine.get_pending_change_count(), 0)

    def test_apply_blocks_new_error(self) -> None:
        engine, _ = self._engine()
        engine.pending_changes.append(PendingShiftChange(
            "山本", 1, Store.OMIYA
        ))
        report = PendingSafetyReport(
            [],
            [],
            [Issue("ERROR", "絶対配置不可", 1, "山本", "テスト")],
            [],
        )

        with patch.object(
            engine, "inspect_pending_changes", return_value=report
        ):
            result = engine.apply_pending_changes()

        self.assertIn("反映を停止", result)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.AKABANE,
        )
        self.assertEqual(engine.get_pending_change_count(), 1)

    def test_new_warning_requires_explicit_acknowledgement(self) -> None:
        engine, _ = self._engine()
        engine.pending_changes.append(PendingShiftChange(
            "山本", 1, Store.OMIYA
        ))
        report = PendingSafetyReport(
            [],
            [],
            [],
            [Issue("WARNING", "店舗人数", 1, None, "テスト")],
        )

        with patch.object(
            engine, "inspect_pending_changes", return_value=report
        ):
            stopped = engine.apply_pending_changes()
            applied = engine.apply_pending_changes(allow_new_warnings=True)

        self.assertIn("重要な警告", stopped)
        self.assertIn("本シフトに反映", applied)
        self.assertEqual(
            engine.shift.get_assignment("山本", 1).store,
            Store.OMIYA,
        )

    def test_pending_inspection_passes_complete_validation_context(self) -> None:
        engine, _ = self._engine()
        engine.set_validation_context({
            "off_requests": {},
            "default_holidays": 9,
            "allow_omiya_short": True,
            "required_assignments": [{"employee": "山本", "day": 1}],
        })
        engine.pending_changes.append(PendingShiftChange(
            "山本", 1, Store.OMIYA
        ))
        introduced = Issue(
            "ERROR", "絶対配置不可", 1, "山本", "テスト"
        )

        with patch(
            "prototype.shift_chat.validate",
            side_effect=[ValidationResult(), ValidationResult([introduced])],
        ) as mocked_validate:
            report = engine.inspect_pending_changes()

        self.assertEqual(report.new_errors, [introduced])
        self.assertEqual(mocked_validate.call_count, 2)
        for call in mocked_validate.call_args_list:
            self.assertEqual(call.kwargs["default_holidays"], 9)
            self.assertTrue(call.kwargs["allow_omiya_short"])
            self.assertEqual(
                call.kwargs["required_assignments"],
                [{"employee": "山本", "day": 1}],
            )

    def test_staffing_order_runs_inspect_change_validate_without_auto_apply(self) -> None:
        def tool_response(response_id, name, arguments, call_id):
            return SimpleNamespace(
                id=response_id,
                output=[SimpleNamespace(
                    type="function_call",
                    name=name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                    call_id=call_id,
                )],
                output_text="",
            )

        scripted = _ScriptedResponses([
            tool_response(
                "response-1", "get_adjustment_overview",
                {"objective": "staffing", "employees": ["今津", "岩野"]},
                "call-1",
            ),
            tool_response(
                "response-2", "get_day_assignments", {"day": 1}, "call-2"
            ),
            tool_response(
                "response-3", "get_employee_profile",
                {"employee": "今津"}, "call-3",
            ),
            tool_response(
                "response-4", "swap_assignments",
                {"emp1": "今津", "day1": 1, "emp2": "岩野", "day2": 1},
                "call-4",
            ),
            tool_response(
                "response-5", "validate_current", {}, "call-5"
            ),
            SimpleNamespace(
                id="response-6", output=[],
                output_text="修正案をプレビューしました。",
            ),
        ])
        client = SimpleNamespace(responses=scripted)
        shift = MonthlyShift(
            year=2026,
            month=9,
            assignments=[
                ShiftAssignment("今津", 1, Store.AKABANE),
                ShiftAssignment("岩野", 1, Store.OMIYA),
                ShiftAssignment("今津", 2, Store.OMIYA),
                ShiftAssignment("岩野", 2, Store.AKABANE),
            ],
        )
        with patch("prototype.shift_chat.OpenAI", return_value=client):
            engine = ShiftChatEngine(
                shift,
                api_key="test-key",
                provider="openai",
                model="test-model",
            )

        result = engine.chat("1日の店舗人数不足を改善して")

        self.assertEqual(result, "修正案をプレビューしました。")
        self.assertEqual(engine.get_pending_change_count(), 2)
        self.assertEqual(
            engine.shift.get_assignment("今津", 1).store,
            Store.AKABANE,
        )
        self.assertEqual(
            engine.get_preview_shift().get_assignment("今津", 1).store,
            Store.OMIYA,
        )
        self.assertEqual(
            engine.get_preview_shift().get_assignment("岩野", 1).store,
            Store.AKABANE,
        )
        tool_names = [
            call["input"][0]["type"]
            if isinstance(call.get("input"), list) else "user"
            for call in scripted.calls
        ]
        self.assertEqual(tool_names[0], "user")
        self.assertTrue(all(name == "function_call_output" for name in tool_names[1:]))


if __name__ == "__main__":
    unittest.main()
