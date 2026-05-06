"""
シフト自動生成エンジン
================================================
OR-Tools の CP-SAT ソルバーを使い、希望データから最適なシフトを生成する。

設計方針:
- ハード制約は CP-SAT のモデルに直接エンコード
- ソフト制約（在勤割合・目標日数）は目的関数の重み付き最適化
- 山本さんの特殊ロジックは本体ソルバー後の後処理で対応
- 顧問は通常シフト対象外（緊急時のみ手動追加）

使い方:
    from prototype.generator import generate_shift
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS,
        work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
    )
"""

from __future__ import annotations
from calendar import monthrange
from datetime import date
from typing import Optional

from ortools.sat.python import cp_model

from .models import (
    MonthlyShift, ShiftAssignment, Store, Skill, OperationMode, Affinity,
    PreviousMonthCarryover, Role,
)
from .employees import ALL_EMPLOYEES, ECO_STAFF, TICKET_STAFF, get_employee, shift_active_employees
from .rules import (
    NORMAL_CAPACITY, REDUCED_CAPACITY, MINIMUM_CAPACITY,
    HARD_CONSTRAINTS, OMIYA_ANCHOR_STAFF,
    YamamotoLogic, MAY_2026_HOLIDAY_OVERRIDES, DEFAULT_HOLIDAY_DAYS_MAY,
)


# ============================================================
# 営業モードの自動判定
# ============================================================

def determine_operation_modes(year: int, month: int) -> dict[int, OperationMode]:
    """
    日本のカレンダーに基づき、各日の営業モードを自動判定する。

    暫定実装: GW・お盆・SW・年末年始のみ判定。
    本番では内閣府の祝日CSVと連携する予定。
    """
    days = monthrange(year, month)[1]
    modes: dict[int, OperationMode] = {}

    for day in range(1, days + 1):
        d = date(year, month, day)

        # 年末年始休業
        if (month == 12 and day == 31) or (month == 1 and day in (1, 2)):
            modes[day] = OperationMode.CLOSED
        # ゴールデンウィーク（パターンB：連休全体）
        elif month == 4 and day == 29:
            modes[day] = OperationMode.REDUCED
        elif month == 5 and 1 <= day <= 5:
            modes[day] = OperationMode.REDUCED
        # お盆
        elif month == 8 and 13 <= day <= 16:
            modes[day] = OperationMode.REDUCED
        # シルバーウィーク（簡易版：9月の連休）
        elif month == 9 and 19 <= day <= 23:
            modes[day] = OperationMode.REDUCED
        else:
            modes[day] = OperationMode.NORMAL

    return modes


# ============================================================
# CP-SAT ソルバー本体
# ============================================================

# シフト稼働対象の主要店舗（休みも含めた選択肢）
ALL_STORES = [
    Store.AKABANE, Store.HIGASHIGUCHI, Store.OMIYA,
    Store.NISHIGUCHI, Store.SUZURAN, Store.OFF,
]


