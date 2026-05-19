"""
ルール整合性チェック。
================================================

シフト生成前に、設定同士の矛盾・齟齬・運用上の注意点を洗い出す。
実際のシフト案を検証する validator.py とは別に、ルール定義そのものを確認する。
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from typing import Optional

from .employee_config import get_all_employees_including_retired
from .models import Affinity, Employee, EmploymentStatus, OperationMode, Store
from .rule_config import RuleConfig, RuleConfigManager
from .rules import (
    CONSTRAINT_EXCLUDED,
    FORBIDDEN_SAME_STORE_GROUPS,
    HIGASHIGUCHI_ALLOWED_STAFF,
    MANDATORY_WORK_ON_REQUEST_EMPLOYEES,
    MONTHLY_WORK_TARGETS,
    NORMAL_CAPACITY,
    REDUCED_CAPACITY,
    MINIMUM_CAPACITY,
    OMIYA_ANCHOR_STAFF,
    STORE_ROTATION_MINIMUMS,
    STORE_STAFFING_LIMITS,
    get_monthly_required_holiday_days,
    get_monthly_work_target,
)
from .shift_lock import ShiftLockManager
from .carryover import previous_year_month


@dataclass(frozen=True)
class ConsistencyIssue:
    """ルール整合性チェックで見つかった問題・注意。"""

    severity: str
    category: str
    target: str
    message: str
    detail: str = ""

    def to_row(self) -> dict:
        return {
            "重要度": self.severity,
            "分類": self.category,
            "対象": self.target,
            "内容": self.message,
            "補足": self.detail,
        }


@dataclass(frozen=True)
class ConsistencyReport:
    """整合性チェック結果。"""

    year: Optional[int]
    month: Optional[int]
    issues: list[ConsistencyIssue]

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "WARNING")

    @property
    def info_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "INFO")

    def rows(self, include_info: bool = True) -> list[dict]:
        return [
            issue.to_row()
            for issue in self.issues
            if include_info or issue.severity != "INFO"
        ]


def _issue(
    issues: list[ConsistencyIssue],
    severity: str,
    category: str,
    target: str,
    message: str,
    detail: str = "",
) -> None:
    issues.append(ConsistencyIssue(severity, category, target, message, detail))


def _employee_map() -> dict[str, Employee]:
    return {emp.name: emp for emp in get_all_employees_including_retired()}


def _active_employees(employees: dict[str, Employee]) -> list[Employee]:
    return [
        emp for emp in employees.values()
        if emp.employment_status in (EmploymentStatus.ACTIVE, EmploymentStatus.PART_TIME)
        and not emp.is_auxiliary
    ]


def _store_from_value(value) -> Optional[Store]:
    if isinstance(value, Store):
        return value
    if value is None:
        return None
    text = str(value)
    try:
        return Store[text]
    except KeyError:
        pass
    for store in Store:
        if text in (store.value, store.display_name):
            return store
    return None


def _monthly_rule_applies(rule, year: int, month: int) -> bool:
    if not getattr(rule, "enabled", True):
        return False
    try:
        target_year = getattr(rule, "target_year", None)
        target_month = getattr(rule, "target_month", None)
        if target_year is not None and int(target_year) != int(year):
            return False
        if target_month is not None and int(target_month) != int(month):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _check_parameters(
    issues: list[ConsistencyIssue],
    cfg: RuleConfig,
    year: Optional[int],
    month: Optional[int],
) -> None:
    params = cfg.parameters
    max_consec = int(params.get("max_consec_work", 5))
    soft_consec = int(params.get("soft_consec_threshold", 4))
    min_2off = int(params.get("min_2off_per_month", 1))
    max_2off = int(params.get("max_2off_per_month", 2))
    default_holidays = int(params.get("default_holiday_days", 8))
    solver_time = int(params.get("solver_time_limit_seconds", 180))

    if soft_consec > max_consec:
        _issue(
            issues, "ERROR", "数値パラメータ", "連勤",
            "推奨連勤上限が最大連勤日数を超えています。",
            f"推奨{soft_consec} / 最大{max_consec}",
        )
    if min_2off > max_2off:
        _issue(
            issues, "ERROR", "数値パラメータ", "2連休",
            "2連休の最低回数が最大回数を超えています。",
            f"最低{min_2off} / 最大{max_2off}",
        )
    if default_holidays < 0:
        _issue(
            issues, "ERROR", "数値パラメータ", "既定休日数",
            "既定休日数がマイナスになっています。",
        )
    if year and month:
        days = monthrange(int(year), int(month))[1]
        if default_holidays > days:
            _issue(
                issues, "ERROR", "数値パラメータ", "既定休日数",
                "既定休日数が月の日数を超えています。",
                f"{int(month)}月は{days}日、既定休日数は{default_holidays}日",
            )
    if solver_time < 60:
        _issue(
            issues, "WARNING", "数値パラメータ", "ソルバー実行時間",
            "ソルバー実行時間が短く、解なしになりやすい設定です。",
            f"現在{solver_time}秒",
        )


def _check_store_capacity(issues: list[ConsistencyIssue]) -> None:
    capacity_sets = {
        "通常営業": NORMAL_CAPACITY,
        "省人員": REDUCED_CAPACITY,
        "最小営業": MINIMUM_CAPACITY,
    }
    for mode_name, capacities in capacity_sets.items():
        for store, cap in capacities.items():
            limit = STORE_STAFFING_LIMITS.get(store)
            if limit is None:
                _issue(
                    issues, "ERROR", "店舗人数", store.display_name,
                    "店舗人数上限が設定されていません。",
                    mode_name,
                )
                continue
            required_total = int(cap.eco_min) + int(cap.ticket_min)
            if required_total > int(limit.max_total):
                _issue(
                    issues, "ERROR", "店舗人数", store.display_name,
                    "必要人数が店舗最大人数を超えています。",
                    f"{mode_name}: 必要{required_total}名 / 最大{limit.max_total}名",
                )
            if int(limit.standard_total) > int(limit.max_total):
                _issue(
                    issues, "ERROR", "店舗人数", store.display_name,
                    "標準人数が最大人数を超えています。",
                    f"標準{limit.standard_total}名 / 最大{limit.max_total}名",
                )
            if int(cap.eco_min) > int(cap.eco_max):
                _issue(
                    issues, "ERROR", "店舗人数", store.display_name,
                    "エコ最小人数がエコ最大人数を超えています。",
                    f"{mode_name}: 最小{cap.eco_min}名 / 最大{cap.eco_max}名",
                )


def _check_employee_master(
    issues: list[ConsistencyIssue],
    employees: dict[str, Employee],
) -> None:
    active = _active_employees(employees)
    store_set = {Store.AKABANE, Store.HIGASHIGUCHI, Store.OMIYA, Store.NISHIGUCHI, Store.SUZURAN}

    for emp in active:
        if emp.home_store is not None and emp.affinities.get(emp.home_store) == Affinity.NONE:
            _issue(
                issues, "ERROR", "従業員マスタ", emp.name,
                "ホーム店舗が絶対配置不可になっています。",
                emp.home_store.display_name,
            )
        known_stores = set(emp.affinities)
        missing = sorted(store_set - known_stores, key=lambda s: s.name)
        if missing:
            _issue(
                issues, "WARNING", "従業員マスタ", emp.name,
                "店舗適性が未設定の店舗があります。",
                "、".join(store.display_name for store in missing),
            )
        if getattr(emp, "only_on_request_days", False) and emp.name not in MANDATORY_WORK_ON_REQUEST_EMPLOYEES:
            _issue(
                issues, "WARNING", "従業員マスタ", emp.name,
                "出勤希望日のみ稼働の設定ですが、必須出勤リストに入っていません。",
            )

    for name in HIGASHIGUCHI_ALLOWED_STAFF:
        emp = employees.get(name)
        if emp is None:
            _issue(
                issues, "ERROR", "赤羽東口", name,
                "赤羽東口の配置可能者が従業員マスタに見つかりません。",
            )
            continue
        if emp.affinities.get(Store.HIGASHIGUCHI) == Affinity.NONE:
            _issue(
                issues, "ERROR", "赤羽東口", name,
                "赤羽東口の配置可能者なのに、従業員マスタでは東口が絶対配置不可です。",
            )

    for emp in active:
        if (
            emp.affinities.get(Store.HIGASHIGUCHI) != Affinity.NONE
            and emp.name not in HIGASHIGUCHI_ALLOWED_STAFF
        ):
            _issue(
                issues, "ERROR", "赤羽東口", emp.name,
                "赤羽東口に配置可能な適性ですが、東口配置可能者リストに入っていません。",
            )

    for name in OMIYA_ANCHOR_STAFF:
        emp = employees.get(name)
        if emp is None:
            _issue(
                issues, "ERROR", "大宮駅前", name,
                "大宮アンカー要員が従業員マスタに見つかりません。",
            )
            continue
        if emp.affinities.get(Store.OMIYA) == Affinity.NONE:
            _issue(
                issues, "ERROR", "大宮駅前", name,
                "大宮アンカー要員なのに、大宮が絶対配置不可です。",
            )

    for group in FORBIDDEN_SAME_STORE_GROUPS:
        missing = [name for name in group if name not in employees]
        if missing:
            _issue(
                issues, "ERROR", "同店舗NG", "、".join(group),
                "同店舗NGグループに従業員マスタ未登録の名前があります。",
                "、".join(missing),
            )

    for emp_name, rules in STORE_ROTATION_MINIMUMS.items():
        emp = employees.get(emp_name)
        if emp is None:
            _issue(
                issues, "ERROR", "月内最低巡回", emp_name,
                "巡回条件の対象者が従業員マスタに見つかりません。",
            )
            continue
        for stores, count in rules:
            for store in stores:
                if emp.affinities.get(store) == Affinity.NONE:
                    _issue(
                        issues, "ERROR", "月内最低巡回", emp_name,
                        "巡回必須店舗が従業員マスタでは絶対配置不可です。",
                        f"{store.display_name} {count}回以上",
                    )


def _check_monthly_targets(
    issues: list[ConsistencyIssue],
    employees: dict[str, Employee],
    cfg: RuleConfig,
    year: Optional[int],
    month: Optional[int],
) -> None:
    if not month:
        return
    days = monthrange(int(year or 2026), int(month))[1]
    default_holidays = int(cfg.parameters.get("default_holiday_days", 8))
    active_names = {emp.name for emp in _active_employees(employees)}
    for emp in _active_employees(employees):
        if emp.annual_target_days is None:
            continue
        target = get_monthly_work_target(emp.name, int(month), emp.annual_target_days)
        if target is None:
            _issue(
                issues, "WARNING", "月別基準勤務日数", emp.name,
                "月別基準勤務日数が見つからず、年間日数からの概算になります。",
            )
            continue
        if target < 0 or target > days:
            _issue(
                issues, "ERROR", "月別基準勤務日数", emp.name,
                "月別基準勤務日数が月の日数の範囲外です。",
                f"{int(month)}月は{days}日、基準は{target}日",
            )
            continue
        required_holidays = get_monthly_required_holiday_days(
            emp.name,
            int(month),
            days,
            emp.annual_target_days,
            default_holidays,
        )
        if required_holidays != default_holidays:
            _issue(
                issues, "INFO", "休日数", emp.name,
                "月別基準勤務日数から休日数を逆算します。",
                f"{int(month)}月: 出勤{target}日 / 休日{required_holidays}日。既定休日{default_holidays}日は予備扱い。",
            )

    for target_name in MONTHLY_WORK_TARGETS:
        if target_name not in active_names and target_name not in CONSTRAINT_EXCLUDED:
            _issue(
                issues, "INFO", "月別基準勤務日数", target_name,
                "月別勤務日数表にはありますが、現在のシフト稼働対象ではありません。",
            )


def _check_custom_rules(
    issues: list[ConsistencyIssue],
    employees: dict[str, Employee],
    cfg: RuleConfig,
    year: Optional[int],
    month: Optional[int],
) -> None:
    if not year or not month:
        return
    days = monthrange(int(year), int(month))[1]
    valid_comparisons = {"min", "max", "exact", "forbid"}
    valid_severities = {"ERROR", "WARNING"}
    for rule in getattr(cfg, "custom_rules", []):
        if not _monthly_rule_applies(rule, int(year), int(month)):
            continue
        if getattr(rule, "rule_type", "note") != "employee_store_count":
            continue
        name = getattr(rule, "name", "") or getattr(rule, "id", "")
        emp_name = str(getattr(rule, "employee", "") or "")
        emp = employees.get(emp_name)
        if emp is None:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの対象者が従業員マスタに見つかりません。",
                emp_name,
            )
            continue
        comparison = str(getattr(rule, "comparison", "min") or "min").lower()
        if comparison not in valid_comparisons:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの比較条件が不正です。",
                comparison,
            )
        severity = str(getattr(rule, "severity", "WARNING") or "WARNING").upper()
        if severity not in valid_severities:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの重要度が不正です。",
                severity,
            )
        try:
            count = int(getattr(rule, "count", 0) or 0)
        except (TypeError, ValueError):
            count = -1
        if comparison != "forbid" and count <= 0:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの回数が0以下です。",
            )
        if count > days:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの回数が月の日数を超えています。",
                f"{int(month)}月は{days}日、指定は{count}回",
            )
        stores = [_store_from_value(v) for v in getattr(rule, "stores", []) or []]
        stores = [store for store in stores if store is not None and store != Store.OFF]
        if not stores:
            _issue(
                issues, "ERROR", "月別ルール", name,
                "月別ルールの対象店舗が空、または不正です。",
            )
            continue
        for store in stores:
            is_makino_nishi_training = emp_name == "牧野" and store == Store.NISHIGUCHI
            if (
                comparison != "forbid"
                and emp.affinities.get(store) == Affinity.NONE
                and not is_makino_nishi_training
            ):
                _issue(
                    issues, "ERROR", "月別ルール", name,
                    "月別ルールで指定した店舗が、従業員マスタでは絶対配置不可です。",
                    f"{emp_name} / {store.display_name}",
                )
            if is_makino_nishi_training and comparison != "forbid":
                _issue(
                    issues, "INFO", "月別ルール", name,
                    "牧野さんの西口研修例外として扱われます。",
                    "生成時は楯さんと同日同店舗になる条件も確認します。",
                )


def _check_previous_month_lock(
    issues: list[ConsistencyIssue],
    year: Optional[int],
    month: Optional[int],
) -> None:
    if not year or not month:
        return
    previous_year, previous_month = previous_year_month(int(year), int(month))
    lock_mgr = ShiftLockManager()
    if lock_mgr.get_lock_info(previous_year, previous_month) is None:
        _issue(
            issues, "WARNING", "前月連勤持ち越し",
            f"{int(year)}年{int(month)}月",
            "前月がロックされていないため、前月末から月初の連勤持ち越しは未反映になります。",
            f"前月: {previous_year}年{previous_month}月",
        )


def run_rule_consistency_checks(
    year: Optional[int] = None,
    month: Optional[int] = None,
    rule_cfg: Optional[RuleConfig] = None,
    include_operational_checks: bool = True,
) -> ConsistencyReport:
    """現在のルール・従業員マスタ・対象月の整合性を確認する。"""
    cfg = rule_cfg or RuleConfigManager().load()
    issues: list[ConsistencyIssue] = []
    employees = _employee_map()

    _check_parameters(issues, cfg, year, month)
    _check_store_capacity(issues)
    _check_employee_master(issues, employees)
    _check_monthly_targets(issues, employees, cfg, year, month)
    _check_custom_rules(issues, employees, cfg, year, month)
    if include_operational_checks:
        _check_previous_month_lock(issues, year, month)

    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    issues.sort(key=lambda issue: (
        severity_order.get(issue.severity, 9),
        issue.category,
        issue.target,
        issue.message,
    ))
    return ConsistencyReport(year=year, month=month, issues=issues)


if __name__ == "__main__":
    report = run_rule_consistency_checks(2026, 6)
    print(f"ERROR={report.error_count} WARNING={report.warning_count} INFO={report.info_count}")
    for row in report.rows(include_info=False):
        print(row)
