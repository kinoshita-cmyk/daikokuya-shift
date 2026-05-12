"""
シフト検証エンジン
================================================
1ヶ月分のシフト（MonthlyShift）を入力として、
すべての制約（ハード制約・ソフト制約）を満たしているかを検証する。

使い方:
    from prototype.validator import validate
    result = validate(shift, prefs, prev_month, holiday_overrides)
    result.print_summary()
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from calendar import monthrange
from typing import Optional

from .models import (
    MonthlyShift, Store, Skill, OperationMode,
    PreferenceMark, PreviousMonthCarryover,
)
from .employees import ALL_EMPLOYEES, get_employee
from .rules import (
    NORMAL_CAPACITY, REDUCED_CAPACITY, MINIMUM_CAPACITY,
    HARD_CONSTRAINTS, OMIYA_ANCHOR_STAFF, HIGASHIGUCHI_ALLOWED_STAFF,
    YamamotoLogic, MAY_2026_HOLIDAY_OVERRIDES, DEFAULT_HOLIDAY_DAYS_MAY,
    CONSTRAINT_EXCLUDED, CONSEC_WORK_CHECK_APPLIES,
    get_capacity, OFF_MAIN_STORE_MINIMUMS,
)


# ============================================================
# 検証結果
# ============================================================

@dataclass
class Issue:
    """検出された問題1件"""
    severity: str        # "ERROR"=ハード制約違反, "WARNING"=ソフト制約違反
    category: str        # "店舗人数", "連勤", "休日数", "東口", etc.
    day: Optional[int]   # 該当日（全体問題の場合None）
    employee: Optional[str]  # 該当従業員（全体問題の場合None）
    message: str
    month: Optional[int] = None  # 何月のシフトか（validate() 内で shift.month から自動設定）

    def __str__(self) -> str:
        prefix = f"[{self.severity}] {self.category}"
        if self.day is not None:
            if self.month is not None:
                prefix += f" / {self.month}/{self.day}"
            else:
                prefix += f" / {self.day}日"
        if self.employee:
            prefix += f" / {self.employee}"
        return f"{prefix}: {self.message}"


@dataclass
class ValidationResult:
    """検証結果のまとめ"""
    issues: list[Issue] = field(default_factory=list)
    summary_stats: dict = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "ERROR" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARNING")

    def print_summary(self) -> None:
        print("=" * 60)
        print("【シフト検証結果】")
        print("=" * 60)
        if not self.issues:
            print("✅ すべての制約を満たしています")
        else:
            print(f"エラー: {self.error_count}件 / 警告: {self.warning_count}件\n")
            errors = [i for i in self.issues if i.severity == "ERROR"]
            warnings = [i for i in self.issues if i.severity == "WARNING"]
            if errors:
                print("--- エラー ---")
                for issue in errors:
                    print(f"  {issue}")
            if warnings:
                print("\n--- 警告 ---")
                for issue in warnings:
                    print(f"  {issue}")
        if self.summary_stats:
            print("\n--- 統計 ---")
            for k, v in self.summary_stats.items():
                print(f"  {k}: {v}")
        print("=" * 60)


# ============================================================
# 検証ロジック本体
# ============================================================

def validate(
    shift: MonthlyShift,
    work_requests: Optional[list] = None,
    off_requests: Optional[dict[str, list[int]]] = None,
    prev_month: Optional[list[PreviousMonthCarryover]] = None,
    holiday_overrides: Optional[dict[str, int]] = None,
    default_holidays: int = DEFAULT_HOLIDAY_DAYS_MAY,
    max_consec: Optional[int] = None,
    allow_omiya_short: bool = True,
    monthly_store_count_rules: Optional[list[dict]] = None,
) -> ValidationResult:
    """
    シフトを検証して問題リストを返す。

    Args:
        shift: 検証対象の1ヶ月シフト
        work_requests: [(name, day, store_or_none), ...] 出勤希望
        off_requests: {name: [day, ...]} 休み希望
        prev_month: 前月持ち越しデータ
        holiday_overrides: その月の個別休日日数指定
        default_holidays: 基本休日日数
    """
    result = ValidationResult()
    work_requests = work_requests or []
    off_requests = off_requests or {}
    prev_month = prev_month or []
    holiday_overrides = holiday_overrides or {}
    monthly_store_count_rules = monthly_store_count_rules or []

    days_in_month = monthrange(shift.year, shift.month)[1]

    # 1. 店舗別の必要人数チェック
    _check_store_capacity(shift, result, days_in_month, allow_omiya_short=allow_omiya_short)

    # 2. エコ配置チェック（東口・西口必須）
    _check_eco_placement(shift, result, days_in_month)

    # 3. 連勤チェック
    _check_consecutive_work(shift, result, days_in_month, prev_month, max_consec=max_consec)

    # 4. 休日日数チェック
    _check_holiday_days(shift, result, days_in_month, holiday_overrides, default_holidays)

    # 5. 連休チェック（2連休回数、3連休確認）
    _check_consecutive_off(shift, result, days_in_month, off_requests)

    # 6. 休み希望厳守チェック
    _check_off_requests(shift, result, off_requests)

    # 7. 出勤希望チェック
    _check_work_requests(shift, result, work_requests, off_requests)

    # 8. 大宮アンカースタッフ（春山・下地）チェック
    _check_omiya_anchor(shift, result, days_in_month)

    # 9. 東口の月曜休店チェック
    _check_higashiguchi_monday_closed(shift, result, days_in_month)

    # 10. 楯・春山・長尾のメイン店舗外勤務チェック
    _check_required_off_main_store_days(shift, result)

    # 11. 月別の追加配置ルールチェック
    _check_monthly_store_count_rules(shift, result, monthly_store_count_rules)

    # 12. 統計情報の集計
    _compute_stats(shift, result, days_in_month)

    # 全 Issue にシフトの月を埋め込む（表示時に "X/Y" 形式で出すため）
    for _issue in result.issues:
        if _issue.month is None:
            _issue.month = shift.month

    return result


# ============================================================
# 個別チェック関数
# ============================================================

def _check_store_capacity(
    shift: MonthlyShift, result: ValidationResult, days: int,
    allow_omiya_short: bool = True,
) -> None:
    """各日・各店舗の人数が必要数を満たしているか（詳細メッセージ付き）"""
    for day in range(1, days + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        capacity_map = get_capacity(mode)
        weekday = date(shift.year, shift.month, day).weekday()

        day_assignments = shift.get_day_assignments(day)

        for store, cap in capacity_map.items():
            if cap is None:
                continue
            # 休店曜日はスキップ
            if weekday in cap.closed_dow:
                continue

            # 補助要員は除外して人数集計
            store_workers = [
                a for a in day_assignments
                if a.store == store and not get_employee(a.employee).is_auxiliary
            ]
            # ECO_SUPPORT は店頭直接応対しないためチケット枠としてカウント
            eco_count = sum(
                1 for a in store_workers
                if get_employee(a.employee).skill == Skill.ECO
            )
            ticket_count = sum(
                1 for a in store_workers
                if get_employee(a.employee).skill in (Skill.TICKET, Skill.ECO_SUPPORT)
            )
            total = eco_count + ticket_count

            # 配属者の名前リスト
            worker_names = [a.employee for a in store_workers]
            worker_str = ", ".join(worker_names) if worker_names else "(誰もいない)"
            all_store_workers = [a.employee for a in day_assignments if a.store == store]
            all_worker_str = ", ".join(all_store_workers) if all_store_workers else "(誰もいない)"
            yamamoto_present = any(
                a.employee == YamamotoLogic.EMPLOYEE_NAME and a.store == store
                for a in day_assignments
            )

            # 赤羽東口店はエコ1名のみ。例外なし。
            if store == Store.HIGASHIGUCHI:
                unexpected_workers = [
                    name for name in all_store_workers
                    if name not in HIGASHIGUCHI_ALLOWED_STAFF
                ]
                if eco_count != 1 or ticket_count != 0 or total != 1:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="1名体制（東口）",
                        day=day, employee=None,
                        message=(
                            f"赤羽東口店はエコ1名のみです"
                            f"／配属: {all_worker_str}"
                            f"／エコ{eco_count}名・チケット{ticket_count}名"
                        ),
                    ))
                if unexpected_workers:
                    result.issues.append(Issue(
                        severity="WARNING",
                        category="東口代替要員",
                        day=day, employee=None,
                        message=(
                            f"赤羽東口店に配置できるのは土井さん、楯さん、春山さん、長尾さん、今津さんのみです"
                            f"／対象: {', '.join(unexpected_workers)}"
                            "。自動生成では避ける条件として扱います。"
                        ),
                    ))
                continue

            # 赤羽駅前店: エコ1+チケット2が基本。
            # エコ2+チケット1も可。山本さんはチケット対応不足時のみ補助扱い。
            if store == Store.AKABANE and mode == OperationMode.NORMAL:
                effective_ticket = ticket_count + max(0, eco_count - 1)
                if yamamoto_present:
                    effective_ticket += 1
                if eco_count < 1:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="店舗人数",
                        day=day, employee=None,
                        message=f"赤羽駅前店 エコ要員不足／配属: {all_worker_str}",
                    ))
                if eco_count > 2:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="店舗人数",
                        day=day, employee=None,
                        message=f"赤羽駅前店 エコ要員が多すぎます（上限2名）／配属: {all_worker_str}",
                    ))
                if effective_ticket < 2:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="店舗人数",
                        day=day, employee=None,
                        message=(
                            f"赤羽駅前店 チケット対応が不足"
                            f"（必要2名分、実質{effective_ticket}名分）"
                            f"／配属: {all_worker_str}"
                        ),
                    ))
                continue

            # 大宮の「人数少」例外: eco_count==1 + ticket_count>=1 でも許容（警告のみ）
            if store == Store.OMIYA and mode == OperationMode.NORMAL:
                if eco_count >= 1 and ticket_count >= 1 and total >= 3:
                    continue
                if allow_omiya_short and eco_count == 1 and ticket_count == 1 and total == 2:
                    result.issues.append(Issue(
                        severity="WARNING",
                        category="人数少（大宮）",
                        day=day, employee=None,
                        message=(
                            f"大宮駅前店 2名体制（最終手段）"
                            f"／配属: {worker_str}"
                            f"／通常は最低3名"
                        ),
                    ))
                    continue

            # すずらん: エコ1〜2名 + チケット2名
            if store == Store.SUZURAN and mode == OperationMode.NORMAL:
                if 1 <= eco_count <= 2 and ticket_count == 2:
                    continue  # OK
                if ticket_count > 2:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="店舗人数",
                        day=day, employee=None,
                        message=(
                            f"大宮すずらん通り店 チケット要員が多すぎます"
                            f"（原則2名、実績{ticket_count}名）／配属: {worker_str}"
                        ),
                    ))
                    continue

            if eco_count < cap.eco_min:
                shortage = cap.eco_min - eco_count
                result.issues.append(Issue(
                    severity="ERROR",
                    category="店舗人数",
                    day=day, employee=None,
                    message=(
                        f"{store.display_name} エコ要員 {shortage}名不足"
                        f"（必要{cap.eco_min}名、実績{eco_count}名）"
                        f"／配属: {worker_str}"
                    ),
                ))
            if eco_count > cap.eco_max:
                result.issues.append(Issue(
                    severity="ERROR",
                    category="店舗人数",
                    day=day, employee=None,
                    message=(
                        f"{store.display_name} エコ要員が多すぎます"
                        f"（上限{cap.eco_max}名、実績{eco_count}名）"
                        f"／配属: {worker_str}"
                    ),
                ))
            if ticket_count < cap.ticket_min:
                shortage = cap.ticket_min - ticket_count
                result.issues.append(Issue(
                    severity="ERROR",
                    category="店舗人数",
                    day=day, employee=None,
                    message=(
                        f"{store.display_name} チケット要員 {shortage}名不足"
                        f"（必要{cap.ticket_min}名、実績{ticket_count}名）"
                        f"／配属: {worker_str}"
                    ),
                ))


def _check_eco_placement(shift: MonthlyShift, result: ValidationResult, days: int) -> None:
    """東口・西口に必ずエコ1名以上いるか"""
    for day in range(1, days + 1):
        for store in (Store.HIGASHIGUCHI, Store.NISHIGUCHI):
            day_assignments = shift.get_day_assignments(day)
            eco_at_store = [
                a for a in day_assignments
                if a.store == store and get_employee(a.employee).skill == Skill.ECO
            ]
            if not eco_at_store:
                # その日の店舗が休店or閉店ならスキップ
                mode = shift.operation_modes.get(day, OperationMode.NORMAL)
                if mode in (OperationMode.MINIMUM, OperationMode.CLOSED):
                    continue
                # 東口の月曜は休店なのでスキップ
                if store == Store.HIGASHIGUCHI:
                    weekday = date(shift.year, shift.month, day).weekday()
                    if weekday == 0:
                        continue
                result.issues.append(Issue(
                    severity="ERROR",
                    category="必須エコ",
                    day=day, employee=None,
                    message=f"{store.display_name} エコ要員未配置",
                ))


def _check_consecutive_work(
    shift: MonthlyShift, result: ValidationResult, days: int,
    prev_month: list[PreviousMonthCarryover],
    max_consec: Optional[int] = None,
) -> None:
    """最大連勤チェック（前月持ち越し含む）"""
    if max_consec is None:
        max_consec = HARD_CONSTRAINTS["max_consecutive_work_days"]

    for emp in ALL_EMPLOYEES:
        if not emp.is_shift_eligible:
            continue
        if emp.name in CONSTRAINT_EXCLUDED and emp.name not in CONSEC_WORK_CHECK_APPLIES:
            continue

        # 前月最終日からの連続出勤日数を取得
        prev_consec = 0
        for p in prev_month:
            if p.employee == emp.name and p.last_working_days:
                # 最後の連続日を数える（4/30, 4/29, 4/28 が連続なら3）
                sorted_days = sorted(p.last_working_days, reverse=True)
                last_month_days = monthrange(shift.year, shift.month - 1 or 12)[1]
                expected = last_month_days
                for d in sorted_days:
                    if d == expected:
                        prev_consec += 1
                        expected -= 1
                    else:
                        break

        # 当月の出勤を順に走査
        consec = prev_consec
        for day in range(1, days + 1):
            a = shift.get_assignment(emp.name, day)
            is_working = a is not None and a.store != Store.OFF and not emp.is_auxiliary
            if is_working:
                consec += 1
                if consec > max_consec:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="連勤",
                        day=day, employee=emp.name,
                        message=f"{consec}連勤（上限{max_consec}）",
                    ))
            else:
                consec = 0


def _check_holiday_days(
    shift: MonthlyShift, result: ValidationResult, days: int,
    overrides: dict[str, int], default_days: int,
) -> None:
    """月内の休日日数チェック"""
    for emp in ALL_EMPLOYEES:
        if not emp.is_shift_eligible:
            continue
        if emp.name in CONSTRAINT_EXCLUDED:
            continue

        required = overrides.get(emp.name, default_days)
        actual_off = sum(
            1 for day in range(1, days + 1)
            if (a := shift.get_assignment(emp.name, day)) is None or a.store == Store.OFF
        )
        if actual_off < required:
            result.issues.append(Issue(
                severity="ERROR",
                category="休日数",
                day=None, employee=emp.name,
                message=f"休日{actual_off}日（必要{required}日）",
            ))


def _check_consecutive_off(
    shift: MonthlyShift, result: ValidationResult, days: int,
    off_requests: dict[str, list[int]],
) -> None:
    """2連休回数（1〜2回）と3連休の確認"""
    min_2off = HARD_CONSTRAINTS["min_two_day_off_per_month"]
    max_2off = HARD_CONSTRAINTS["max_two_day_off_per_month"]

    for emp in ALL_EMPLOYEES:
        if not emp.is_shift_eligible:
            continue
        if emp.name in CONSTRAINT_EXCLUDED:
            continue

        emp_off_requests = set(off_requests.get(emp.name, []))

        consec_off = 0
        two_off_count = 0
        for day in range(1, days + 1):
            a = shift.get_assignment(emp.name, day)
            is_off = a is None or a.store == Store.OFF
            if is_off:
                consec_off += 1
            else:
                if consec_off >= 2:
                    two_off_count += 1
                if consec_off >= 3:
                    # 3連休になった日（最終日）を特定
                    third_day = day - 1
                    off_block = list(range(day - consec_off, day))
                    # 寛容版: 連休のうち1日でも希望休（または柔軟休み候補）が含まれていれば許容
                    has_request = any(d in emp_off_requests for d in off_block)
                    if not has_request:
                        result.issues.append(Issue(
                            severity="WARNING",
                            category="3連休確認",
                            day=third_day, employee=emp.name,
                            message=(
                                f"{consec_off}連休"
                                f"（{shift.month}/{off_block[0]}〜{shift.month}/{off_block[-1]}）"
                                "。人数過多などの事情があれば許容可能です。"
                            ),
                        ))
                consec_off = 0

        # 月末の処理
        if consec_off >= 2:
            two_off_count += 1
            if consec_off >= 3:
                off_block = list(range(days - consec_off + 1, days + 1))
                has_request = any(d in emp_off_requests for d in off_block)
                if not has_request:
                    result.issues.append(Issue(
                        severity="WARNING",
                        category="3連休確認",
                        day=days, employee=emp.name,
                        message=(
                            f"{consec_off}連休"
                            f"（{shift.month}/{off_block[0]}〜{shift.month}/{off_block[-1]}）"
                            "。人数過多などの事情があれば許容可能です。"
                        ),
                    ))

        if two_off_count < min_2off:
            result.issues.append(Issue(
                severity="WARNING",
                category="2連休不足",
                day=None, employee=emp.name,
                message=f"2連休{two_off_count}回（最低{min_2off}回必要）",
            ))
        if two_off_count > max_2off:
            result.issues.append(Issue(
                severity="WARNING",
                category="2連休過多",
                day=None, employee=emp.name,
                message=f"2連休{two_off_count}回（最大{max_2off}回）",
            ))


def _check_off_requests(
    shift: MonthlyShift, result: ValidationResult,
    off_requests: dict[str, list[int]],
) -> None:
    """休み希望日が必ず休みになっているか"""
    for emp_name, days_off in off_requests.items():
        for day in days_off:
            a = shift.get_assignment(emp_name, day)
            if a is not None and a.store != Store.OFF:
                result.issues.append(Issue(
                    severity="ERROR",
                    category="休み希望未充足",
                    day=day, employee=emp_name,
                    message=f"休み希望なのに{a.store.display_name}に配置",
                ))


def _check_work_requests(
    shift: MonthlyShift, result: ValidationResult,
    work_requests: list,
    off_requests: dict[str, list[int]],
) -> None:
    """出勤希望日が出勤になっているか（指定店舗があればそこに配置）"""
    for name, day, requested_store in work_requests:
        # 山本さんは赤羽不足時の補助・手動入力枠なので、通常スタッフと同じ
        # 「出勤希望未充足」警告には含めない。×休み希望は別チェックで厳守する。
        if name == "山本":
            continue
        # 同じ日に「×」休み希望がある場合は、休み希望を最優先する。
        if day in set(off_requests.get(name, [])):
            continue
        a = shift.get_assignment(name, day)
        if a is None or a.store == Store.OFF:
            result.issues.append(Issue(
                severity="ERROR",
                category="出勤希望未充足",
                day=day, employee=name,
                message="出勤希望なのに休みに配置",
            ))
        elif requested_store and a.store != requested_store:
            result.issues.append(Issue(
                severity="WARNING",
                category="希望店舗不一致",
                day=day, employee=name,
                message=f"希望{requested_store.display_name}, 実配置{a.store.display_name}",
            ))


def _check_omiya_anchor(shift: MonthlyShift, result: ValidationResult, days: int) -> None:
    """大宮店に春山または下地のどちらか1人は必ずいる"""
    for day in range(1, days + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        # 最小営業モードでは大宮駅前店は営業するが春山・下地ルール適用
        omiya_workers = [
            a.employee for a in shift.get_day_assignments(day)
            if a.store == Store.OMIYA
        ]
        if not any(name in omiya_workers for name in OMIYA_ANCHOR_STAFF):
            if omiya_workers:  # 大宮店が営業している日のみチェック
                result.issues.append(Issue(
                    severity="ERROR",
                    category="大宮アンカー",
                    day=day, employee=None,
                    message=f"大宮駅前店に{OMIYA_ANCHOR_STAFF}のいずれもいない",
                ))


def _check_higashiguchi_monday_closed(
    shift: MonthlyShift, result: ValidationResult, days: int,
) -> None:
    """東口は月曜休店（誰も配置されていてはいけない）"""
    for day in range(1, days + 1):
        weekday = date(shift.year, shift.month, day).weekday()
        if weekday != 0:
            continue
        higashi_workers = [
            a.employee for a in shift.get_day_assignments(day)
            if a.store == Store.HIGASHIGUCHI
        ]
        if higashi_workers:
            result.issues.append(Issue(
                severity="ERROR",
                category="東口月曜休店",
                day=day, employee=None,
                message=f"東口に{higashi_workers}が配置（月曜休店）",
            ))


def _check_required_off_main_store_days(
    shift: MonthlyShift, result: ValidationResult,
) -> None:
    """楯・春山・長尾が月3日以上メイン店舗以外で勤務しているか。"""
    for emp_name, (main_store, min_count) in OFF_MAIN_STORE_MINIMUMS.items():
        outside_days = [
            a.day for a in shift.assignments
            if a.employee == emp_name
            and a.store not in (Store.OFF, main_store)
        ]
        if len(outside_days) < min_count:
            result.issues.append(Issue(
                severity="ERROR",
                category="メイン店舗外勤務",
                day=None,
                employee=emp_name,
                message=(
                    f"メイン店舗（{main_store.display_name}）以外の勤務が"
                    f"{len(outside_days)}日です。最低{min_count}日必要です。"
                ),
            ))


def _store_from_rule_value(value) -> Optional[Store]:
    """月別ルールの店舗表記を Store に変換する。"""
    if isinstance(value, Store):
        return value
    value_str = str(value)
    try:
        return Store[value_str]
    except KeyError:
        pass
    for store in Store:
        if store.display_name == value_str or store.value == value_str:
            return store
    return None


def _check_monthly_store_count_rules(
    shift: MonthlyShift, result: ValidationResult, rules: list[dict],
) -> None:
    """月別追加ルール（特定スタッフを指定店舗へN回）を検証する。"""
    for rule in rules:
        if not rule or not rule.get("employee"):
            continue
        emp_name = str(rule.get("employee"))
        stores = [
            s for s in (_store_from_rule_value(v) for v in (rule.get("stores") or []))
            if s is not None and s != Store.OFF
        ]
        if not stores:
            continue
        try:
            required_count = int(rule.get("count") or 0)
        except (TypeError, ValueError):
            required_count = 0
        if required_count <= 0:
            continue
        actual_count = sum(
            1 for a in shift.assignments
            if a.employee == emp_name and a.store in stores
        )
        if actual_count >= required_count:
            continue
        severity = str(rule.get("severity") or "WARNING").upper()
        store_names = "・".join(s.display_name for s in stores)
        result.issues.append(Issue(
            severity="ERROR" if severity == "ERROR" else "WARNING",
            category="月別ルール",
            day=None,
            employee=emp_name,
            message=(
                f"{rule.get('name') or '月別追加ルール'}: "
                f"{store_names}への勤務が{actual_count}日です。"
                f"{required_count}日以上必要です。"
            ),
        ))


def _compute_stats(shift: MonthlyShift, result: ValidationResult, days: int) -> None:
    """
    統計情報の集計（経営側可視化用）

    重要: 月間目標出勤日数の未達は「情報提供のみ」で、エラーや警告にはしない。
    会社方針として、月単位での過不足は他の月で調整しない運用のため。
    ただし経営判断材料として「目標 vs 実績」の数字は明示する。
    """
    # 月別目標出勤日数の計算（年間基準を12分割したもの）
    # 簡易版: 年間 / 12 を四捨五入。本来は月別に細かく按分（rules.py のロジック参照予定）
    def get_monthly_target(emp) -> Optional[int]:
        if emp.annual_target_days is None:
            return None
        return round(emp.annual_target_days / 12)

    # 各従業員の出勤日数・休日日数・目標達成度
    for emp in ALL_EMPLOYEES:
        if emp.is_auxiliary:
            continue
        work_days = sum(
            1 for d in range(1, days + 1)
            if (a := shift.get_assignment(emp.name, d)) and a.store != Store.OFF
        )
        off_days = sum(
            1 for d in range(1, days + 1)
            if (a := shift.get_assignment(emp.name, d)) is None or a.store == Store.OFF
        )

        target = get_monthly_target(emp)
        if target is not None:
            diff = work_days - target
            if diff < 0:
                diff_str = f"  📉 -{abs(diff)}日（不足）"
            elif diff > 0:
                diff_str = f"  📈 +{diff}日（超過）"
            else:
                diff_str = f"  ✓ ぴったり"
            result.summary_stats[f"{emp.name}"] = (
                f"出勤{work_days}日 / 休{off_days}日 / 目標{target}日{diff_str}"
            )
        else:
            result.summary_stats[f"{emp.name}"] = (
                f"出勤{work_days}日 / 休{off_days}日 / 目標：定めなし"
            )

    # 各日の総人数（人数不足日のリスト・詳細付き）
    short_days = []
    for day in range(1, days + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        capacity_map = get_capacity(mode)
        weekday = date(shift.year, shift.month, day).weekday()

        # 営業対象店舗の必要人数（休店曜日は除外）
        required_total = 0
        for store, cap in capacity_map.items():
            if weekday in cap.closed_dow:
                continue
            required_total += cap.eco_min + cap.ticket_min

        actual_total = sum(
            1 for a in shift.get_day_assignments(day)
            if a.store != Store.OFF and not get_employee(a.employee).is_auxiliary
        )
        if actual_total < required_total:
            shortage = required_total - actual_total
            short_days.append(f"5/{day}({actual_total}/{required_total}, -{shortage}名)")
    if short_days:
        result.summary_stats["⚠ 人数不足日"] = ", ".join(short_days)

    # 全体の目標達成サマリー（経営側可視化用）
    total_target = 0
    total_actual = 0
    shortfall_employees = []
    for emp in ALL_EMPLOYEES:
        if emp.is_auxiliary or emp.annual_target_days is None:
            continue
        target = round(emp.annual_target_days / 12)
        actual = sum(
            1 for d in range(1, days + 1)
            if (a := shift.get_assignment(emp.name, d)) and a.store != Store.OFF
        )
        total_target += target
        total_actual += actual
        if actual < target:
            shortfall_employees.append(f"{emp.name}({actual}/{target})")

    result.summary_stats["📊 月間総出勤日数"] = (
        f"目標{total_target}日 vs 実績{total_actual}日 "
        f"（差分{total_actual - total_target:+d}日）"
    )
    if shortfall_employees:
        result.summary_stats["📋 目標未達のメンバー"] = (
            f"{len(shortfall_employees)}名: {', '.join(shortfall_employees)}"
        )


if __name__ == "__main__":
    print("Validator module loaded successfully.")
    print("Use validate(shift, ...) to run validation.")
