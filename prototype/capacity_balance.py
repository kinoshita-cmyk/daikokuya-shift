"""
月間需給バランスの概算（前任者の手計算のシステム化）
================================================
シフトを生成する前に「そもそも人が足りるのか」を大づかみに検算する。
前任者が毎月手作業で行っていた3つの検算を、実データから自動計算する:

1. 全店舗の必要延べ人区 vs 出勤可能延べ人区（余裕の把握）
2. エコの必要延べ人区 vs エコ人員の延べ人区
   → 大宮駅前を「エコ2人」にできる日数の上限
3. 東口・西口の必要延べ人区 vs 専任者（土井・楯）の延べ人区
   → 代替要員（春山・長尾・今津）で補う必要のある人区

※あくまで概算（目標出勤日数ベース）。営業モード（省人員・休業）と
  店舗定休日は考慮するが、個別の×休みの並びや連勤制約は考慮しない。
  正確な成立可否はシフト生成そのものが判定する。
"""

from __future__ import annotations
from calendar import monthrange
from datetime import date
from typing import Optional

from .models import OperationMode, Skill, Store
from .rules import (
    MANDATORY_WORK_ON_REQUEST_EMPLOYEES,
    STORE_STAFFING_LIMITS,
    get_capacity,
    get_monthly_work_target,
)

# 出勤希望のみ勤務の人が未提出のときの目安日数（generator と同じ値）
ONLY_ON_REQUEST_TARGET_DAYS = 8

# 東口・西口の専任者（この2人で埋まらない分を代替要員が補う）
EAST_WEST_DEDICATED = ("土井", "楯")
EAST_WEST_SUBSTITUTES = ("春山", "長尾", "今津")

# 補助要員（山本さん）の月間供給人区は rules.yamamoto_monthly_max_days に従う
# （1・2月=14、3〜12月=15）。総供給にのみ計上し、エコ・東西口には含めない。
from .rules import yamamoto_monthly_max_days


def _auxiliary_monthly_supply(name: str, month: int) -> int:
    if str(name) == "山本":
        return int(yamamoto_monthly_max_days(month))
    return 0


