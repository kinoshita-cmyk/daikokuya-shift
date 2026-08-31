from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from prototype.models import MonthlyShift, ShiftAssignment, Store
from prototype.shift_chat import PendingShiftChange, ShiftChatEngine


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


class ShiftChatOpenAITest(unittest.TestCase):
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
        engine.apply_pending_changes()
        self.assertIsNone(engine.shift.get_assignment("山本", 1))

    def test_redo_restores_preview_before_reapplying(self) -> None:
        engine, _ = self._engine()
        engine.pending_changes.append(PendingShiftChange(
            "山本", 1, Store.OMIYA
        ))
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


if __name__ == "__main__":
    unittest.main()
