"""
AI対話によるシフト微調整
================================================
経営者がシフト案を見ながら、AIに質問・指示を出して微調整できる。

例:
  経営者: 「15日の大宮、田中さんを佐藤さんに変えるとどうなる？」
  AI: 「田中さんは元々第3土曜希望休でしたが叶えられます。
        ただし佐藤さんは4連勤目になります（上限5連勤の希望なのでOK）。
        入れ替えますか？」

実装方針:
- Claude のツール使用（Tool Use）機能を活用
- 利用可能なツール:
  - get_day_assignments: ある日の配属を取得
  - get_employee_schedule: ある人の月内の出勤予定を取得
  - swap_assignments: 2人の配属を入れ替え（提案のみ、即時反映しない）
  - validate_change: 変更後の制約違反チェック
  - apply_change: 確定して適用
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from typing import Optional, Callable

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

from .models import MonthlyShift, ShiftAssignment, Store
from .employees import ALL_EMPLOYEES, get_employee
from .validator import validate


SYSTEM_PROMPT = """\
あなたは大黒屋（ブランド買取店）のシフト管理アシスタントです。
経営者と対話しながらシフトを微調整するのが役割です。

# 大黒屋の店舗（記号）
- AKABANE (○): 赤羽駅前店
- HIGASHIGUCHI (□): 赤羽東口店
- OMIYA (△): 大宮駅前店
- NISHIGUCHI (☆): 大宮西口店
- SUZURAN (◆): 大宮すずらん通り店
- OFF (×): 休み

# 振る舞いルール
- 経営者の質問・指示に対し、まず現状を確認するためツールを呼び出す
- 変更案を提案する際は、必ず影響を分析して伝える（連勤になる、希望に反する等）
- 変更は必ず「プレビュー」として作る。確定・破棄は画面の操作ボタンで行う
- 経営者が「実行して」「変更して」と書いても、確定操作は画面の「本シフトに反映」ボタンを案内する
- 本人が提出した「×」休み希望日は絶対に勤務へ変更しない
- 制約違反のリスクがある場合は警告する
- 簡潔で実用的な日本語で答える

