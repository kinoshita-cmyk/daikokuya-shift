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
    PreferenceMark, PreviousMonthCarryover, Affinity,
)
from .employees import ALL_EMPLOYEES, get_employee
try:
    from .employees import is_probationary_employee as _employee_is_probationary_employee
except ImportError:
    _employee_is_probationary_employee = None
from .rules import (
    NORMAL_CAPACITY, REDUCED_CAPACITY, MINIMUM_CAPACITY,
    HARD_CONSTRAINTS, OMIYA_ANCHOR_STAFF, HIGASHIGUCHI_ALLOWED_STAFF,
    YamamotoLogic, MAY_2026_HOLIDAY_OVERRIDES, DEFAULT_HOLIDAY_DAYS_MAY,
    CONSTRAINT_EXCLUDED, CONSEC_WORK_CHECK_APPLIES,
    get_capacity, STORE_ROTATION_MINIMUMS,
    MAKINO_NISHIGUCHI_TRAINING_PARTNER,
    STORE_KEYHOLDERS, SUZURAN_KEY_SUPPORT_FROM_OMIYA,
    STORE_STAFFING_LIMITS, GLOBAL_DAILY_STAFFING_LIMIT,
    get_monthly_work_target,
    get_monthly_required_holiday_days,
    FORBIDDEN_SAME_STORE_PAIRINGS, FORBIDDEN_SAME_STORE_GROUPS,
    MANDATORY_WORK_ON_REQUEST_EMPLOYEES, MONTH_END_START_OMIYA_STAFF,
    WORK_TARGET_SHORTFALL_WARNING_DIFF_DAYS,
    WORK_TARGET_OVERAGE_WARNING_DIFF_DAYS,
    WORK_TARGET_ERROR_DIFF_DAYS,
)


def _parse_iso_date(value: str | None) -> Optional[date]:
    """YYYY-MM-DD の入社日を date に変換する。"""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _add_months(value: date, months: int) -> date:
    """月末日を考慮して date に月数を足す。"""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def is_probationary_employee(
    employee,
    target_year: int,
    target_month: int,
    target_day: Optional[int] = None,
) -> bool:
    """
    入社日から2か月間の試用期間かどうか。

    employees.py 側に同名関数がある場合はそれを使い、
    ない環境でも validator.py 単体で起動できるようにする。
    """
    if _employee_is_probationary_employee is not None:
        try:
            return bool(_employee_is_probationary_employee(
                employee, target_year, target_month, target_day,
            ))
        except TypeError:
            try:
                return bool(_employee_is_probationary_employee(
                    employee, target_year, target_month,
                ))
            except Exception:
                pass
        except Exception:
            pass

    hired_on = _parse_iso_date(getattr(employee, "hired_at", None))
    if hired_on is None:
        return False

    probation_end = _add_months(hired_on, 2)
    if target_day is not None:
        try:
            target = date(int(target_year), int(target_month), int(target_day))
        except ValueError:
            return False
        return hired_on <= target < probation_end

    month_start = date(int(target_year), int(target_month), 1)
    next_month = _add_months(month_start, 1)
    return month_start < probation_end and next_month > hired_on


def _validation_employees() -> list:
    """検証対象の従業員を従業員マスターから動的に取得する。"""
    def _is_active_for_validation(emp) -> bool:
        if getattr(emp, "is_auxiliary", False):
            return False
        if not getattr(emp, "is_shift_eligible", True):
            return False

        role = getattr(emp, "role", None)
        role_text = str(getattr(role, "value", role))
        if role_text in ("顧問", "ADVISOR", "代表取締役", "REPRESENTATIVE"):
            return False

        status = getattr(emp, "employment_status", None)
        if status is not None:
            status_text = str(getattr(status, "value", status))
            if status_text not in ("正社員", "パート", "ACTIVE", "PART_TIME"):
                return False
        return True

    try:
        from .employee_config import get_all_employees_including_retired

        return [
            emp for emp in get_all_employees_including_retired()
            if _is_active_for_validation(emp)
        ]
    except Exception:
        return [
            emp for emp in ALL_EMPLOYEES
            if _is_active_for_validation(emp)
        ]


# ============================================================
# 検証結果
# ============================================================