def compute_monthly_capacity_balance(
    year: int,
    month: int,
    employees: list,
    operation_modes: dict,
    submitted_paid_leave: Optional[dict] = None,
    admin_paid_leave: Optional[dict] = None,
    work_request_counts: Optional[dict] = None,
    requested_holiday_days: Optional[dict] = None,
) -> dict:
    """月間の需給バランスを概算する。

    Args:
        employees: 対象従業員オブジェクトのリスト（補助要員・顧問を含んでよい。
                   is_auxiliary / role / skill / annual_target_days を参照する）
        operation_modes: {日: OperationMode}
        submitted_paid_leave: {氏名: 提出された有給日数}
        admin_paid_leave: {氏名: 管理者調整の有給日数}
        work_request_counts: {氏名: 出勤希望日数}（出勤希望日のみ勤務の人用）
    """
    submitted_paid_leave = submitted_paid_leave or {}
    admin_paid_leave = admin_paid_leave or {}
    work_request_counts = work_request_counts or {}
    requested_holiday_days = requested_holiday_days or {}
    days_in_month = monthrange(int(year), int(month))[1]

    # ---- 需要side ------------------------------------------------
    # 前任者の計算と突き合わせられるよう、需要は「全日を標準人数で
    # 回す場合」を主表示にする（例: 8月 31日×11人−東口休5日=336）。
    # 省人員モード適用時の軽減分は mode_reduction として別に返す。
    demand_total = 0          # 標準人数ベースの必要延べ人区（前任者方式）
    mode_reduction = 0        # 省人員・休業モード適用時に減る人区
    demand_eco_baseline = 0   # エコ最低ライン（大宮を1人として数える）
    east_west_demand = 0      # 東口＋西口の必要延べ人区
    omiya_normal_open_days = 0  # 大宮を2人体制に格上げできる候補日数
    higashi_closed_days = 0
    reduced_days = 0
    closed_days = 0

    for d in range(1, days_in_month + 1):
        mode = operation_modes.get(d, OperationMode.NORMAL)
        if mode == OperationMode.REDUCED:
            reduced_days += 1
        elif mode == OperationMode.CLOSED:
            closed_days += 1
        mode_capacity = get_capacity(mode)
        weekday = date(int(year), int(month), d).weekday()
        for store, std in STORE_STAFFING_LIMITS.items():
            std_cap = get_capacity(OperationMode.NORMAL).get(store)
            if std_cap is None:
                continue
            if weekday in std_cap.closed_dow:
                if store == Store.HIGASHIGUCHI:
                    higashi_closed_days += 1
                continue
            # 標準需要（前任者方式: モードに関わらず標準人数で数える）
            standard_need = int(std.standard_total)
            demand_total += standard_need
            # モード適用時の実需要との差分
            mode_cap = mode_capacity.get(store)
            if mode == OperationMode.CLOSED or mode_cap is None:
                actual_need = 0
            elif mode == OperationMode.NORMAL:
                actual_need = standard_need
            else:
                actual_need = int(mode_cap.eco_min) + int(mode_cap.ticket_min)
            mode_reduction += max(0, standard_need - actual_need)
            # エコ最低ライン（エコが必要な店に1人ずつ。大宮も1人と数える）
            if int(std_cap.eco_min) >= 1:
                demand_eco_baseline += 1
            # 東口・西口
            if store in (Store.HIGASHIGUCHI, Store.NISHIGUCHI):
                east_west_demand += 1
            # 大宮2人体制の格上げ候補日（通常モードで営業している日）
            if store == Store.OMIYA and mode == OperationMode.NORMAL:
                omiya_normal_open_days += 1

    # ---- 供給side（従業員ごとの目標出勤日数 − 有給） ------------
    supply_rows = []
    supply_total = 0
    eco_supply = 0
    east_west_dedicated_supply = 0

    excluded_names = []
    for e in employees:
        name = str(getattr(e, "name", ""))
        if not name:
            continue
        role = getattr(e, "role", None)
        role_text = str(getattr(role, "value", role) or "")
        if role_text in ("顧問", "ADVISOR"):
            continue  # 顧問は自動配置しない方針のため供給に数えない
        if getattr(e, "is_auxiliary", False):
            # 補助要員（山本さん）: 前任者の計算方式に合わせて
            # 固定の月間供給（半月=15人区）で総供給にのみ計上する
            aux_base = _auxiliary_monthly_supply(name, int(month))
            if aux_base <= 0:
                excluded_names.append(f"{name}（補助要員・供給目安なし）")
                continue
            aux_paid = int(submitted_paid_leave.get(name, 0) or 0) + int(
                admin_paid_leave.get(name, 0) or 0
            )
            aux_supply = max(0, aux_base - aux_paid)
            supply_total += aux_supply
            formula = f"月上限{aux_base}人区（補助要員の固定計算）"
            if aux_paid:
                formula += f" − 有給{aux_paid}日"
            supply_rows.append({
                "氏名": name,
                "供給人区": aux_supply,
                "計算式": formula + "／エコ・東西口には含めない",
                "エコ": "",
            })
            continue

        paid = int(submitted_paid_leave.get(name, 0) or 0) + int(
            admin_paid_leave.get(name, 0) or 0
        )
        if name in MANDATORY_WORK_ON_REQUEST_EMPLOYEES:
            base = int(work_request_counts.get(name, 0) or 0)
            if base <= 0:
                base = int(ONLY_ON_REQUEST_TARGET_DAYS)
                formula = f"出勤希望のみ勤務（未提出のため目安 {base}日）"
            else:
                formula = f"出勤希望 {base}日（出勤希望のみ勤務）"
            supply = max(0, base - paid)
            if paid:
                formula += f" − 有給{paid}日"
        elif name in requested_holiday_days:
            # 自由記載で「休み計◯日」を指定した人（勤務日数が確定している）
            req_off = int(requested_holiday_days.get(name, 0) or 0)
            supply = max(0, days_in_month - req_off)
            formula = f"{days_in_month}日 − 休み計{req_off}日（自由記載の指定）"
            if paid:
                formula += f"※有給{paid}日は休み計に含む"
        else:
            target = get_monthly_work_target(
                name, int(month), getattr(e, "annual_target_days", None),
            )
            if target is None:
                excluded_names.append(f"{name}（目標日数が未定義）")
                continue
            base = int(target)
            supply = max(0, base - paid)
            formula = f"基準 {base}日"
            if paid:
                formula += f" − 有給{paid}日"
        supply_total += supply
        is_eco = getattr(e, "skill", None) == Skill.ECO
        if is_eco:
            eco_supply += supply
        if name in EAST_WEST_DEDICATED:
            east_west_dedicated_supply += supply
        supply_rows.append({
            "氏名": name,
            "供給人区": supply,
            "計算式": formula,
            "エコ": "○" if is_eco else "",
        })

    # ---- 判定 ---------------------------------------------------
    slack_total = supply_total - demand_total
    eco_surplus = eco_supply - demand_eco_baseline
    omiya_two_eco_days = max(0, min(int(eco_surplus), int(omiya_normal_open_days)))
    east_west_gap = east_west_demand - east_west_dedicated_supply

    return {
        "year": int(year),
        "month": int(month),
        "days_in_month": days_in_month,
        "closed_days": closed_days,
        "reduced_days": reduced_days,
        "higashi_closed_days": higashi_closed_days,
        # 1. 全体需給
        "demand_total": demand_total,
        "mode_reduction": mode_reduction,
        "supply_total": supply_total,
        "slack_total": slack_total,
        # 2. エコ需給
        "demand_eco_baseline": demand_eco_baseline,
        "eco_supply": eco_supply,
        "eco_surplus": eco_surplus,
        "omiya_normal_open_days": omiya_normal_open_days,
        "omiya_two_eco_days": omiya_two_eco_days,
        # 3. 東西口
        "east_west_demand": east_west_demand,
        "east_west_dedicated_supply": east_west_dedicated_supply,
        "east_west_gap": east_west_gap,
        "east_west_dedicated": list(EAST_WEST_DEDICATED),
        "east_west_substitutes": list(EAST_WEST_SUBSTITUTES),
        # 明細
        "supply_rows": supply_rows,
        "excluded_names": excluded_names,
    }


