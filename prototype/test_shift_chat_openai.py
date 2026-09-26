from __future__ import annotations

import ast
import json
from dataclasses import asdict
from pathlib import Path
from textwrap import dedent
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.shift_chat import (
    PendingSafetyReport,
    PendingShiftChange,
    ShiftChatEngine,
    format_chat_error,
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
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _APIError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class _ToolLoopResponses:
    """Reject a user message linked to unanswered calls, like the API."""
    def __init__(self, ignore_tool_choice=False):
        self.calls = []
        self.outputs = {}
        self.ignore_tool_choice = ignore_tool_choice

    def create(self, **kwargs):
        self.calls.append(kwargs)
        previous = kwargs.get("previous_response_id")
        waiting = self.outputs.get(previous, [])
        if waiting:
            supplied = kwargs["input"]
            ids = {item["call_id"] for item in supplied} if isinstance(supplied, list) else set()
            if ids != {call.call_id for call in waiting}:
                raise _APIError(400, f"No tool output found for function call {waiting[0].call_id}.")
        response_id = f"response-{len(self.calls)}"
        output = [] if kwargs.get("tool_choice") == "none" and not self.ignore_tool_choice else [
            SimpleNamespace(type="function_call", name="validate_current",
                            arguments="{}", call_id=f"call-{len(self.calls)}")
        ]
        self.outputs[response_id] = output
        return SimpleNamespace(id=response_id, output=output,
                               output_text="確認結果です。" if not output else "")


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

    def test_iteration_limit_does_not_poison_the_next_message(self) -> None:
        engine, client = self._engine()
        client.responses = _ToolLoopResponses()
        with patch.object(engine, "_execute_tool", return_value="検証OK") as execute:
            first = engine.chat("確認して", max_iterations=1)
            second = engine.chat("もう一度確認して", max_iterations=1)
        self.assertEqual(first, "確認結果です。")
        self.assertEqual(second, "確認結果です。")
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(client.responses.calls[1]["tool_choice"], "none")
        self.assertEqual(client.responses.calls[2]["previous_response_id"], "response-2")
        self.assertEqual(engine.openai_previous_response_id, "response-4")

    @staticmethod
    def _response(response_id="done", calls=(), status="completed"):
        return SimpleNamespace(id=response_id, status=status, output=[
            SimpleNamespace(type="function_call", name="validate_current", arguments="{}", call_id=call_id)
            for call_id in calls
        ], output_text="確認結果です。" if not calls else "")

    def test_limit_ignoring_response_is_not_reused_or_executed(self) -> None:
        engine, client = self._engine()
        client.responses = _ToolLoopResponses(ignore_tool_choice=True)
        with patch.object(engine, "_execute_tool", return_value="検証OK") as execute:
            result = engine.chat("確認して", max_iterations=1)
        self.assertIn("上限", result)
        self.assertIsNone(engine.openai_previous_response_id)
        execute.assert_called_once()
        self.assertEqual(len(client.responses.calls), 2)
        client.responses.ignore_tool_choice = False
        with patch.object(engine, "_execute_tool", return_value="検証OK"):
            self.assertEqual(engine.chat("確認して", max_iterations=1), "確認結果です。")
        self.assertNotIn("previous_response_id", client.responses.calls[2])

    def test_zero_iterations_never_executes_tools(self) -> None:
        engine, client = self._engine()
        client.responses = _ToolLoopResponses()
        with patch.object(engine, "_execute_tool") as execute:
            self.assertEqual(engine.chat("確認して", max_iterations=0), "確認結果です。")
        execute.assert_not_called()
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(engine.openai_previous_response_id, "response-1")

    def test_final_round_sends_all_parallel_tool_outputs(self) -> None:
        engine, client = self._engine()
        client.responses = _ScriptedResponses([
            self._response("tools", ("call-a", "call-b")), self._response(),
        ])
        with patch.object(engine, "_execute_tool", side_effect=["結果A", "結果B"]) as execute:
            self.assertEqual(engine.chat("確認して", max_iterations=1), "確認結果です。")
        self.assertEqual(execute.call_count, 2)
        submitted = client.responses.calls[1]
        self.assertEqual(submitted["previous_response_id"], "tools")
        self.assertEqual(submitted["tool_choice"], "none")
        self.assertEqual(submitted["input"], [
            {"type": "function_call_output", "call_id": "call-a", "output": "結果A"},
            {"type": "function_call_output", "call_id": "call-b", "output": "結果B"},
        ])
        self.assertEqual(engine.openai_previous_response_id, "done")

    def test_old_broken_history_recovers_once_without_changing_schedule_state(self) -> None:
        engine, client = self._engine()
        engine.openai_previous_response_id = "old-unanswered"
        engine.pending_changes.append(PendingShiftChange("山本", 1, None))
        engine.undo_stack.append(("undo", engine._clone_shift(engine.shift)))
        engine.redo_stack.append(("redo", engine._clone_shift(engine.shift)))
        engine.redo_preview_active = True
        engine.validation_inputs = {"off_requests": {"山本": [2]}}
        before = (asdict(engine.shift), list(engine.pending_changes), list(engine.undo_stack),
                  list(engine.redo_stack), engine.validation_inputs.copy())
        client.responses = _ScriptedResponses([
            _APIError(400, "No tool output found for function call call-old."),
            self._response("recovered"),
        ])
        with patch.object(engine, "_execute_tool") as execute:
            result = engine.chat("10月25日の配属を確認して")
        self.assertIn("リセットして再開", result)
        self.assertIn("以前の会話内容は引き継いでいません", result)
        self.assertEqual(client.responses.calls[0]["previous_response_id"], "old-unanswered")
        self.assertNotIn("previous_response_id", client.responses.calls[1])
        self.assertEqual(client.responses.calls[1]["input"], "10月25日の配属を確認して")
        self.assertIn("以前の案や口頭条件を推測せず", client.responses.calls[1]["instructions"])
        self.assertEqual(engine.openai_previous_response_id, "recovered")
        self.assertEqual(before, (asdict(engine.shift), engine.pending_changes, engine.undo_stack,
                                 engine.redo_stack, engine.validation_inputs))
        self.assertTrue(engine.redo_preview_active)
        execute.assert_not_called()

    def test_broken_history_retry_is_bounded(self) -> None:
        engine, client = self._engine()
        engine.openai_previous_response_id = "old-unanswered"
        failure = _APIError(400, "No tool output found for function call call-old.")
        client.responses = _ScriptedResponses([failure, failure])
        with self.assertRaises(_APIError):
            engine.chat("確認して")
        self.assertEqual(len(client.responses.calls), 2)
        self.assertIsNone(engine.openai_previous_response_id)

    def test_unrelated_api_errors_are_not_retried_or_clear_history(self) -> None:
        for status, message in [(401, "invalid key"), (429, "insufficient_quota"),
                                (400, "unsupported model"), (500, "server error")]:
            with self.subTest(status=status):
                engine, client = self._engine()
                engine.openai_previous_response_id = "complete"
                client.responses = _ScriptedResponses([_APIError(status, message)])
                with self.assertRaises(_APIError):
                    engine.chat("確認して")
                self.assertEqual(len(client.responses.calls), 1)
                self.assertEqual(engine.openai_previous_response_id, "complete")

    def test_failed_tool_result_submission_does_not_reexecute_changes(self) -> None:
        for failure in [_APIError(400, "No tool output found for function call call-a."),
                        TimeoutError("connection failed")]:
            with self.subTest(failure=type(failure).__name__):
                engine, client = self._engine()
                client.responses = _ScriptedResponses([
                    self._response("tools", ("call-a",)), failure,
                ])
                before = asdict(engine.shift)

                def make_preview(*_):
                    engine.pending_changes.append(PendingShiftChange("山本", 1, None))
                    return "プレビューを作成しました"

                with patch.object(engine, "_execute_tool", side_effect=make_preview) as execute:
                    with self.assertRaises(type(failure)):
                        engine.chat("変更して")
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(len(client.responses.calls), 2)
                self.assertEqual(asdict(engine.shift), before)
                self.assertEqual(engine.get_pending_change_count(), 1)
                self.assertIsNone(engine.openai_previous_response_id)

    def test_malformed_arguments_return_output_without_executing(self) -> None:
        for raw in ["{broken", "[]", "null", '"text"']:
            with self.subTest(raw=raw):
                engine, client = self._engine()
                response = self._response("tools", ("call-a",))
                response.output[0].arguments = raw
                client.responses = _ScriptedResponses([response, self._response()])
                with patch.object(engine, "_execute_tool") as execute:
                    engine.chat("確認して")
                execute.assert_not_called()
                output = client.responses.calls[1]["input"][0]
                self.assertEqual(output["call_id"], "call-a")
                self.assertIn("処理は実行していません", output["output"])

    def test_incomplete_response_is_not_saved_or_executed(self) -> None:
        for calls in [(), ("partial-call",)]:
            with self.subTest(calls=calls):
                engine, client = self._engine()
                client.responses = _ScriptedResponses([self._response("partial", calls, "incomplete")])
                with patch.object(engine, "_execute_tool") as execute:
                    result = engine.chat("確認して")
                self.assertIn("途中で終了", result)
                self.assertIsNone(engine.openai_previous_response_id)
                execute.assert_not_called()

    def test_reset_conversation_keeps_preview_undo_and_rules(self) -> None:
        engine, _ = self._engine()
        engine.message_history = [{"role": "user", "content": "過去の会話"}]
        engine.openai_previous_response_id = "old"
        engine.pending_changes.append(PendingShiftChange("山本", 1, None))
        engine.undo_stack.append(("undo", engine._clone_shift(engine.shift)))
        engine.redo_stack.append(("redo", engine._clone_shift(engine.shift)))
        engine.validation_inputs = {"off_requests": {"山本": [2]}}
        before = (asdict(engine.shift), list(engine.pending_changes), list(engine.undo_stack),
                  list(engine.redo_stack), engine.validation_inputs.copy())
        engine.reset_conversation()
        self.assertEqual(engine.message_history, [])
        self.assertIsNone(engine.openai_previous_response_id)
        self.assertEqual(before, (asdict(engine.shift), engine.pending_changes, engine.undo_stack,
                                 engine.redo_stack, engine.validation_inputs))

    def test_history_error_has_actionable_message_not_key_or_credit_advice(self) -> None:
        error = _APIError(400, "No tool output found for function call call-sensitive.")
        message = format_chat_error(error)
        self.assertIn("会話をクリア", message)
        self.assertIn("追加課金で解決するエラーではありません", message)
        self.assertNotIn("call-sensitive", message)
        self.assertIn("利用上限", format_chat_error(_APIError(429, "quota")))

    def test_clear_button_resets_internal_conversation_but_keeps_work(self) -> None:
        from streamlit.testing.v1 import AppTest
        source = (Path(__file__).resolve().parents[1] / "app" / "app.py").read_text(encoding="utf-8")
        button = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.Call)
                      and any(k.arg == "key" and isinstance(k.value, ast.Constant)
                              and k.value.value == "chat_clear" for k in n.test.keywords))
        app = AppTest.from_string('''
import streamlit as st
from prototype.shift_chat import ShiftChatEngine, PendingShiftChange
from prototype.models import MonthlyShift, ShiftAssignment, Store
if "chat_engine" not in st.session_state:
    engine = ShiftChatEngine(MonthlyShift(2026, 10, assignments=[ShiftAssignment("山本", 1, Store.AKABANE)]), provider="local")
    engine.openai_previous_response_id = "unanswered"
    engine.message_history = [{"role": "user", "content": "以前の会話"}]
    engine.pending_changes = [PendingShiftChange("山本", 1, None)]
    engine.undo_stack = [("undo", engine._clone_shift(engine.shift))]
    st.session_state.chat_engine = engine
    st.session_state.chat_messages = [{"role": "user", "content": "以前の会話"}]
    st.session_state.chat_quality_guard = {"protected": "unchanged"}
chat_engine = st.session_state.chat_engine
''' + dedent(ast.get_source_segment(source, button))).run()
        self.assertEqual(len(app.exception), 0)
        app.button(key="chat_clear").click().run()
        self.assertEqual(len(app.exception), 0)
        engine = app.session_state.chat_engine
        self.assertIsNone(engine.openai_previous_response_id)
        self.assertEqual(engine.message_history, [])
        self.assertEqual(app.session_state.chat_messages, [])
        self.assertEqual(engine.get_pending_change_count(), 1)
        self.assertEqual(len(engine.undo_stack), 1)
        self.assertEqual(engine.shift.get_assignment("山本", 1).store, Store.AKABANE)
        self.assertEqual(app.session_state.chat_quality_guard, {"protected": "unchanged"})

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