def generate_shift(
    year: int,
    month: int,
    off_requests: dict[str, list[int]],
    work_requests: list[tuple[str, int, Optional[Store]]],
    prev_month: list[PreviousMonthCarryover],
    flexible_off: Optional[list[tuple[str, list[int], int]]] = None,
    holiday_overrides: Optional[dict[str, int]] = None,
    default_holidays: int = DEFAULT_HOLIDAY_DAYS_MAY,
    operation_modes: Optional[dict[int, OperationMode]] = None,
    consec_exceptions: Optional[list[str]] = None,
    max_consec_override: Optional[int] = None,
    time_limit_seconds: int = 60,
    random_seed: int = 42,
    verbose: bool = True,
) -> Optional[MonthlyShift]:
    """
    Args:
        consec_exceptions: 連勤上限チェックを今月のみ免除する従業員リスト
                           例: 5月の野澤さん（前月4連勤＋5/1出勤希望で5連勤になる特例）
    """
    """
    1ヶ月分のシフトを自動生成する。

    Returns:
        生成された MonthlyShift。解が見つからない場合は None。
    """
    flexible_off = flexible_off or []
    holiday_overrides = holiday_overrides or {}
    operation_modes = operation_modes or determine_operation_modes(year, month)
    consec_exceptions = consec_exceptions or []

    # 主要なメイン稼働メンバーリスト（顧問・山本を除く）
    main_employees = [
        e for e in shift_active_employees() if not e.is_auxiliary
    ]
    yamamoto = next((e for e in ALL_EMPLOYEES if e.name == "山本"), None)

    days_in_month = monthrange(year, month)[1]
    days = list(range(1, days_in_month + 1))
    main_stores = [s for s in ALL_STORES if s != Store.OFF]

    # ============================================================
    # CP-SAT モデルの構築
    # ============================================================
    model = cp_model.CpModel()

    # x[e_name][day][store] = 1 ならば e_name は day に store で勤務、0 ならばそうでない
    # 「休み」は専用の off[e_name][day] = 1 で表現
    x: dict = {}
    off: dict = {}
    for e in main_employees:
        x[e.name] = {}
        off[e.name] = {}
        for d in days:
            x[e.name][d] = {}
            for s in main_stores:
                x[e.name][d][s] = model.NewBoolVar(f"x_{e.name}_{d}_{s.name}")
            off[e.name][d] = model.NewBoolVar(f"off_{e.name}_{d}")

    # ============================================================
    # 制約 1: 各 (従業員, 日) は店舗1つ or 休み（排他）
    # ============================================================
    for e in main_employees:
        for d in days:
            model.Add(sum(x[e.name][d][s] for s in main_stores) + off[e.name][d] == 1)

    # ============================================================
    # 制約 2: 休み希望厳守
    # ============================================================
    for emp_name, off_days in off_requests.items():
        if emp_name not in [e.name for e in main_employees]:
            continue
        for d in off_days:
            model.Add(off[emp_name][d] == 1)

    # ============================================================
    # 制約 3: 出勤希望厳守（指定店舗があればその店舗）
    # ============================================================
    for name, d, store in work_requests:
        if name not in [e.name for e in main_employees]:
            continue
        model.Add(off[name][d] == 0)  # その日は必ず出勤
        if store is not None:
            model.Add(x[name][d][store] == 1)

    # ============================================================
    # 制約 4: 柔軟休み希望（候補日のうち N 日を休みに）
    # ============================================================
    for name, candidate_days, n_required in flexible_off:
        if name not in [e.name for e in main_employees]:
            continue
        model.Add(sum(off[name][d] for d in candidate_days) >= n_required)

    # ============================================================
    # 制約 5: 配置不可な店舗には配置しない（Affinity.NONE）
    # ============================================================
    for e in main_employees:
        for s in main_stores:
            if e.affinities.get(s) == Affinity.NONE:
                for d in days:
                    model.Add(x[e.name][d][s] == 0)

    # ============================================================
    # 制約 6: 各日・各店舗の必要人数
    # ============================================================
    capacity_by_mode = {
        OperationMode.NORMAL: NORMAL_CAPACITY,
        OperationMode.REDUCED: REDUCED_CAPACITY,
        OperationMode.MINIMUM: MINIMUM_CAPACITY,
        OperationMode.CLOSED: {},
    }
    # 店頭の必須エコ要員はSkill.ECOのみ。
    # ECO_SUPPORT は店頭直接応対しないためチケット枠に含める。
    eco_employees = [e for e in main_employees if e.skill == Skill.ECO]
    ticket_employees = [
        e for e in main_employees
        if e.skill in (Skill.TICKET, Skill.ECO_SUPPORT)
    ]

    # 大宮の「人数少」状態を表す変数（人員不足時はエコ1+チケット1で可）
    omiya_short = {d: model.NewBoolVar(f"omiya_short_{d}") for d in days}

    for d in days:
        mode = operation_modes.get(d, OperationMode.NORMAL)
        cap = capacity_by_mode[mode]

        # 営業停止日：全員休み
        if mode == OperationMode.CLOSED:
            for e in main_employees:
                model.Add(off[e.name][d] == 1)
            continue

        # 最小営業モード：赤羽・大宮以外の店舗には誰も配置しない
        if mode == OperationMode.MINIMUM:
            closed_stores = [Store.HIGASHIGUCHI, Store.NISHIGUCHI, Store.SUZURAN]
            for e in main_employees:
                for s in closed_stores:
                    model.Add(x[e.name][d][s] == 0)

        weekday = date(year, month, d).weekday()

        # 各店舗ごとに必要人数を制約
        for s, store_cap in cap.items():
            # 月曜休店チェック（東口）
            if weekday in store_cap.closed_dow:
                for e in main_employees:
                    model.Add(x[e.name][d][s] == 0)
                continue

            eco_at_store = sum(x[e.name][d][s] for e in eco_employees)
            ticket_at_store = sum(x[e.name][d][s] for e in ticket_employees)

            # 大宮の特殊ルール:
            # 通常: eco 2 + ticket 1
            # 人数少時: eco 1 + ticket 1 (omiya_short=1 でフラグ)
            if s == Store.OMIYA and mode == OperationMode.NORMAL:
                # eco_at_store >= 2 OR omiya_short = 1
                # → eco_at_store + 2 * omiya_short >= 2
                model.Add(eco_at_store + 2 * omiya_short[d] >= 2)
                model.Add(eco_at_store >= 1)  # 最低1名は必須
                model.Add(eco_at_store <= 2)
                model.Add(ticket_at_store >= 1)
                continue

            # すずらんの特殊ルール: eco1+ticket2 OR eco2+ticket1
            # → eco + ticket >= 3, eco >= 1, ticket >= 1
            if s == Store.SUZURAN and mode == OperationMode.NORMAL:
                model.Add(eco_at_store >= 1)
                model.Add(eco_at_store <= 2)
                model.Add(ticket_at_store >= 1)
                model.Add(ticket_at_store <= 2)
                model.Add(eco_at_store + ticket_at_store >= 3)
                continue

            # 通常の制約
            model.Add(eco_at_store >= store_cap.eco_min)
            model.Add(eco_at_store <= store_cap.eco_max)
            model.Add(ticket_at_store >= store_cap.ticket_min)

    # ============================================================
    # 制約 7: 大宮駅前店アンカー（春山 or 下地が必ずいる）
    # ============================================================
    for d in days:
        mode = operation_modes.get(d, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        anchor_present = sum(
            x[name][d][Store.OMIYA]
            for name in OMIYA_ANCHOR_STAFF
            if name in [e.name for e in main_employees]
        )
        model.Add(anchor_present >= 1)

    # ============================================================
    # 制約 8: 連勤上限
    # ============================================================
    # 設計方針:
    # - max_consec_override が指定された場合: 厳密に max_consec まで許容（ハード）
    # - 指定がない場合: 5連勤までハード制約、4連勤超えはソフトペナルティ
    #   （手動運用で5連勤が頻出するため、AIも同水準を許容しつつ4連勤を目指す）
    # - 前月持ち越しの境界: 前月から既に max_consec 連勤の人は月初強制休み
    #   ただし consec_exceptions に入っている人は境界スキップ（特例）
    # ============================================================
    base_max_consec = HARD_CONSTRAINTS["max_consecutive_work_days"]  # 4
    hard_max_consec = max_consec_override or 5  # ハード制約は5連勤
    soft_threshold = base_max_consec  # 4連勤を超えたらペナルティ加算

    # 前月最終日からの連続出勤日数を計算
    prev_consec_map: dict[str, int] = {}
    for p in prev_month:
        if not p.last_working_days:
            continue
        sorted_days = sorted(p.last_working_days, reverse=True)
        prev_month_num = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        last_day = monthrange(prev_year, prev_month_num)[1]
        consec = 0
        expected = last_day
        for dd in sorted_days:
            if dd == expected:
                consec += 1
                expected -= 1
            else:
                break
        prev_consec_map[p.employee] = consec

    over_4_indicators = []  # 4連勤超えのインジケータ（ソフトペナルティ用）

    for e in main_employees:
        prev = prev_consec_map.get(e.name, 0)
        if e.constraint_check_excluded and e.name != "大塚":
            continue

        # ハード制約：(hard_max_consec + 1)日窓内の出勤日数 ≤ hard_max_consec
        for start_day in days:
            window_days = list(range(start_day, min(start_day + hard_max_consec + 1, days_in_month + 1)))
            if len(window_days) < hard_max_consec + 1:
                continue
            model.Add(
                sum(1 - off[e.name][d] for d in window_days) <= hard_max_consec
            )

        # ソフト制約：4連勤超え（5連勤）の発生数を計算してペナルティ
        if hard_max_consec > soft_threshold:
            for start_day in days:
                window_days = list(range(start_day, min(start_day + soft_threshold + 1, days_in_month + 1)))
                if len(window_days) < soft_threshold + 1:
                    continue
                # 4連勤超え判定: window 内の出勤数 > 4
                over = model.NewBoolVar(f"over4_{e.name}_{start_day}")
                model.Add(
                    sum(1 - off[e.name][d] for d in window_days) >= soft_threshold + 1
                ).OnlyEnforceIf(over)
                model.Add(
                    sum(1 - off[e.name][d] for d in window_days) <= soft_threshold
                ).OnlyEnforceIf(over.Not())
                over_4_indicators.append(over)

        # 前月境界制約: 前月から prev 連勤している場合、月初の連勤も合算で hard_max_consec を超えないように
        # 例：prev=3, hard_max=5 なら [5/1, 5/2, 5/3] のうち少なくとも1日は休み
        # （前月3連勤 + 5/1-3 全勤務 = 6連勤を防ぐ）
        if prev > 0 and e.name not in consec_exceptions:
            window_size = hard_max_consec - prev + 1
            allowed_work = hard_max_consec - prev
            if window_size > 0 and allowed_work >= 0:
                window = list(range(1, min(window_size + 1, days_in_month + 1)))
                if window:
                    model.Add(
                        sum(1 - off[e.name][d] for d in window) <= allowed_work
                    )

    # ============================================================
    # 制約 10: 休日日数の最低ライン
    # ============================================================
    for e in main_employees:
        if e.constraint_check_excluded:
            continue
        required_off = holiday_overrides.get(e.name, default_holidays)
        model.Add(sum(off[e.name][d] for d in days) >= required_off)

    # ============================================================
    # 制約 11: 3連休禁止（休み希望日が含まれる連休は除く）
    # ★簡易版：休み希望が1つでも含まれる窓は除外（厳密には全部off要求の場合のみ除外すべきだが緩める）
    # ============================================================
    for e in main_employees:
        if e.constraint_check_excluded:
            continue
        emp_off_days = set(off_requests.get(e.name, []))
        # 柔軟休み候補日も除外対象に追加
        for fname, fcand_days, _ in (flexible_off or []):
            if fname == e.name:
                emp_off_days.update(fcand_days)
        for start in range(1, days_in_month - 1):
            window = [start, start + 1, start + 2]
            # 1日でも休み希望に含まれていればこの窓は3連休チェック除外
            # （希望休が連続2日 + 自然休1日 のパターンを許容するため）
            if any(d in emp_off_days for d in window):
                continue
            model.Add(off[e.name][start] + off[e.name][start + 1] + off[e.name][start + 2] <= 2)

    # ============================================================
    # 制約 12: 大塚さんの月間出勤日数（5月は10日）
    # ============================================================
    if any(e.name == "大塚" for e in main_employees):
        # 5月のみ：合計10日出勤
        if year == 2026 and month == 5:
            model.Add(sum(1 - off["大塚"][d] for d in days) == 10)

    # ============================================================
    # 制約 13: 南さんは出勤希望日のみ稼働
    # ============================================================
    if any(e.name == "南" for e in main_employees):
        minami_work_days = set(
            d for name, d, _ in work_requests if name == "南"
        )
        for d in days:
            if d not in minami_work_days:
                model.Add(off["南"][d] == 1)

    # ============================================================
    # 目的関数（ソフト制約）: 在勤要望の達成度を最大化
    # ============================================================
    objective_terms = []

    # 各従業員 × 各店舗の在勤数を勘定し、Affinity に応じた重み付けで最適化
    AFFINITY_WEIGHT = {
        Affinity.STRONG: 10,    # 強：是非ここに配置したい
        Affinity.MEDIUM: 5,     # 中：可能ならここに
        Affinity.WEAK: 1,       # 弱：少しだけ
        Affinity.NONE: 0,       # 不可：配置しない（既にハード制約で除外）
    }
    for e in main_employees:
        for s in main_stores:
            aff = e.affinities.get(s, Affinity.NONE)
            weight = AFFINITY_WEIGHT[aff]
            if weight > 0:
                # その店舗の出勤回数 × 重み を加算
                for d in days:
                    objective_terms.append(weight * x[e.name][d][s])

    # 目標出勤日数への近づき度（不足分を強くペナルティ）
    target_penalty_terms = []
    for e in main_employees:
        if e.annual_target_days is None:
            continue
        target_monthly = round(e.annual_target_days / 12)
        actual = sum(1 - off[e.name][d] for d in days)
        # actual < target ならペナルティ
        shortfall = model.NewIntVar(0, days_in_month, f"shortfall_{e.name}")
        model.Add(shortfall >= target_monthly - actual)
        model.Add(shortfall >= 0)
        target_penalty_terms.append(shortfall)

    # 在勤要望スコアを最大化、目標未達/連勤超過ペナルティを最小化
    obj = sum(objective_terms)
    if target_penalty_terms:
        obj = obj - 20 * sum(target_penalty_terms)
    if over_4_indicators:
        # 4連勤超え1件あたり 50 ポイントのペナルティ（できる限り避けたい）
        obj = obj - 50 * sum(over_4_indicators)
    model.Maximize(obj)

    # ============================================================
    # ソルバー実行
    # ============================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # 並列ワーカーは1にして決定論的動作を保証
    # （複数ワーカーで並列探索すると実行ごとに別解になる）
    solver.parameters.num_search_workers = 1
    # シード固定: 同じ入力に対して毎回同じシフトが生成されるようにする
    solver.parameters.random_seed = random_seed
    if verbose:
        print(f"ソルバー実行中... (制限時間: {time_limit_seconds}秒, シード: {random_seed})")

    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if verbose:
            print(f"❌ 解が見つかりませんでした (status: {solver.StatusName(status)})")
        return None

    if verbose:
        print(f"✅ 解が見つかりました (status: {solver.StatusName(status)}, 目的値: {solver.ObjectiveValue():.0f})")

    # ============================================================
    # 解を MonthlyShift に変換
    # ============================================================
    shift = MonthlyShift(year=year, month=month)
    shift.operation_modes = operation_modes

    for e in main_employees:
        for d in days:
            if solver.Value(off[e.name][d]) == 1:
                shift.assignments.append(ShiftAssignment(
                    employee=e.name, day=d, store=Store.OFF,
                ))
            else:
                for s in main_stores:
                    if solver.Value(x[e.name][d][s]) == 1:
                        shift.assignments.append(ShiftAssignment(
                            employee=e.name, day=d, store=s,
                        ))
                        break

    # ============================================================
    # 山本さんの後処理（特殊ロジック）
    # ============================================================
    if yamamoto is not None:
        yamamoto_off = set(off_requests.get("山本", []))
        for d in days:
            mode = operation_modes.get(d, OperationMode.NORMAL)
            if mode == OperationMode.CLOSED:
                continue

            if d in yamamoto_off:
                shift.assignments.append(ShiftAssignment(
                    employee="山本", day=d, store=Store.OFF,
                ))
                continue

            # その日の赤羽の構成を確認
            # ECO_SUPPORT はチケット枠としてカウント（店頭応対しないため）
            akabane_workers = [
                a for a in shift.get_day_assignments(d) if a.store == Store.AKABANE
            ]
            akabane_eco = sum(
                1 for a in akabane_workers if get_employee(a.employee).skill == Skill.ECO
            )
            akabane_ticket = sum(
                1 for a in akabane_workers
                if get_employee(a.employee).skill in (Skill.TICKET, Skill.ECO_SUPPORT)
            )
            if YamamotoLogic.should_deploy(akabane_eco, akabane_ticket, False):
                shift.assignments.append(ShiftAssignment(
                    employee="山本", day=d, store=Store.AKABANE,
                ))
            # それ以外は山本の assignment を作らない（出勤も休みもしない＝空白扱い）

    return shift


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("Generator module loaded.")
    print("Use generate_shift(...) to create a shift.")
