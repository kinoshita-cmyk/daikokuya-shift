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
from typing import Optional

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

from .models import MonthlyShift, ShiftAssignment, Store
from .employees import ALL_EMPLOYEES, get_employee
from .shift_readjuster import (
    AdjustmentProposal,
    propose_tobishi_reoptimization_options,
    propose_yamamoto_cleanup,
    tobishi_days,
)
from .validator import validate


SYSTEM_PROMPT = """\
あなたは大黒屋（ブランド買取店）のシフト再調整アシスタントです。
経営者が自然な言葉で伝えた意図を整理し、現在のシフト案を安全に微調整します。

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
- 「飛び石勤務」は「休み・出勤・休み」の1日だけの単独出勤を意味する。
  「出勤・休み・出勤」の単独休日や「休み・出勤・出勤・休み」の2連勤は
  飛び石勤務として扱わない
- 「飛び石を減らして」のような目的指定には、専用の再最適化ツールを優先する
- 専用ツールは現在の本シフトを出発点に、複数の相互入れ替えを同時に比較する。
  対象者が複数いる場合は一部の人だけでなく、対象者全員の改善を優先する。候補なしの場合は
  「不可能」と断定せず、その探索範囲では見つからなかったと説明する
- 飛び石以外の依頼は get_adjustment_overview で全体像を確認してから、必要な日・人の
  詳細を取得し、複数の変更を組み合わせて検討する
- 山本は赤羽で本当に必要な日のみ自動出勤し、それ以外は空欄とする
- 曖昧な依頼は勝手に決めず、確認に必要な日・従業員・店舗を質問する
- 変更できない場合は、守る必要がある条件と、緩めれば可能になる条件を説明する
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
        "name": "get_adjustment_overview",
        "description": (
            "再調整したい観点について、現在の勤務日数、飛び石、店舗配分、"
            "エラー・警告をまとめて取得します。個別の変更を考える前に使用します"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "enum": [
                        "tobishi", "yamamoto", "staffing", "consecutive",
                        "store_balance", "workdays", "training", "overall",
                    ],
                    "description": "確認したい調整目的",
                },
                "employees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "重点確認する従業員。省略時は全員",
                },
            },
            "required": ["objective"],
        },
    },
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
        "name": "get_employee_profile",
        "description": (
            "指定従業員のスキル、主担当、店舗適性、配置不可、備考を取得します。"
            "店舗変更を提案する前に確認してください"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee": {"type": "string", "description": "従業員名"}
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
    {
        "name": "optimize_tobishi",
        "description": (
            "休み・出勤・休みとなる1日だけの単独出勤を、本人の絶対休み・各日の店舗人数・"
            "各人の月間勤務日数を維持し、複数の相互入れ替えを同時に"
            "再最適化して減らします。対象者を省略するとエコ主力を最優先し、"
            "その後ほかの通常スタッフも可能な範囲で減らします。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "対象従業員名。省略時はエコ主力優先で全通常スタッフ",
                },
                "max_swaps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 6,
                    "description": "提案する相互入れ替えの最大組数",
                },
            },
        },
    },
    {
        "name": "cleanup_yamamoto",
        "description": (
            "山本の現在の出勤日のうち、赤羽の通常スタッフだけで必要人数を"
            "満たしていて不要になった日を空欄へ戻すプレビューを作ります"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _openai_tools() -> list[dict]:
    """Convert the shared tool definitions to Responses API function tools."""
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            "strict": False,
        }
        for tool in TOOLS
    ]


@dataclass
class PendingShiftChange:
    """A preview change.  ``store=None`` means remove the assignment."""

    employee: str
    day: int
    store: Optional[Store]
    is_paid_leave: bool = False


# ============================================================
# チャットエンジン
# ============================================================

class ShiftChatEngine:
    """シフト調整用のチャットエンジン"""

    def __init__(
        self,
        shift: MonthlyShift,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: str = "anthropic",
        validation_inputs: Optional[dict] = None,
        max_consec: int = 5,
    ):
        provider = str(provider or "anthropic").strip().lower()
        if provider not in {"anthropic", "openai", "local"}:
            raise ValueError(f"未対応のAIプロバイダーです: {provider}")
        if provider == "anthropic" and not HAS_ANTHROPIC:
            raise ImportError("anthropic パッケージが必要です")
        if provider == "openai" and not HAS_OPENAI:
            raise ImportError("openai パッケージが必要です")

        env_key = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        self.api_key = api_key or (os.environ.get(env_key) if provider != "local" else None)
        if provider != "local" and not self.api_key:
            raise ValueError(f"{env_key} が必要です")
        self.provider = provider
        if provider == "openai":
            self.client = OpenAI(api_key=self.api_key)
            self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
        elif provider == "anthropic":
            self.client = Anthropic(api_key=self.api_key)
            self.model = model or os.environ.get(
                "ANTHROPIC_SHIFT_MODEL", "claude-opus-4-7"
            )
        else:
            self.client = None
            self.model = "local-rules"

        # 確定済みシフト + 仮（pending）変更
        self.shift = shift
        self.pending_changes: list[PendingShiftChange] = []
        self.message_history: list[dict] = []
        self.openai_previous_response_id: Optional[str] = None
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
            comments=list(getattr(shift, "comments", []) or []),
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

    def _dedup_pending_changes(self) -> list[PendingShiftChange]:
        """同じ人・同じ日のプレビュー変更は最後の内容だけを有効にする。"""
        changes: dict[tuple[str, int], PendingShiftChange] = {}
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
        changes: list[PendingShiftChange],
    ) -> list[str]:
        """本人の×休み希望を勤務へ変えようとしていないか確認する。"""
        off_requests = self.validation_inputs.get("off_requests", {}) or {}
        messages = []
        for change in changes:
            if change.store in (None, Store.OFF):
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
            preferred_work_requests=self.validation_inputs.get(
                "preferred_work_requests", []
            ),
            preferred_work_groups=self.validation_inputs.get(
                "preferred_work_groups", []
            ),
            off_requests=self.validation_inputs.get("off_requests", {}),
            prev_month=self.validation_inputs.get("prev_month", []),
            holiday_overrides=self.validation_inputs.get("holiday_overrides", {}),
            exact_holiday_days=self.validation_inputs.get("exact_holiday_days", {}),
            paid_leave_days=self.validation_inputs.get("paid_leave_days", {}),
            employee_max_consecutive_work=self.validation_inputs.get(
                "employee_max_consecutive_work", {}
            ),
            employee_max_consecutive_off=self.validation_inputs.get(
                "employee_max_consecutive_off", {}
            ),
            monthly_store_count_rules=self.validation_inputs.get(
                "monthly_store_count_rules", []
            ),
            required_assignments=self.validation_inputs.get(
                "required_assignments", []
            ),
            allow_omiya_short=self.validation_inputs.get("allow_omiya_short"),
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
            after_store = p.store.display_name if p.store is not None else "未配置（空欄）"
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
                if p.store is None:
                    return None
                return ShiftAssignment(
                    employee=p.employee,
                    day=p.day,
                    store=p.store,
                    is_paid_leave=p.is_paid_leave,
                )
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
            if p.store is not None:
                copy.assignments.append(ShiftAssignment(
                    employee=p.employee,
                    day=p.day,
                    store=p.store,
                    is_paid_leave=p.is_paid_leave,
                ))
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

    def _tool_get_adjustment_overview(
        self,
        objective: str,
        employees: Optional[list[str]] = None,
    ) -> str:
        """Return compact facts the AI needs before planning a re-adjustment."""
        from calendar import monthrange

        preview = self._apply_pending_to_shift()
        names = [
            name for name in (employees or [emp.name for emp in ALL_EMPLOYEES])
            if any(a.employee == name for a in preview.assignments)
        ]
        days = monthrange(preview.year, preview.month)[1]
        lines = [
            f"{preview.year}年{preview.month}月 / 調整目的: {objective}",
            "対象: " + ("、".join(names) if names else "該当者なし"),
        ]

        for name in names:
            assignments = [
                a for a in preview.assignments
                if a.employee == name and a.store != Store.OFF
            ]
            store_counts: dict[str, int] = {}
            for assignment in assignments:
                label = assignment.store.display_name
                store_counts[label] = store_counts.get(label, 0) + 1
            working = {
                day: bool(
                    (assignment := preview.get_assignment(name, day))
                    and assignment.store != Store.OFF
                )
                for day in range(1, days + 1)
            }
            longest = 0
            current = 0
            for day in range(1, days + 1):
                if working[day]:
                    current += 1
                    longest = max(longest, current)
                else:
                    current = 0
            isolated_work = tobishi_days(preview, name)
            store_text = "、".join(
                f"{store}:{count}日" for store, count in sorted(store_counts.items())
            ) or "なし"
            lines.append(
                f"- {name}: 出勤{len(assignments)}日 / 最長{longest}連勤 / "
                f"飛び石勤務（休み・出勤・休み）{isolated_work or 'なし'} / "
                f"配分 {store_text}"
            )

        validation = self._validate_shift_with_context(preview)
        relevant_categories = {
            "tobishi": ("飛び石勤務",),
            "yamamoto": ("店舗人数", "全体人数"),
            "staffing": ("店舗人数", "全体人数", "技能構成"),
            "consecutive": ("連勤", "月末連勤"),
            "store_balance": ("店舗バランス", "店舗人数"),
            "workdays": ("月間勤務日数", "休日数"),
            "training": ("研修", "絶対配置不可", "店舗人数"),
            "overall": tuple(),
        }
        filters = relevant_categories.get(objective, tuple())
        issues = [
            issue for issue in validation.issues
            if not filters or any(key in issue.category for key in filters)
        ]
        lines.append(
            f"検証: エラー{validation.error_count}件 / 警告{validation.warning_count}件"
        )
        if issues:
            lines.append("関連する指摘:")
            lines.extend(f"- {issue}" for issue in issues[:20])
            if len(issues) > 20:
                lines.append(f"- ...他 {len(issues) - 20}件")
        else:
            lines.append("関連する指摘: なし")
        return "\n".join(lines)

    def _tool_get_employee_schedule(self, employee: str) -> str:
        from calendar import monthrange
        days = monthrange(self.shift.year, self.shift.month)[1]
        result = []
        for d in range(1, days + 1):
            a = self._get_effective_assignment(employee, d)
            store_str = a.store.display_name if a else "未配置"
            result.append(f"  {self.shift.month}/{d}: {store_str}")
        return f"{employee}の{self.shift.month}月スケジュール:\n" + "\n".join(result)

    def _tool_get_employee_profile(self, employee: str) -> str:
        try:
            profile = get_employee(employee)
        except KeyError:
            return f"エラー: 従業員 {employee} が見つかりません"
        home_store = profile.home_store.display_name if profile.home_store else "なし"
        affinities = []
        forbidden = []
        for store, affinity in profile.affinities.items():
            if affinity.value == "不可":
                forbidden.append(store.display_name)
            else:
                affinities.append(f"{store.display_name}:{affinity.value}")
        substitute = "、".join(s.display_name for s in profile.can_substitute_at) or "なし"
        return "\n".join([
            f"{profile.name}の店舗適性:",
            f"  スキル: {profile.skill.value}",
            f"  役職: {profile.role.value}",
            f"  主担当: {home_store}",
            f"  店舗適性: {'、'.join(affinities) or '指定なし'}",
            f"  絶対配置不可: {'、'.join(forbidden) or 'なし'}",
            f"  1名体制の代行候補: {substitute}",
            f"  備考: {profile.notes or 'なし'}",
        ])

    def _tool_swap_assignments(self, emp1: str, day1: int, emp2: str, day2: int) -> str:
        a1 = self._get_effective_assignment(emp1, day1)
        a2 = self._get_effective_assignment(emp2, day2)
        if a1 is None or a2 is None:
            return f"エラー: 配属が見つかりません ({emp1}/{day1}: {a1}, {emp2}/{day2}: {a2})"
        # 入れ替え
        proposed = [
            PendingShiftChange(employee=emp1, day=day1, store=a2.store),
            PendingShiftChange(employee=emp2, day=day2, store=a1.store),
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
        proposed = PendingShiftChange(employee=employee, day=day, store=store)
        violations = self._off_request_violation_messages([proposed])
        if violations:
            return "変更できません: " + " / ".join(violations)
        self.pending_changes.append(proposed)
        return f"プレビュー: {employee} {self.shift.month}/{day} ({before_str} → {store.display_name})"

    def _queue_proposal(
        self,
        proposal: AdjustmentProposal,
        *,
        replace_pending: bool = False,
    ) -> str:
        """Add a deterministic proposal to the shared preview queue."""
        if not proposal.has_changes:
            details = " / ".join(proposal.notes)
            return proposal.summary + (f"\n{details}" if details else "")
        proposed = [
            PendingShiftChange(
                employee=change.employee,
                day=change.day,
                store=change.after_store,
                is_paid_leave=change.is_paid_leave,
            )
            for change in proposal.changes
        ]
        violations = self._off_request_violation_messages(proposed)
        if violations:
            return "変更できません: " + " / ".join(violations)
        if replace_pending:
            self.pending_changes.clear()
        self.pending_changes.extend(proposed)

        lines = [proposal.summary]
        if proposal.before_metrics or proposal.after_metrics:
            before = "、".join(
                f"{key} {value}件" for key, value in proposal.before_metrics.items()
            ) or "-"
            after = "、".join(
                f"{key} {value}件" for key, value in proposal.after_metrics.items()
            ) or "-"
            lines.append(f"再調整前: {before}")
            lines.append(f"再調整後: {after}")
        lines.append("変更内容:")
        for change in sorted(
            proposal.changes, key=lambda item: (item.day, item.employee)
        ):
            before_store = (
                change.before_store.display_name
                if change.before_store is not None else "未配置"
            )
            after_store = (
                change.after_store.display_name
                if change.after_store is not None else "未配置"
            )
            lines.append(
                f"- {change.day}日 {change.employee}: "
                f"{before_store} → {after_store}"
            )
        lines.extend(proposal.notes)
        lines.append(
            f"プレビュー {len(proposal.changes)}セル分を作成しました。"
            "表と検証結果を確認してから「本シフトに反映」を押してください。"
        )
        return "\n".join(lines)

    def _tool_optimize_tobishi(
        self,
        employees: Optional[list[str]] = None,
        max_swaps: int = 6,
    ) -> str:
        # The language model may choose an overly small value.  The repair
        # engine needs room for the five-exchange patterns seen in production.
        max_swaps = max(6, int(max_swaps or 6))
        options = propose_tobishi_reoptimization_options(
            self.shift,
            validation_context=self.validation_inputs,
            max_consec=self.max_consec,
            employee_names=employees,
            max_swaps=max_swaps,
        )
        return self._queue_proposal(options[0], replace_pending=True)

    def _tool_cleanup_yamamoto(self) -> str:
        proposal = propose_yamamoto_cleanup(self._apply_pending_to_shift())
        return self._queue_proposal(proposal)

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
            if p.store is not None:
                self.shift.assignments.append(ShiftAssignment(
                    employee=p.employee,
                    day=p.day,
                    store=p.store,
                    is_paid_leave=p.is_paid_leave,
                ))
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

    def propose_tobishi_adjustment(
        self,
        employees: Optional[list[str]] = None,
        max_swaps: int = 6,
    ) -> str:
        """画面の簡単操作から飛び石再調整案を作る。"""
        return self._tool_optimize_tobishi(employees, max_swaps)

    def find_tobishi_adjustment_options(
        self,
        employees: Optional[list[str]] = None,
        max_swaps: int = 6,
    ) -> list[AdjustmentProposal]:
        """Return safe alternatives without changing the current preview."""
        return propose_tobishi_reoptimization_options(
            self.shift,
            validation_context=self.validation_inputs,
            max_consec=self.max_consec,
            employee_names=employees,
            max_swaps=max_swaps,
        )

    def preview_adjustment_proposal(self, proposal: AdjustmentProposal) -> str:
        """Replace the current preview with a manager-selected proposal."""
        return self._queue_proposal(proposal, replace_pending=True)

    def propose_yamamoto_adjustment(self) -> str:
        """画面の簡単操作から山本の不要出勤整理案を作る。"""
        return self._tool_cleanup_yamamoto()

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
        if name == "get_adjustment_overview":
            return self._tool_get_adjustment_overview(
                args["objective"], args.get("employees")
            )
        elif name == "get_day_assignments":
            return self._tool_get_day_assignments(args["day"])
        elif name == "get_employee_schedule":
            return self._tool_get_employee_schedule(args["employee"])
        elif name == "get_employee_profile":
            return self._tool_get_employee_profile(args["employee"])
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
        elif name == "optimize_tobishi":
            return self._tool_optimize_tobishi(
                employees=args.get("employees"),
                max_swaps=args.get("max_swaps", 6),
            )
        elif name == "cleanup_yamamoto":
            return self._tool_cleanup_yamamoto()
        elif name == "apply_changes":
            return self._tool_apply_changes()
        elif name == "discard_changes":
            return self._tool_discard_changes()
        return f"不明なツール: {name}"

    # ========== チャットメイン ==========

    def _chat_anthropic(self, user_message: str, max_iterations: int) -> str:
        """Anthropic tool-use loop."""
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

    def _chat_openai(self, user_message: str, max_iterations: int) -> str:
        """OpenAI Responses API function-calling loop."""
        request: dict = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": user_message,
            "tools": _openai_tools(),
        }
        if self.openai_previous_response_id:
            request["previous_response_id"] = self.openai_previous_response_id

        response = self.client.responses.create(**request)
        for _ in range(max_iterations):
            tool_calls = [
                item for item in getattr(response, "output", [])
                if getattr(item, "type", None) == "function_call"
            ]
            if not tool_calls:
                self.openai_previous_response_id = getattr(response, "id", None)
                output_text = str(getattr(response, "output_text", "") or "").strip()
                return output_text or "内容を確認しました。追加の条件を教えてください。"

            tool_outputs = []
            for tool_call in tool_calls:
                raw_arguments = getattr(tool_call, "arguments", "{}") or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                result = self._execute_tool(
                    str(getattr(tool_call, "name", "")), arguments
                )
                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": str(getattr(tool_call, "call_id", "")),
                    "output": result,
                })

            response = self.client.responses.create(
                model=self.model,
                instructions=SYSTEM_PROMPT,
                previous_response_id=response.id,
                input=tool_outputs,
                tools=_openai_tools(),
            )

        self.openai_previous_response_id = getattr(response, "id", None)
        return "（応答生成中にツール呼び出しが多すぎました）"

    def chat(self, user_message: str, max_iterations: int = 10) -> str:
        """ユーザーメッセージに応答（ツール呼び出しを含む）。"""
        normalized_message = str(user_message or "")
        if (
            "飛び石" in normalized_message
            and any(
                keyword in normalized_message
                for keyword in ("減ら", "改善", "調整", "なく", "最適")
            )
        ):
            requested_names = [
                employee.name for employee in ALL_EMPLOYEES
                if employee.name in normalized_message
            ]
            return self._tool_optimize_tobishi(
                employees=requested_names or None,
                max_swaps=6,
            )
        if self.provider == "openai":
            return self._chat_openai(user_message, max_iterations)
        if self.provider == "local":
            return (
                "自由文の解釈にはOpenAIまたはClaudeのAPIキーが必要です。"
                "キー未設定のままでも、上の「飛び石勤務」または「山本」の"
                "機械的な再調整は利用できます。"
            )
        return self._chat_anthropic(user_message, max_iterations)


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