def balance_summary_lines(result: dict) -> list:
    """画面表示用の日本語サマリー行（3本柱＋判定）を返す。"""
    lines = []

    slack = int(result["slack_total"])
    if slack < 0:
        verdict1 = f"⚠️ **{-slack}人区の不足**（標準人数を全日維持するのは困難）"
    elif slack <= 3:
        verdict1 = f"🟡 余裕 **{slack}人区**（ギリギリ。休み希望の重なりに注意）"
    else:
        verdict1 = f"🟢 余裕 **{slack}人区**"
    line1 = (
        f"**1. 全体の需給**: 必要 {result['demand_total']}人区"
        "（全日を標準人数で計算） ／ "
        f"供給 {result['supply_total']}人区 → {verdict1}"
    )
    reduction = int(result.get("mode_reduction", 0) or 0)
    if reduction > 0:
        line1 += (
            f"　※省人員モード適用日はさらに{reduction}人区軽くなります"
            f"（実質必要 {int(result['demand_total']) - reduction}人区）"
        )
    lines.append(line1)

    lines.append(
        f"**2. エコの需給**: 最低ライン {result['demand_eco_baseline']}人区"
        f"（大宮エコ1人換算） ／ エコ供給 {result['eco_supply']}人区 → "
        f"大宮駅前を**エコ2人**にできる日は最大 "
        f"**{result['omiya_two_eco_days']}日** "
        f"（通常営業 {result['omiya_normal_open_days']}日中。"
        f"残りの日は「人数少（大宮）」の警告が出るのが正常）"
    )

    gap = int(result["east_west_gap"])
    dedicated = "・".join(result["east_west_dedicated"])
    subs = "・".join(result["east_west_substitutes"])
    if gap > 0:
        per_person = gap / max(1, len(result["east_west_substitutes"]))
        lines.append(
            f"**3. 東口・西口**: 必要 {result['east_west_demand']}人区 ／ "
            f"専任（{dedicated}）供給 {result['east_west_dedicated_supply']}人区 → "
            f"**差{gap}人区**を代替要員（{subs}）が補う想定"
            f"（1人あたり平均 約{per_person:.1f}日）"
        )
    else:
        lines.append(
            f"**3. 東口・西口**: 必要 {result['east_west_demand']}人区 ／ "
            f"専任（{dedicated}）供給 {result['east_west_dedicated_supply']}人区 → "
            "🟢 専任だけで充足可能"
        )

    return lines


# ============================================================
# 動作テスト（2026年8月を前任者の手計算と突き合わせる）
# ============================================================

if __name__ == "__main__":
    from .employees import shift_active_employees
    from .generator import determine_operation_modes
    from .submission_loader import load_submissions_for_month

    year, month = 2026, 8
    employees = list(shift_active_employees())
    modes = determine_operation_modes(year, month)
    expected = [e.name for e in employees if not e.is_auxiliary]
    sub = load_submissions_for_month(year, month, expected)
    wr_counts: dict = {}
    for emp, _d, _s in sub.work_requests:
        wr_counts[emp] = wr_counts.get(emp, 0) + 1

    result = compute_monthly_capacity_balance(
        year, month, employees, modes,
        submitted_paid_leave=sub.paid_leave_days,
        work_request_counts=wr_counts,
    )
    print(f"【{year}年{month}月 需給バランス】"
          f"（省人員{result['reduced_days']}日・東口休{result['higashi_closed_days']}日）")
    for line in balance_summary_lines(result):
        print(" ", line.replace("**", ""))
    print("\n供給明細:")
    for row in result["supply_rows"]:
        print(f"  {row['氏名']}: {row['供給人区']}人区 ← {row['計算式']} {row['エコ']}")