@dataclass
class Issue:
    """検出された問題1件"""
    severity: str        # "ERROR"=ハード制約違反, "WARNING"=ソフト制約違反, "INFO"=参考情報
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
    exact_holiday_days: Optional[dict[str, int]] = None,
    employee_max_consecutive_work: Optional[dict[str, int]] = None,
    employee_max_consecutive_off: Optional[dict[str, int]] = None,
    default_holidays: int = DEFAULT_HOLIDAY_DAYS_MAY,
    max_consec: Optional[int] = None,
    allow_omiya_short: bool = True,
    monthly_store_count_rules: Optional[list[dict]] = None,
    required_assignments: Optional[list[dict]] = None,
    preferred_work_requests: Optional[list] = None,
    preferred_work_groups: Optional[list] = None,
) -> ValidationResult:
    """
    シフトを検証して問題リストを返す。

    Args:
        shift: 検証対象の1ヶ月シフト
        work_requests: [(name, day, store_or_none), ...] 出勤希望
        preferred_work_requests: [(name, day, store_or_none), ...] 自由記載から抽出した単日出勤希望
        preferred_work_groups: [(name, [day, ...], required_count, store_or_none), ...] 自由記載の候補日出勤希望
        off_requests: {name: [day, ...]} 休み希望
        prev_month: 前月持ち越しデータ
        holiday_overrides: その月の個別休日日数指定
        default_holidays: 基本休日日数
    """
    result = ValidationResult()
    work_requests = work_requests or []
    preferred_work_requests = preferred_work_requests or []
    preferred_work_groups = preferred_work_groups or []
    off_requests = off_requests or {}
    prev_month = prev_month or []
    holiday_overrides = holiday_overrides or {}
    exact_holiday_days = exact_holiday_days or {}
    employee_max_consecutive_work = employee_max_consecutive_work or {}
    employee_max_consecutive_off = employee_max_consecutive_off or {}
    monthly_store_count_rules = monthly_store_count_rules or []
    required_assignments = required_assignments or []

    days_in_month = monthrange(shift.year, shift.month)[1]

    # 1. 店舗別の必要人数チェック
    _check_store_capacity(shift, result, days_in_month, allow_omiya_short=allow_omiya_short)

    # 2. 1日全体の人数上限チェック
    _check_daily_staffing_limit(shift, result, days_in_month)

    # 3. エコ配置チェック（東口・西口必須）
    _check_eco_placement(shift, result, days_in_month)

    # 4. 連勤チェック
    _check_consecutive_work(
        shift, result, days_in_month, prev_month,
        max_consec=max_consec,
        employee_max_consecutive_work=employee_max_consecutive_work,
    )

    # 5. 休日日数チェック
    _check_holiday_days(
        shift, result, days_in_month,
        holiday_overrides, default_holidays,
        exact_holiday_days=exact_holiday_days,
    )

    # 6. 連休チェック（2連休回数、3連休確認）
    _check_consecutive_off(
        shift, result, days_in_month, off_requests,
        employee_max_consecutive_off=employee_max_consecutive_off,
    )

    # 7. 休み希望厳守チェック
    _check_off_requests(shift, result, off_requests)

    # 8. 絶対配置不可チェック
    _check_absolute_forbidden_assignments(
        shift, result, days_in_month, monthly_store_count_rules,
    )

    # 9. エコメンバー同店舗同勤務NGチェック
    _check_forbidden_same_store_pairings(shift, result, days_in_month)

    # 10. 南さんなど、出勤希望日が絶対出勤扱いのスタッフ
    _check_mandatory_work_on_request(
        shift,
        result,
        work_requests,
        preferred_work_requests,
        preferred_work_groups,
        off_requests,
    )

    # 11. 出勤希望チェック
    _check_work_requests(shift, result, work_requests, off_requests)

    # 12. 大宮アンカースタッフ（春山・下地）チェック
    _check_omiya_anchor(shift, result, days_in_month)

    # 13. 月末月初の大宮駅前固定チェック
    _check_month_end_start_omiya(shift, result, days_in_month, off_requests)

    # 14. 東口の月曜休店チェック
    _check_higashiguchi_monday_closed(shift, result, days_in_month)

    # 15. 月内の最低巡回条件チェック
    _check_store_rotation_minimums(shift, result)

    # 16. 月別の追加配置ルールチェック
    _check_monthly_store_count_rules(shift, result, monthly_store_count_rules)

    # 17. 月別の日付指定配置ルールチェック
    _check_required_assignment_rules(shift, result, required_assignments)

    # 18. 牧野さんの東口・西口研修ルールチェック
    _check_makino_training_rules(shift, result, days_in_month, monthly_store_count_rules)

    # 19. 店舗鍵担当チェック（警告表示のみ。生成の制約にはしない）
    _check_store_keyholders(shift, result, days_in_month)

    # 20. 主担当なし・通常対応可複数店舗の偏りチェック
    _check_balanced_normal_store_assignments(shift, result)

    # 21. 月間勤務日数バランスチェック
    _check_monthly_workday_balance(
        shift,
        result,
        days_in_month,
        off_requests,
        holiday_overrides,
        exact_holiday_days,
    )

    # 22. 統計情報の集計
    _compute_stats(shift, result, days_in_month)

    # 全 Issue にシフトの月を埋め込む（表示時に "X/Y" 形式で出すため）
    for _issue in result.issues:
        if _issue.month is None:
            _issue.month = shift.month

    return result