# 配属変更の流れ
1. 経営者の希望を理解
2. get_day_assignments / get_employee_schedule で現状確認
3. swap_assignments でプレビュー変更を作成
4. validate_current で違反チェック
5. 結果を経営者に報告し、画面の「本シフトに反映」または「プレビューを破棄」ボタンを案内する
"""


# ============================================================
# ツール定義
# ============================================================

TOOLS = [
    {
        "name": "get_day_assignments",
        "description": "指定した日の全員の配属を取得します",
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "integer", "description": "取得する日（1-31）"}
            },
            "required": ["day"],
        },
    },
    {
        "name": "get_employee_schedule",
        "description": "指定従業員の月内の出勤スケジュール一覧を取得します",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee": {"type": "string", "description": "従業員名（例：田中）"}
            },
            "required": ["employee"],
        },
    },
    {
        "name": "swap_assignments",
        "description": "2つの配属を入れ替えます（プレビュー変更を作成し、反映前にユーザー承認が必要）。1人だけの店舗変更も可能（emp2/day2 を省略すると emp1 を target_store に配置）",
        "input_schema": {
            "type": "object",
            "properties": {
                "emp1": {"type": "string"},
                "day1": {"type": "integer"},
                "emp2": {"type": "string"},
                "day2": {"type": "integer"},
            },
            "required": ["emp1", "day1", "emp2", "day2"],
        },
    },
    {
        "name": "change_single_assignment",
        "description": "1人の特定日の配属を変更します（プレビュー変更を作成）",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee": {"type": "string"},
                "day": {"type": "integer"},
                "new_store": {
                    "type": "string",
                    "enum": ["AKABANE", "HIGASHIGUCHI", "OMIYA", "NISHIGUCHI", "SUZURAN", "OFF"],
                },
            },
            "required": ["employee", "day", "new_store"],
        },
    },
    {
        "name": "validate_current",
        "description": "現在の（仮）シフトの制約違反をチェックして要約を返します",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ============================================================
# チャットエンジン
# ============================================================

class ShiftChatEngine:
    """シフト調整用のチャットエンジン"""

    def __init__(
        self,
        shift: MonthlyShift,
        api_key: Optional[str] = None,
        model: str = "claude-opus-4-7",
        validation_inputs: Optional[dict] = None,
        max_consec: int = 5,
    ):
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic パッケージが必要です")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY が必要です")
        self.client = Anthropic(api_key=self.api_key)
        self.model = model

        # 確定済みシフト + 仮（pending）変更
        self.shift = shift
        self.pending_changes: list[ShiftAssignment] = []
        self.message_history: list[dict] = []
        self.undo_stack: list[tuple[str, MonthlyShift]] = []
        self.redo_stack: list[tuple[str, MonthlyShift]] = []
        self.last_status_message = ""
        self.validation_inputs = validation_inputs or {}
        self.max_consec = max_consec

    # ========== 内部ヘルパ ==========

    def _clone_shift(self, shift: MonthlyShift) -> MonthlyShift:
        """シフトを履歴保存用にコピーする。"""
        return MonthlyShift(
            year=shift.year,
            month=shift.month,
            assignments=[
                ShiftAssignment(
                    employee=a.employee,
                    day=a.day,
                    store=a.store,
                    is_paid_leave=a.is_paid_leave,
                )
                for a in shift.assignments
            ],
            operation_modes=dict(shift.operation_modes),
        )

    def _replace_shift_contents(self, source: MonthlyShift) -> None:
        """既存の MonthlyShift オブジェクトを保ったまま中身だけ差し替える。"""
        self.shift.year = source.year
        self.shift.month = source.month
        self.shift.assignments = [
            ShiftAssignment(
                employee=a.employee,
                day=a.day,
                store=a.store,
                is_paid_leave=a.is_paid_leave,
            )
            for a in source.assignments
        ]
        self.shift.operation_modes = dict(source.operation_modes)

    def _dedup_pending_changes(self) -> list[ShiftAssignment]:
        """同じ人・同じ日のプレビュー変更は最後の内容だけを有効にする。"""
        changes: dict[tuple[str, int], ShiftAssignment] = {}
        for p in self.pending_changes:
            changes[(p.employee, p.day)] = p
        return list(changes.values())

    def set_validation_context(
        self,
        validation_inputs: Optional[dict] = None,
        max_consec: Optional[int] = None,
    ) -> None:
        """画面側の最新検証条件をAI対話にも渡す。"""
        if validation_inputs is not None:
            self.validation_inputs = validation_inputs
        if max_consec is not None:
            self.max_consec = max_consec

    def _off_request_violation_messages(
        self,
        changes: list[ShiftAssignment],
    ) -> list[str]:
        """本人の×休み希望を勤務へ変えようとしていないか確認する。"""
        off_requests = self.validation_inputs.get("off_requests", {}) or {}
        messages = []
        for change in changes:
            if change.store == Store.OFF:
                continue
            off_days = {
                int(d) for d in off_requests.get(change.employee, [])
                if str(d).isdigit()
            }
            if change.day in off_days:
                messages.append(
                    f"{change.employee}さんの{self.shift.month}/{change.day}は"
                    "本人の×休み希望です。勤務への変更はできません。"
                )
        return messages

    def _validate_shift_with_context(self, shift: MonthlyShift):
        """生成時に使った希望データがあれば、それも含めて検証する。"""
        return validate(
            shift=shift,
            work_requests=self.validation_inputs.get("work_requests", []),
            off_requests=self.validation_inputs.get("off_requests", {}),
            prev_month=self.validation_inputs.get("prev_month", []),
            holiday_overrides=self.validation_inputs.get("holiday_overrides", {}),
            max_consec=self.max_consec,
        )

    def get_pending_change_count(self) -> int:
        return len(self._dedup_pending_changes())

    def get_pending_change_keys(self) -> set[tuple[str, int]]:
        return {(p.employee, p.day) for p in self._dedup_pending_changes()}

    def get_pending_change_summary(self, limit: int = 8) -> list[str]:
        """画面表示用にプレビュー変更を短く要約する。"""
        summary = []
        for p in sorted(self._dedup_pending_changes(), key=lambda x: (x.day, x.employee)):
            before = self.shift.get_assignment(p.employee, p.day)
            before_store = before.store.display_name if before else "未配置"
            after_store = p.store.display_name
            summary.append(
                f"{self.shift.month}/{p.day} {p.employee}: {before_store} → {after_store}"
            )
        if len(summary) > limit:
            return summary[:limit] + [f"...他 {len(summary) - limit} 件"]
        return summary

    def get_preview_shift(self) -> MonthlyShift:
        """プレビュー変更を反映した表示用シフトを返す。"""
        return self._apply_pending_to_shift()

    def _get_effective_assignment(self, employee: str, day: int) -> Optional[ShiftAssignment]:
        """確定 + プレビュー変更を反映した配属を取得"""
        # プレビュー変更があればそれを返す
        for p in reversed(self.pending_changes):
            if p.employee == employee and p.day == day:
                return p
        return self.shift.get_assignment(employee, day)

    def _apply_pending_to_shift(self) -> MonthlyShift:
        """プレビュー変更を反映したシフトのコピーを返す（検証用）"""
        copy = self._clone_shift(self.shift)
        # プレビュー変更を反映
        for p in self._dedup_pending_changes():
            # 同じ (employee, day) の既存を削除
            copy.assignments = [
                a for a in copy.assignments
                if not (a.employee == p.employee and a.day == p.day)
            ]
            copy.assignments.append(p)
        return copy

    # ========== ツール実装 ==========

    def _tool_get_day_assignments(self, day: int) -> str:
        result = []
        for emp in ALL_EMPLOYEES:
            a = self._get_effective_assignment(emp.name, day)
            if a is None:
                continue
            result.append(f"  {emp.name}: {a.store.display_name}")
        ym_day = f"{self.shift.month}/{day}"
        return f"{ym_day}日の配属:\n" + "\n".join(result) if result else f"{ym_day}日: 配属なし"

    def _tool_get_employee_schedule(self, employee: str) -> str:
        from calendar import monthrange
        days = monthrange(self.shift.year, self.shift.month)[1]
        result = []
        for d in range(1, days + 1):
            a = self._get_effective_assignment(employee, d)
            store_str = a.store.display_name if a else "未配置"
            result.append(f"  {self.shift.month}/{d}: {store_str}")
        return f"{employee}の{self.shift.month}月スケジュール:\n" + "\n".join(result)

    def _tool_swap_assignments(self, emp1: str, day1: int, emp2: str, day2: int) -> str:
        a1 = self._get_effective_assignment(emp1, day1)
        a2 = self._get_effective_assignment(emp2, day2)
        if a1 is None or a2 is None:
            return f"エラー: 配属が見つかりません ({emp1}/{day1}: {a1}, {emp2}/{day2}: {a2})"
        # 入れ替え
        proposed = [
            ShiftAssignment(employee=emp1, day=day1, store=a2.store),
            ShiftAssignment(employee=emp2, day=day2, store=a1.store),
        ]
        violations = self._off_request_violation_messages(proposed)
        if violations:
            return "変更できません: " + " / ".join(violations)
        self.pending_changes.extend(proposed)
        return (
            f"プレビュー: {emp1} {self.shift.month}/{day1} ({a1.store.display_name} → {a2.store.display_name}) / "
            f"{emp2} {self.shift.month}/{day2} ({a2.store.display_name} → {a1.store.display_name})"
        )

    def _tool_change_single_assignment(self, employee: str, day: int, new_store: str) -> str:
        try:
            store = Store[new_store]
        except KeyError:
            return f"エラー: 不明な店舗 {new_store}"
        before = self._get_effective_assignment(employee, day)
        before_str = before.store.display_name if before else "未配置"
        proposed = ShiftAssignment(employee=employee, day=day, store=store)
        violations = self._off_request_violation_messages([proposed])
        if violations:
            return "変更できません: " + " / ".join(violations)
        self.pending_changes.append(proposed)
        return f"プレビュー: {employee} {self.shift.month}/{day} ({before_str} → {store.display_name})"

    def _tool_validate_current(self) -> str:
        copy = self._apply_pending_to_shift()
        result = self._validate_shift_with_context(copy)
        if result.error_count == 0 and result.warning_count == 0:
            return "✅ 制約違反はありません"
        out = [f"エラー {result.error_count}件 / 警告 {result.warning_count}件"]
        for issue in result.issues[:8]:  # 上位8件のみ
            out.append(f"  {issue}")
        if len(result.issues) > 8:
            out.append(f"  ...他 {len(result.issues) - 8} 件")
        return "\n".join(out)

    def _tool_apply_changes(self) -> str:
        pending = self._dedup_pending_changes()
        if not pending:
            return "適用すべき変更はありません"
        violations = self._off_request_violation_messages(pending)
        if violations:
            return "反映できません: " + " / ".join(violations)
        n = len(pending)
        before = self._clone_shift(self.shift)
        # 確定シフトに反映
        for p in pending:
            self.shift.assignments = [
                a for a in self.shift.assignments
                if not (a.employee == p.employee and a.day == p.day)
            ]
            self.shift.assignments.append(p)
        self.pending_changes.clear()
        self.undo_stack.append((f"{n}件の変更", before))
        self.undo_stack = self.undo_stack[-20:]
        self.redo_stack.clear()
        self.last_status_message = f"✅ {n}件のプレビュー変更を本シフトに反映しました"
        return self.last_status_message

    def _tool_discard_changes(self) -> str:
        n = self.get_pending_change_count()
        self.pending_changes.clear()
        self.last_status_message = f"🗑 {n}件のプレビュー変更を破棄しました"
        return self.last_status_message

    def apply_pending_changes(self) -> str:
        """画面ボタンからプレビュー変更を確定する。"""
        return self._tool_apply_changes()

    def discard_pending_changes(self) -> str:
        """画面ボタンからプレビュー変更を破棄する。"""
        return self._tool_discard_changes()

    def undo_last_apply(self) -> str:
        """直近の確定変更を元に戻す。"""
        if not self.undo_stack:
            return "戻せる変更はありません"
        label, previous = self.undo_stack.pop()
        current = self._clone_shift(self.shift)
        self.redo_stack.append((label, current))
        self.pending_changes.clear()
        self._replace_shift_contents(previous)
        self.last_status_message = f"↩ {label}を元に戻しました"
        return self.last_status_message

    def redo_last_apply(self) -> str:
        """元に戻した変更をやり直す。"""
        if not self.redo_stack:
            return "進める変更はありません"
        label, next_shift = self.redo_stack.pop()
        current = self._clone_shift(self.shift)
        self.undo_stack.append((label, current))
        self.pending_changes.clear()
        self._replace_shift_contents(next_shift)
        self.last_status_message = f"↪ {label}をやり直しました"
        return self.last_status_message

    # ========== ツールルーター ==========

    def _execute_tool(self, name: str, args: dict) -> str:
        if name == "get_day_assignments":
            return self._tool_get_day_assignments(args["day"])
        elif name == "get_employee_schedule":
            return self._tool_get_employee_schedule(args["employee"])
        elif name == "swap_assignments":
            return self._tool_swap_assignments(
                args["emp1"], args["day1"], args["emp2"], args["day2"]
            )
        elif name == "change_single_assignment":
            return self._tool_change_single_assignment(
                args["employee"], args["day"], args["new_store"]
            )
        elif name == "validate_current":
            return self._tool_validate_current()
        elif name == "apply_changes":
            return self._tool_apply_changes()
        elif name == "discard_changes":
            return self._tool_discard_changes()
        return f"不明なツール: {name}"

    # ========== チャットメイン ==========

    def chat(self, user_message: str, max_iterations: int = 5) -> str:
        """ユーザーメッセージに応答（ツール呼び出しを含む）"""
        self.message_history.append({"role": "user", "content": user_message})

        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=self.message_history,
            )
            # ツール呼び出しがあるか
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            # アシスタントの応答を履歴に追加
            self.message_history.append({"role": "assistant", "content": response.content})

            if not tool_calls:
                # 終了：テキスト応答のみ
                return "\n".join(b.text for b in text_blocks)

            # ツール実行結果を返す
            tool_results = []
            for tc in tool_calls:
                result = self._execute_tool(tc.name, tc.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })
            self.message_history.append({"role": "user", "content": tool_results})

        # max_iterations 超え
        return "（応答生成中にツール呼び出しが多すぎました）"


if __name__ == "__main__":
    print("【AI対話エンジン 動作テスト】\n")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠ ANTHROPIC_API_KEY が設定されていません。")
        print("テストするには環境変数を設定してください。")
        exit()

    from .generator import generate_shift, determine_operation_modes
    from .may_2026_data import (
        OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
    )
    from .rules import MAY_2026_HOLIDAY_OVERRIDES

    print("シフト生成中...")
    modes = determine_operation_modes(2026, 5)
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS, work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER, flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, operation_modes=modes,
        consec_exceptions=["野澤"], max_consec_override=5, verbose=False,
    )

    engine = ShiftChatEngine(shift)
    response = engine.chat("5/15の大宮駅前店には誰がいますか？")
    print(f"AI: {response}")