# ============================================================
# 個別チェック関数
# ============================================================

def _check_daily_staffing_limit(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
) -> None:
    """1日全体の人数が許容範囲に収まっているか確認する。"""
    for day in range(1, days + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue

        workers = sorted({
            a.employee
            for a in shift.get_day_assignments(day)
            if a.store != Store.OFF
        })
        total = len(workers)
        worker_str = ", ".join(workers) if workers else "(誰もいない)"

        if total > GLOBAL_DAILY_STAFFING_LIMIT.max_total:
            result.issues.append(Issue(
                severity="ERROR",
                category="全体人数上限",
                day=day,
                employee=None,
                message=(
                    f"全体の出勤人数が{total}名です"
                    f"（最大{GLOBAL_DAILY_STAFFING_LIMIT.max_total}名）"
                    f"／出勤者: {worker_str}"
                ),
            ))
        elif total > GLOBAL_DAILY_STAFFING_LIMIT.standard_total:
            is_exceptional_total = total >= GLOBAL_DAILY_STAFFING_LIMIT.max_total
            result.issues.append(Issue(
                severity="WARNING" if is_exceptional_total else "INFO",
                category="全体人数多め",
                day=day,
                employee=None,
                message=(
                    f"全体の出勤人数が{total}名です"
                    f"（標準{GLOBAL_DAILY_STAFFING_LIMIT.standard_total}名、"
                    f"最大{GLOBAL_DAILY_STAFFING_LIMIT.max_total}名まで）"
                    f"／出勤者: {worker_str}"
                ),
            ))


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
                if a.store == store
                and not get_employee(a.employee).is_auxiliary
                and not is_probationary_employee(
                    get_employee(a.employee), shift.year, shift.month, day,
                )
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
            all_store_workers = [
                a.employee for a in day_assignments
                if a.store == store
                and not is_probationary_employee(
                    get_employee(a.employee), shift.year, shift.month, day,
                )
            ]
            all_worker_str = ", ".join(all_store_workers) if all_store_workers else "(誰もいない)"
            staffing_total = len(all_store_workers)
            yamamoto_present = any(
                a.employee == YamamotoLogic.EMPLOYEE_NAME and a.store == store
                for a in day_assignments
            )
            staffing_limit = STORE_STAFFING_LIMITS.get(store)
            if staffing_limit is not None:
                if staffing_total > staffing_limit.max_total:
                    akabane_five_person_exception = (
                        store == Store.AKABANE
                        and staffing_total == staffing_limit.max_total + 1
                    )
                    result.issues.append(Issue(
                        severity="WARNING" if akabane_five_person_exception else "ERROR",
                        category=(
                            "店舗人数多め"
                            if akabane_five_person_exception
                            else "店舗人数上限"
                        ),
                        day=day, employee=None,
                        message=(
                            f"{store.display_name}が{staffing_total}名です"
                            f"（原則最大{staffing_limit.max_total}名）"
                            f"／配属: {all_worker_str}"
                        ),
                    ))
                elif staffing_total > staffing_limit.standard_total:
                    result.issues.append(Issue(
                        severity="WARNING",
                        category="店舗人数多め",
                        day=day, employee=None,
                        message=(
                            f"{store.display_name}が{staffing_total}名です"
                            f"（標準{staffing_limit.standard_total}名）"
                            f"／配属: {all_worker_str}"
                        ),
                    ))

            # 赤羽東口店はエコ1名のみ。例外なし。
            if store == Store.HIGASHIGUCHI:
                unexpected_workers = [
                    name for name in all_store_workers
                    if name != "顧問" and name not in HIGASHIGUCHI_ALLOWED_STAFF
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
            # エコ担当はチケット対応も可能なため、エコ必須分を超えた人数は
            # チケット対応分として数える。山本さんは不足時のみ補助扱い。
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

            # 大宮の「人数少」例外: エコ対応者1名以上 + 2名体制なら許容（警告のみ）
            if store == Store.OMIYA and mode == OperationMode.NORMAL:
                if eco_count >= 1 and total >= 3:
                    continue
                if allow_omiya_short and eco_count >= 1 and total == 2:
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

            # すずらん: エコ対応者1名以上 + 合計3名以上。
            # エコ担当はチケット対応もできるため、チケット専任2名に固定しない。
            if store == Store.SUZURAN and mode == OperationMode.NORMAL:
                if eco_count >= 1 and total >= 3:
                    if ticket_count > 2:
                        result.issues.append(Issue(
                            severity="WARNING",
                            category="店舗人数多め",
                            day=day, employee=None,
                            message=(
                                f"大宮すずらん通り店 チケット要員が多めです"
                                f"（原則2名、実績{ticket_count}名）／配属: {worker_str}"
                            ),
                        ))
                    continue  # OK
                if ticket_count > 2:
                    result.issues.append(Issue(
                        severity="WARNING",
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
            required_total = cap.eco_min + cap.ticket_min
            if total < required_total:
                shortage = required_total - total
                result.issues.append(Issue(
                    severity="ERROR",
                    category="店舗人数",
                    day=day, employee=None,
                    message=(
                        f"{store.display_name} 人員 {shortage}名不足"
                        f"（必要{required_total}名、実績{total}名）"
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
                if a.store == store
                and get_employee(a.employee).skill == Skill.ECO
                and not is_probationary_employee(
                    get_employee(a.employee), shift.year, shift.month, day,
                )
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
    employee_max_consecutive_work: Optional[dict[str, int]] = None,
) -> None:
    """最大連勤チェック（前月持ち越し含む）"""
    if max_consec is None:
        max_consec = HARD_CONSTRAINTS["max_consecutive_work_days"]
    employee_max_consecutive_work = employee_max_consecutive_work or {}

    for emp in _validation_employees():
        if not emp.is_shift_eligible:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
            continue
        if (
            emp.name in CONSTRAINT_EXCLUDED
            and emp.name not in CONSEC_WORK_CHECK_APPLIES
            and emp.name not in employee_max_consecutive_work
        ):
            continue
        emp_max_consec = min(
            int(max_consec),
            int(employee_max_consecutive_work.get(emp.name, max_consec)),
        )

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
                if consec > emp_max_consec:
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="連勤",
                        day=day, employee=emp.name,
                        message=f"{consec}連勤（上限{emp_max_consec}）",
                    ))
            else:
                consec = 0


def _check_holiday_days(
    shift: MonthlyShift, result: ValidationResult, days: int,
    overrides: dict[str, int], default_days: int,
    exact_holiday_days: Optional[dict[str, int]] = None,
) -> None:
    """月内の休日日数チェック"""
    exact_holiday_days = exact_holiday_days or {}
    for emp in _validation_employees():
        if not emp.is_shift_eligible:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
            continue
        if (
            emp.name in CONSTRAINT_EXCLUDED
            and emp.name not in overrides
            and emp.name not in exact_holiday_days
        ):
            continue

        required = overrides.get(
            emp.name,
            get_monthly_required_holiday_days(
                emp.name,
                shift.month,
                days,
                emp.annual_target_days,
                default_days,
            ),
        )
        actual_off = sum(
            1 for day in range(1, days + 1)
            if (a := shift.get_assignment(emp.name, day)) is None or a.store == Store.OFF
        )
        if emp.name in exact_holiday_days:
            expected = int(exact_holiday_days[emp.name])
            if actual_off < expected:
                result.issues.append(Issue(
                    severity="ERROR",
                    category="休日数",
                    day=None, employee=emp.name,
                    message=f"休日{actual_off}日（指定{expected}日、{expected - actual_off}日不足）",
                ))
            elif actual_off > expected:
                result.issues.append(Issue(
                    severity="WARNING",
                    category="休日数",
                    day=None, employee=emp.name,
                    message=f"休日{actual_off}日（指定{expected}日、{actual_off - expected}日多い）",
                ))
        elif actual_off < required:
            result.issues.append(Issue(
                severity="ERROR",
                category="休日数",
                day=None, employee=emp.name,
                message=f"休日{actual_off}日（必要{required}日）",
            ))


def _check_consecutive_off(
    shift: MonthlyShift, result: ValidationResult, days: int,
    off_requests: dict[str, list[int]],
    employee_max_consecutive_off: Optional[dict[str, int]] = None,
) -> None:
    """2連休回数（1〜2回）と3連休の確認"""
    min_2off = HARD_CONSTRAINTS["min_two_day_off_per_month"]
    max_2off = HARD_CONSTRAINTS["max_two_day_off_per_month"]
    employee_max_consecutive_off = employee_max_consecutive_off or {}

    for emp in _validation_employees():
        if not emp.is_shift_eligible:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
            continue
        if emp.name in CONSTRAINT_EXCLUDED and emp.name not in employee_max_consecutive_off:
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
                    emp_max_off = employee_max_consecutive_off.get(emp.name)
                    if emp_max_off is not None and consec_off > int(emp_max_off):
                        result.issues.append(Issue(
                            severity="ERROR",
                            category="連休",
                            day=third_day, employee=emp.name,
                            message=f"{consec_off}連休（上限{int(emp_max_off)}）",
                        ))
                    elif not has_request:
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
                emp_max_off = employee_max_consecutive_off.get(emp.name)
                if emp_max_off is not None and consec_off > int(emp_max_off):
                    result.issues.append(Issue(
                        severity="ERROR",
                        category="連休",
                        day=days, employee=emp.name,
                        message=f"{consec_off}連休（上限{int(emp_max_off)}）",
                    ))
                elif not has_request:
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

        if emp.name in CONSTRAINT_EXCLUDED:
            continue

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


def _check_absolute_forbidden_assignments(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
    monthly_store_count_rules: list[dict],
) -> None:
    """従業員マスタで絶対配置不可になっている店舗への配置を検出する。"""
    absolute_allowed_stores = {
        "土井": {Store.HIGASHIGUCHI},
        "下地": {Store.OMIYA},
        "板倉": {Store.AKABANE},
        "野澤": {Store.SUZURAN},
        "南": {Store.AKABANE, Store.OMIYA, Store.SUZURAN},
    }
    makino_nishi_training_enabled = _monthly_rule_allows_employee_store(
        monthly_store_count_rules,
        "牧野",
        Store.NISHIGUCHI,
    )
    for day in range(1, days + 1):
        for assignment in shift.get_day_assignments(day):
            if assignment.store == Store.OFF:
                continue
            try:
                emp = get_employee(assignment.employee)
            except KeyError:
                continue

            allowed_stores = absolute_allowed_stores.get(emp.name)
            if allowed_stores is not None and assignment.store not in allowed_stores:
                result.issues.append(Issue(
                    severity="ERROR",
                    category="絶対配置不可",
                    day=day,
                    employee=emp.name,
                    message=f"{assignment.store.display_name}には配置できません",
                ))
                continue

            is_makino_nishi_exception = (
                emp.name == "牧野"
                and assignment.store == Store.NISHIGUCHI
                and makino_nishi_training_enabled
            )
            if is_makino_nishi_exception:
                continue

            if emp.affinities.get(assignment.store) == Affinity.NONE:
                result.issues.append(Issue(
                    severity="ERROR",
                    category="絶対配置不可",
                    day=day,
                    employee=emp.name,
                    message=f"{assignment.store.display_name}には配置できません",
                ))


def _check_forbidden_same_store_pairings(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
) -> None:
    """同じ店舗で一緒に勤務してはいけない組み合わせを検出する。"""
    for day in range(1, days + 1):
        day_assignments = shift.get_day_assignments(day)
        for store, anchor_name, blocked_names in FORBIDDEN_SAME_STORE_PAIRINGS:
            store_workers = {
                a.employee for a in day_assignments
                if a.store == store
            }
            if anchor_name not in store_workers:
                continue
            blocked_present = [
                name for name in blocked_names
                if name in store_workers
            ]
            if not blocked_present:
                continue
            result.issues.append(Issue(
                severity="ERROR",
                category="同勤務NG",
                day=day,
                employee=anchor_name,
                message=(
                    f"{store.display_name}で{anchor_name}さんと同時勤務NGのメンバーがいます"
                    f"／対象: {', '.join(blocked_present)}"
                ),
            ))
        for group in FORBIDDEN_SAME_STORE_GROUPS:
            group_members = set(group)
            for store in (s for s in Store if s != Store.OFF):
                store_group_workers = [
                    a.employee for a in day_assignments
                    if a.store == store and a.employee in group_members
                ]
                if len(store_group_workers) <= 1:
                    continue
                result.issues.append(Issue(
                    severity="ERROR",
                    category="同勤務NG",
                    day=day,
                    employee=None,
                    message=(
                        f"{store.display_name}で同日に同じ店舗NGのメンバーが重複しています"
                        f"／対象: {', '.join(store_group_workers)}"
                    ),
                ))


def _check_mandatory_work_on_request(
    shift: MonthlyShift,
    result: ValidationResult,
    work_requests: list,
    preferred_work_requests: list,
    preferred_work_groups: list,
    off_requests: dict[str, list[int]],
) -> None:
    """出勤希望日を必ず出勤扱いにする従業員の未充足を検出する。"""
    mandatory_names = set(MANDATORY_WORK_ON_REQUEST_EMPLOYEES)
    off_sets = {
        name: {int(day) for day in days}
        for name, days in (off_requests or {}).items()
    }
    single_day_requests: dict[str, set[int]] = {}
    for name, day, _store in list(work_requests or []) + list(preferred_work_requests or []):
        if name not in mandatory_names:
            continue
        try:
            day_int = int(day)
        except (TypeError, ValueError):
            continue
        if day_int in off_sets.get(name, set()):
            continue
        single_day_requests.setdefault(name, set()).add(day_int)

    for name, requested_days in single_day_requests.items():
        for day in sorted(requested_days):
            assignment = shift.get_assignment(name, day)
            if assignment is not None and assignment.store != Store.OFF:
                continue
            result.issues.append(Issue(
                severity="ERROR",
                category="出勤希望未充足",
                day=day,
                employee=name,
                message="出勤希望日なので必ず出勤にしてください",
            ))

    for name, candidate_days, required_count, _store in preferred_work_groups or []:
        if name not in mandatory_names:
            continue
        try:
            required = int(required_count)
        except (TypeError, ValueError):
            continue
        if required <= 0:
            continue
        safe_days = []
        for day in candidate_days or []:
            try:
                day_int = int(day)
            except (TypeError, ValueError):
                continue
            if day_int in off_sets.get(name, set()):
                continue
            safe_days.append(day_int)
        if not safe_days:
            continue
        worked_count = sum(
            1
            for day in set(safe_days)
            if (
                (assignment := shift.get_assignment(name, day)) is not None
                and assignment.store != Store.OFF
            )
        )
        if worked_count >= required:
            continue
        result.issues.append(Issue(
            severity="ERROR",
            category="出勤希望未充足",
            day=None,
            employee=name,
            message=(
                f"候補日のうち{required}日以上の出勤希望に対し、"
                f"実績{worked_count}日です"
            ),
        ))


def _check_work_requests(
    shift: MonthlyShift, result: ValidationResult,
    work_requests: list,
    off_requests: dict[str, list[int]],
) -> None:
    """出勤希望・希望店舗がどの程度反映されたかを確認する。"""
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
            if name in MANDATORY_WORK_ON_REQUEST_EMPLOYEES:
                continue
            result.issues.append(Issue(
                severity="INFO",
                category="出勤希望未反映",
                day=day, employee=name,
                message="出勤希望は希望扱いです。調整上、休みに配置されています",
            ))
        elif requested_store and a.store != requested_store:
            result.issues.append(Issue(
                severity="INFO",
                category="希望店舗不一致",
                day=day, employee=name,
                message=(
                    f"出勤時の希望店舗は{requested_store.display_name}、"
                    f"実配置は{a.store.display_name}"
                ),
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


def _check_month_end_start_omiya(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
    off_requests: dict[str, list[int]],
) -> None:
    """下地・春山は月初1日と月末最終日に大宮駅前へ配置する。"""
    for emp_name in MONTH_END_START_OMIYA_STAFF:
        for day in (1, days):
            if day in set(off_requests.get(emp_name, [])):
                continue
            assignment = shift.get_assignment(emp_name, day)
            actual_store = assignment.store if assignment is not None else Store.OFF
            if actual_store == Store.OMIYA:
                continue
            result.issues.append(Issue(
                severity="ERROR",
                category="月末月初大宮",
                day=day,
                employee=emp_name,
                message=(
                    f"{emp_name}は月初1日・月末最終日は大宮駅前店勤務です。"
                    f"現在は{actual_store.display_name}です。"
                ),
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


def _check_store_rotation_minimums(
    shift: MonthlyShift, result: ValidationResult,
) -> None:
    """月内の最低巡回条件が満たされているか。"""
    for emp_name, rules in STORE_ROTATION_MINIMUMS.items():
        for stores, min_count in rules:
            days = [
                a.day for a in shift.assignments
                if a.employee == emp_name and a.store in stores
            ]
            if len(days) >= min_count:
                continue
            store_label = "・".join(store.display_name for store in stores)
            result.issues.append(Issue(
                severity="ERROR",
                category="巡回配置",
                day=None,
                employee=emp_name,
                message=(
                    f"{store_label}への巡回が{len(days)}日です。"
                    f"最低{min_count}日必要です。"
                ),
            ))


def _check_balanced_normal_store_assignments(
    shift: MonthlyShift,
    result: ValidationResult,
) -> None:
    """主担当なし・通常対応可複数店舗の配置偏りを確認する。"""
    for emp in _validation_employees():
        if getattr(emp, "home_store", None) is not None:
            continue
        normal_stores = [
            store for store, affinity in (getattr(emp, "affinities", {}) or {}).items()
            if affinity == Affinity.MEDIUM
        ]
        if len(normal_stores) < 2:
            continue
        counts = {
            store: sum(
                1
                for assignment in shift.assignments
                if assignment.employee == emp.name and assignment.store == store
            )
            for store in normal_stores
        }
        worked_total = sum(counts.values())
        if worked_total < 6:
            continue
        max_count = max(counts.values())
        min_count = min(counts.values())
        if max_count - min_count < 4:
            continue
        count_text = " / ".join(
            f"{store.display_name}:{count}日" for store, count in counts.items()
        )
        result.issues.append(Issue(
            severity="WARNING",
            category="店舗バランス",
            day=None,
            employee=emp.name,
            message=(
                "主担当なし・通常対応可複数店舗の配置に偏りがあります。"
                f"内訳: {count_text}"
            ),
        ))


def _employee_names_at_store(shift: MonthlyShift, day: int, store: Store) -> list[str]:
    """指定日の指定店舗に入っている従業員名を返す。"""
    return [
        a.employee for a in shift.get_day_assignments(day)
        if a.store == store
    ]


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


def _monthly_rule_allows_employee_store(
    rules: list[dict],
    employee: str,
    store: Store,
) -> bool:
    """月別ルールで特定スタッフの特定店舗勤務が明示されているか。"""
    for rule in rules or []:
        if str(rule.get("employee") or "") != employee:
            continue
        comparison = str(rule.get("comparison") or "min").lower()
        if comparison == "forbid":
            continue
        try:
            count = int(rule.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        stores = [
            s for s in (_store_from_rule_value(v) for v in (rule.get("stores") or []))
            if s is not None and s != Store.OFF
        ]
        if store in stores:
            return True
    return False


def _check_makino_training_rules(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
    monthly_store_count_rules: list[dict],
) -> None:
    """牧野さんの赤羽東口・大宮西口研修ルールを検証する。"""
    nishi_training_enabled = _monthly_rule_allows_employee_store(
        monthly_store_count_rules,
        "牧野",
        Store.NISHIGUCHI,
    )
    for day in range(1, days + 1):
        higashi_workers = _employee_names_at_store(shift, day, Store.HIGASHIGUCHI)
        if "牧野" in higashi_workers:
            result.issues.append(Issue(
                severity="ERROR",
                category="牧野研修ルール",
                day=day,
                employee="牧野",
                message="牧野さんは赤羽東口店の単独勤務・配置は当面NGです。",
            ))

        nishi_workers = _employee_names_at_store(shift, day, Store.NISHIGUCHI)
        if "牧野" not in nishi_workers:
            continue
        if not nishi_training_enabled:
            continue
        elif MAKINO_NISHIGUCHI_TRAINING_PARTNER not in nishi_workers:
            result.issues.append(Issue(
                severity="WARNING",
                category="牧野研修ルール",
                day=day,
                employee="牧野",
                message=(
                    "牧野さんの大宮西口店勤務は楯君との研修時を優先します。"
                    f"楯君と同時配置してください／配属: {', '.join(nishi_workers)}"
                ),
            ))


def _check_store_keyholders(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
) -> None:
    """各店舗に鍵担当がいるか確認する。"""
    for day in range(1, days + 1):
        mode = shift.operation_modes.get(day, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        capacity_map = get_capacity(mode)
        weekday = date(shift.year, shift.month, day).weekday()
        for store, keyholders in STORE_KEYHOLDERS.items():
            cap = capacity_map.get(store)
            if cap is not None and weekday in cap.closed_dow:
                continue
            workers = _employee_names_at_store(shift, day, store)
            if not workers:
                continue
            if any(name in keyholders for name in workers):
                continue
            if store == Store.SUZURAN:
                omiya_workers = _employee_names_at_store(shift, day, Store.OMIYA)
                supporters = [
                    name for name in omiya_workers
                    if name in SUZURAN_KEY_SUPPORT_FROM_OMIYA
                ]
                if supporters:
                    result.issues.append(Issue(
                        severity="INFO",
                        category="鍵応援",
                        day=day,
                        employee=None,
                        message=(
                            "大宮すずらん通り店に鍵担当がいません。"
                            f"大宮駅前店の{', '.join(supporters)}が開け締め応援候補です"
                            f"／すずらん配属: {', '.join(workers)}"
                        ),
                    ))
                    continue
            result.issues.append(Issue(
                severity="WARNING",
                category="鍵不足",
                day=day,
                employee=None,
                message=(
                    f"{store.display_name}に鍵担当がいません"
                    f"／配属: {', '.join(workers)}"
                    f"／鍵担当: {', '.join(keyholders)}"
                ),
            ))


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
        comparison = str(rule.get("comparison") or "min").lower()
        try:
            required_count = int(rule.get("count") or 0)
        except (TypeError, ValueError):
            required_count = 0
        if comparison == "forbid":
            required_count = 0
        if comparison != "forbid" and required_count <= 0:
            continue
        actual_count = sum(
            1 for a in shift.assignments
            if a.employee == emp_name and a.store in stores
        )
        if comparison in ("max", "forbid"):
            ok = actual_count <= required_count
            condition_text = "配置禁止" if comparison == "forbid" else f"{required_count}日以下"
        elif comparison == "exact":
            ok = actual_count == required_count
            condition_text = f"{required_count}日ちょうど"
        else:
            ok = actual_count >= required_count
            condition_text = f"{required_count}日以上"
        if ok:
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
                f"条件は{condition_text}です。"
            ),
        ))


def _check_required_assignment_rules(
    shift: MonthlyShift,
    result: ValidationResult,
    rules: list[dict],
) -> None:
    """月別追加ルール（日付指定で特定店舗へ配置）を検証する。"""
    days_in_month = monthrange(shift.year, shift.month)[1]
    for rule in rules:
        if not rule or not rule.get("employee"):
            continue
        emp_name = str(rule.get("employee"))
        try:
            day = int(rule.get("day") or rule.get("target_day") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= day <= days_in_month):
            continue
        raw_store = rule.get("store")
        if raw_store is None:
            stores = rule.get("stores") or []
            raw_store = stores[0] if stores else None
        store = _store_from_rule_value(raw_store)
        if store is None or store == Store.OFF:
            continue

        assignment = shift.get_assignment(emp_name, day)
        actual_store = assignment.store if assignment is not None else Store.OFF
        if actual_store == store:
            continue

        severity = str(rule.get("severity") or "ERROR").upper()
        result.issues.append(Issue(
            severity="ERROR" if severity == "ERROR" else "WARNING",
            category="月別ルール",
            day=day,
            employee=emp_name,
            message=(
                f"{rule.get('name') or '月別日付指定配置'}: "
                f"{store.display_name}への配置指定です。"
                f"現在は{actual_store.display_name}です。"
            ),
        ))


def _check_monthly_workday_balance(
    shift: MonthlyShift,
    result: ValidationResult,
    days: int,
    off_requests: dict[str, list[int]],
    holiday_overrides: dict[str, int],
    exact_holiday_days: dict[str, int],
) -> None:
    """月間基準勤務日数から大きく外れていないか確認する。"""
    shortfall_warning_diff = int(WORK_TARGET_SHORTFALL_WARNING_DIFF_DAYS)
    overage_warning_diff = int(WORK_TARGET_OVERAGE_WARNING_DIFF_DAYS)
    error_diff = int(WORK_TARGET_ERROR_DIFF_DAYS)
    for emp in _validation_employees():
        if emp.is_auxiliary or emp.annual_target_days is None:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
            continue
        base_target = get_monthly_work_target(
            emp.name,
            shift.month,
            emp.annual_target_days,
        )
        if base_target is None:
            continue
        target = int(base_target)

        actual = sum(
            1
            for day in range(1, days + 1)
            if (
                (assignment := shift.get_assignment(emp.name, day)) is not None
                and assignment.store != Store.OFF
            )
        )
        diff = actual - target
        if diff <= -shortfall_warning_diff:
            severity = "ERROR" if abs(diff) >= error_diff else "WARNING"
            note = ""
            if emp.name in exact_holiday_days:
                note = f" 指定休日数は{int(exact_holiday_days[emp.name])}日です。"
            elif emp.name in holiday_overrides:
                note = f" 目標休日数は{int(holiday_overrides[emp.name])}日です。"
            result.issues.append(Issue(
                severity=severity,
                category="月間勤務日数不足",
                day=None,
                employee=emp.name,
                message=(
                    f"出勤{actual}日 / 基準{target}日（{abs(diff)}日不足）。"
                    "月別例外がなければ、出勤日数を増やしてください。"
                    f"{note}"
                ),
            ))
        elif diff >= overage_warning_diff:
            severity = "ERROR" if diff >= error_diff else "WARNING"
            note = ""
            if emp.name in exact_holiday_days:
                note = f" 指定休日数は{int(exact_holiday_days[emp.name])}日です。"
            elif emp.name in holiday_overrides:
                note = f" 目標休日数は{int(holiday_overrides[emp.name])}日です。"
            result.issues.append(Issue(
                severity=severity,
                category="月間勤務日数超過",
                day=None,
                employee=emp.name,
                message=(
                    f"出勤{actual}日 / 基準{target}日（{diff}日超過）。"
                    "人数が余る月でなければ、休みに寄せてください。"
                    f"{note}"
                ),
            ))


def _compute_stats(shift: MonthlyShift, result: ValidationResult, days: int) -> None:
    """
    統計情報の集計（経営側可視化用）

    月間目標出勤日数の大幅なズレは別チェックでWARNINGにし、
    ここでは経営判断材料として「目標 vs 実績」の数字を明示する。
    """
    def get_monthly_target(emp) -> Optional[int]:
        return get_monthly_work_target(
            emp.name,
            shift.month,
            emp.annual_target_days,
        )

    # 各従業員の出勤日数・休日日数・目標達成度
    for emp in _validation_employees():
        if emp.is_auxiliary:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
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
            if a.store != Store.OFF
            and not get_employee(a.employee).is_auxiliary
            and not is_probationary_employee(
                get_employee(a.employee), shift.year, shift.month, day,
            )
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
    for emp in _validation_employees():
        if emp.is_auxiliary or emp.annual_target_days is None:
            continue
        if is_probationary_employee(emp, shift.year, shift.month):
            continue
        target = get_monthly_work_target(emp.name, shift.month, emp.annual_target_days)
        if target is None:
            continue
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
