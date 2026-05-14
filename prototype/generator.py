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
    HARD_CONSTRAINTS, OMIYA_ANCHOR_STAFF, HIGASHIGUCHI_ALLOWED_STAFF,
    YamamotoLogic, MAY_2026_HOLIDAY_OVERRIDES, DEFAULT_HOLIDAY_DAYS_MAY,
    OFF_MAIN_STORE_MINIMUMS, CONSTRAINT_EXCLUDED,
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
    preferred_work_requests: Optional[list[tuple[str, int, Optional[Store]]]] = None,
    preferred_work_groups: Optional[list[tuple[str, list[int], int, Optional[Store]]]] = None,
    preferred_consecutive_off: Optional[list[tuple[str, int]]] = None,
    monthly_store_count_rules: Optional[list[dict]] = None,
    historical_actual_preferences: Optional[list[tuple[str, int, Store]]] = None,
    strict_warning_constraints: bool = True,
    advisor_max_days: Optional[int] = 0,
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
    preferred_work_requests = preferred_work_requests or []
    preferred_work_groups = preferred_work_groups or []
    preferred_consecutive_off = preferred_consecutive_off or []
    monthly_store_count_rules = monthly_store_count_rules or []
    historical_actual_preferences = historical_actual_preferences or []
    operation_modes = operation_modes or determine_operation_modes(year, month)
    consec_exceptions = consec_exceptions or []

    # 主要なメイン稼働メンバーリスト（山本を除く）。
    # 顧問は基本的に自動投入しない。必要な場合だけ advisor_max_days を上げる。
    main_employees = [
        e for e in shift_active_employees() if not e.is_auxiliary
    ]
    advisor = next((e for e in ALL_EMPLOYEES if e.name == "顧問"), None)
    if advisor is not None and all(e.name != "顧問" for e in main_employees):
        main_employees.append(advisor)
    main_employee_names = {e.name for e in main_employees}
    yamamoto = next((e for e in ALL_EMPLOYEES if e.name == "山本"), None)

    days_in_month = monthrange(year, month)[1]
    days = list(range(1, days_in_month + 1))
    main_stores = [s for s in ALL_STORES if s != Store.OFF]

    # 本人の「×」休み希望は最優先。日付を安全に丸め、
    # 同じ日に出勤希望が混ざっている場合も「休み希望」を優先する。
    normalized_off_requests: dict[str, list[int]] = {}
    for emp_name, off_days in (off_requests or {}).items():
        safe_days = sorted({
            int(d) for d in off_days
            if str(d).isdigit() and 1 <= int(d) <= days_in_month
        })
        if safe_days:
            normalized_off_requests[emp_name] = safe_days
    off_requests = normalized_off_requests

    normalized_work_requests = []
    for name, d, store in (work_requests or []):
        try:
            day = int(d)
        except (TypeError, ValueError):
            continue
        if not (1 <= day <= days_in_month):
            continue
        if day in set(off_requests.get(name, [])):
            continue
        normalized_work_requests.append((name, day, store))
    work_requests = normalized_work_requests

    normalized_preferred_work_requests = []
    for name, d, store in (preferred_work_requests or []):
        try:
            day = int(d)
        except (TypeError, ValueError):
            continue
        if not (1 <= day <= days_in_month):
            continue
        if day in set(off_requests.get(name, [])):
            continue
        normalized_preferred_work_requests.append((name, day, store))
    preferred_work_requests = normalized_preferred_work_requests

    normalized_preferred_work_groups = []
    for name, candidate_days, required_count, store in (preferred_work_groups or []):
        safe_candidates = sorted({
            int(d) for d in candidate_days
            if str(d).isdigit()
            and 1 <= int(d) <= days_in_month
            and int(d) not in set(off_requests.get(name, []))
        })
        try:
            required = int(required_count)
        except (TypeError, ValueError):
            required = 1
        if safe_candidates and required > 0:
            normalized_preferred_work_groups.append((
                name,
                safe_candidates,
                min(required, len(safe_candidates)),
                store,
            ))
    preferred_work_groups = normalized_preferred_work_groups

    normalized_historical_actual_preferences = []
    for name, d, store in (historical_actual_preferences or []):
        try:
            day = int(d)
        except (TypeError, ValueError):
            continue
        if not (1 <= day <= days_in_month):
            continue
        if not isinstance(store, Store):
            continue
        normalized_historical_actual_preferences.append((name, day, store))
    historical_actual_preferences = normalized_historical_actual_preferences
    historical_reconciliation_mode = bool(historical_actual_preferences)

    normalized_flexible_off = []
    for name, candidate_days, n_required in (flexible_off or []):
        safe_candidates = sorted({
            int(d) for d in candidate_days
            if str(d).isdigit() and 1 <= int(d) <= days_in_month
        })
        if safe_candidates:
            normalized_flexible_off.append((name, safe_candidates, int(n_required)))
    flexible_off = normalized_flexible_off
    normalized_preferred_consecutive_off = []
    for name, block_len in (preferred_consecutive_off or []):
        try:
            block_len_int = int(block_len)
        except (TypeError, ValueError):
            continue
        if 2 <= block_len_int <= min(7, days_in_month):
            normalized_preferred_consecutive_off.append((name, block_len_int))
    preferred_consecutive_off = normalized_preferred_consecutive_off
    yamamoto_off_days = set(off_requests.get("山本", []))

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
        if emp_name not in main_employee_names:
            continue
        for d in off_days:
            model.Add(off[emp_name][d] == 1)

    # 出勤希望・店舗希望は「できる限り反映する」希望扱い。
    # 本人の × 休み希望だけをハード制約にし、出勤希望は目的関数で強く優先する。
    # 出勤希望日のみ稼働する人（例: 南さん）は、希望日以外には配置しない。
    request_allowed_days_by_employee: dict[str, set[int]] = {}
    for name, d, _store in work_requests:
        request_allowed_days_by_employee.setdefault(name, set()).add(int(d))
    for name, d, _store in preferred_work_requests:
        request_allowed_days_by_employee.setdefault(name, set()).add(int(d))
    for name, candidate_days, _required, _store in preferred_work_groups:
        request_allowed_days_by_employee.setdefault(name, set()).update(int(d) for d in candidate_days)
    for e in main_employees:
        if not getattr(e, "only_on_request_days", False):
            continue
        allowed_days = request_allowed_days_by_employee.get(e.name, set())
        for d in days:
            if d not in allowed_days:
                model.Add(off[e.name][d] == 1)

    # ============================================================
    # 制約 4: 柔軟休み希望（候補日のうち N 日を休みに）
    # ============================================================
    for name, candidate_days, n_required in flexible_off:
        if name not in main_employee_names:
            continue
        model.Add(sum(off[name][d] for d in candidate_days) >= n_required)

    # ============================================================
    # 制約 5: 配置不可な店舗には配置しない（Affinity.NONE）
    # ============================================================
    affinity_none_assignments = []
    absolute_allowed_stores = {
        "土井": {Store.HIGASHIGUCHI},
        "下地": {Store.OMIYA},
        "南": {Store.AKABANE, Store.OMIYA, Store.SUZURAN},
    }
    for e in main_employees:
        for s in main_stores:
            affinity_none = e.affinities.get(s) == Affinity.NONE
            fixed_allowed = absolute_allowed_stores.get(e.name)
            hard_forbidden = (
                fixed_allowed is not None and s not in fixed_allowed
            ) or (
                getattr(e, "only_on_request_days", False) and affinity_none
            ) or (
                affinity_none and not historical_reconciliation_mode
            )
            if hard_forbidden:
                for d in days:
                    model.Add(x[e.name][d][s] == 0)
            elif affinity_none:
                affinity_none_assignments.extend(x[e.name][d][s] for d in days)

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
    higashi_unexpected_assignments = []

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
            total_at_store = eco_at_store + ticket_at_store

            # 赤羽東口店: エコ1名のみ。例外なし。
            if s == Store.HIGASHIGUCHI:
                for e in main_employees:
                    if e.name != "顧問" and e.name not in HIGASHIGUCHI_ALLOWED_STAFF:
                        higashi_unexpected_assignments.append(x[e.name][d][s])
                        model.Add(x[e.name][d][s] == 0)
                model.Add(eco_at_store == 1)
                model.Add(ticket_at_store == 0)
                continue

            # 赤羽駅前店:
            # 基本はエコ1+チケット2。例外としてエコ2+チケット1も可。
            # チケット対応が1名分だけの日は、後処理で山本さんを補助投入する。
            if s == Store.AKABANE and mode == OperationMode.NORMAL:
                model.Add(eco_at_store >= 1)
                if d in yamamoto_off_days:
                    model.Add(total_at_store >= 3)
                else:
                    model.Add(total_at_store >= 2)
                continue

            # 大宮の特殊ルール:
            # 通常: エコ対応者1名以上 + 合計3名以上
            # 人数少時: エコ対応者1名以上 + 2名体制も許容（omiya_short=1 でフラグ）
            if s == Store.OMIYA and mode == OperationMode.NORMAL:
                model.Add(eco_at_store >= 1)  # 最低1名は必須
                model.Add(total_at_store + omiya_short[d] >= 3)
                continue

            # すずらんの特殊ルール: エコ対応者1名以上 + 合計3名以上
            # エコ担当はチケット対応も可能なため、チケット専任2名には固定しない。
            if s == Store.SUZURAN and mode == OperationMode.NORMAL:
                model.Add(eco_at_store >= 1)
                model.Add(ticket_at_store <= 2)
                model.Add(total_at_store >= 3)
                continue

            # 通常の制約
            model.Add(eco_at_store >= store_cap.eco_min)
            model.Add(total_at_store >= store_cap.eco_min + store_cap.ticket_min)

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
    three_off_indicators = []  # 希望休を含まない3連休のインジケータ
    two_off_goal_terms = []  # 2連休を確保できた人のインジケータ

    for e in main_employees:
        prev = prev_consec_map.get(e.name, 0)
        if e.role == Role.ADVISOR:
            continue
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
        if e.constraint_check_excluded or e.role == Role.ADVISOR:
            continue
        required_off = holiday_overrides.get(e.name, default_holidays)
        model.Add(sum(off[e.name][d] for d in days) >= required_off)

    # ============================================================
    # 制約 10.5: 2連休を月1回以上
    # ============================================================
    for e in main_employees:
        if e.constraint_check_excluded or e.name in CONSTRAINT_EXCLUDED or e.role == Role.ADVISOR:
            continue
        two_day_blocks = []
        for start in range(1, days_in_month):
            block = model.NewBoolVar(f"two_off_block_{e.name}_{start}")
            model.AddBoolAnd([off[e.name][start], off[e.name][start + 1]]).OnlyEnforceIf(block)
            model.AddBoolOr([off[e.name][start].Not(), off[e.name][start + 1].Not()]).OnlyEnforceIf(block.Not())
            two_day_blocks.append(block)
        if two_day_blocks:
            has_two_day_block = model.NewBoolVar(f"has_two_off_block_{e.name}")
            model.AddMaxEquality(has_two_day_block, two_day_blocks)
            if strict_warning_constraints:
                model.Add(has_two_day_block == 1)
            two_off_goal_terms.append(has_two_day_block)

    # ============================================================
    # 制約 11: 3連休は原則避ける（ソフト）
    # 人員が多い月は3連休もあり得るため、禁止ではなくペナルティとして扱う。
    # 休み希望日が含まれる3連休は本人希望の反映としてペナルティ対象外にする。
    # ============================================================
    for e in main_employees:
        if e.constraint_check_excluded or e.role == Role.ADVISOR:
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
            three_off = model.NewBoolVar(f"three_off_{e.name}_{start}")
            off_sum = off[e.name][start] + off[e.name][start + 1] + off[e.name][start + 2]
            model.Add(off_sum == 3).OnlyEnforceIf(three_off)
            model.Add(off_sum <= 2).OnlyEnforceIf(three_off.Not())
            three_off_indicators.append(three_off)

    # ============================================================
    # 制約 12: 大塚さんの月間出勤日数（5月は10日）
    # ============================================================
    if any(e.name == "大塚" for e in main_employees):
        # 5月のみ：合計10日出勤
        if year == 2026 and month == 5:
            model.Add(sum(1 - off["大塚"][d] for d in days) == 10)

    # ============================================================
    # 制約 13: 楯・春山・長尾は月3日以上、メイン店舗以外で勤務
    # ============================================================
    for name, (main_store, min_count) in OFF_MAIN_STORE_MINIMUMS.items():
        if name not in main_employee_names:
            continue
        outside_main = [
            x[name][d][s]
            for d in days
            for s in main_stores
            if s != main_store
        ]
        if outside_main:
            model.Add(sum(outside_main) >= int(min_count))

    # ============================================================
    # 目的関数（ソフト制約）: 在勤要望の達成度を最大化
    # ============================================================
    objective_terms = []
    preferred_consecutive_off_indicators = []
    preferred_work_terms = []
    monthly_rule_terms = []
    monthly_rule_penalty_terms = []
    historical_actual_terms = []

    # 提出フォームの「○」や自由記載の「出勤希望」「○日は赤羽希望」などはソフト制約。
    # まず出勤できるなら出勤、出勤になった場合は希望店舗へ、という2段階で優先する。
    combined_work_preferences = list(work_requests) + list(preferred_work_requests)
    seen_work_preferences: set[tuple[str, int, Optional[Store]]] = set()
    for name, d, store in combined_work_preferences:
        key = (name, int(d), store)
        if key in seen_work_preferences:
            continue
        seen_work_preferences.add(key)
        if name not in main_employee_names:
            continue
        any_work = sum(x[name][d][s] for s in main_stores)
        if store is not None and store in main_stores:
            preferred_work_terms.append(70 * any_work)
            preferred_work_terms.append(130 * x[name][d][store])
        else:
            preferred_work_terms.append(90 * any_work)

    # 「3日か29日のいずれか1日は出勤したい」のような自由記載は、
    # 候補のうち指定回数分だけ満たせるように優先する。
    for idx, (name, candidate_days, required_count, store) in enumerate(preferred_work_groups):
        if name not in main_employee_names:
            continue
        work_count = sum(
            x[name][d][s]
            for d in candidate_days
            for s in main_stores
        )
        capped = model.NewIntVar(0, int(required_count), f"preferred_work_group_{name}_{idx}")
        model.AddMinEquality(capped, [work_count, model.NewConstant(int(required_count))])
        preferred_work_terms.append(180 * capped)
        if store is not None and store in main_stores:
            preferred_work_terms.extend(
                80 * x[name][d][store]
                for d in candidate_days
            )

    # 過去月のすり合わせでは、実績シフトにできるだけ近い解を優先する。
    for name, d, store in historical_actual_preferences:
        if name not in main_employee_names:
            continue
        weight = 70000 if name == "顧問" else 900
        if store == Store.OFF:
            historical_actual_terms.append(120 * off[name][d])
        elif store in main_stores:
            historical_actual_terms.append(weight * x[name][d][store])

    # 自由記載の「4連休がほしい」などは、解なしを避けるためソフト制約として強く優先する。
    for name, block_len in preferred_consecutive_off:
        if name not in main_employee_names:
            continue
        for start in range(1, days_in_month - block_len + 2):
            window = list(range(start, start + block_len))
            block = model.NewBoolVar(f"preferred_off_block_{name}_{block_len}_{start}")
            off_sum = sum(off[name][d] for d in window)
            model.Add(off_sum == block_len).OnlyEnforceIf(block)
            model.Add(off_sum <= block_len - 1).OnlyEnforceIf(block.Not())
            preferred_consecutive_off_indicators.append(block)

    # 月別ルール: 特定スタッフを、その月だけ指定店舗へ一定回数入れる/抑える/禁止する。
    # 例: 6月は牧野さんを研修のため西口に3回。
    for rule in monthly_store_count_rules:
        if not rule or not rule.get("employee"):
            continue
        name = str(rule.get("employee"))
        if name not in main_employee_names:
            continue
        store_values = rule.get("stores") or []
        stores = []
        for raw_store in store_values:
            try:
                store = raw_store if isinstance(raw_store, Store) else Store[str(raw_store)]
            except Exception:
                store = next(
                    (
                        s for s in main_stores
                        if s.display_name == str(raw_store) or s.value == str(raw_store)
                    ),
                    None,
                )
            if store in main_stores:
                stores.append(store)
        stores = sorted(set(stores), key=lambda s: s.name)
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
        actual_count = sum(x[name][d][s] for d in days for s in stores)
        severity = str(rule.get("severity", "WARNING")).upper()
        if comparison in ("max", "forbid"):
            if severity == "ERROR":
                model.Add(actual_count <= required_count)
            else:
                over = model.NewIntVar(0, days_in_month, f"monthly_rule_over_{name}_{len(monthly_rule_penalty_terms)}")
                model.Add(over >= actual_count - required_count)
                model.Add(over >= 0)
                monthly_rule_penalty_terms.append(over)
        elif comparison == "exact":
            if severity == "ERROR":
                model.Add(actual_count == required_count)
            else:
                diff = model.NewIntVar(-days_in_month, days_in_month, f"monthly_rule_diff_{name}_{len(monthly_rule_penalty_terms)}")
                abs_diff = model.NewIntVar(0, days_in_month, f"monthly_rule_abs_{name}_{len(monthly_rule_penalty_terms)}")
                model.Add(diff == actual_count - required_count)
                model.AddAbsEquality(abs_diff, diff)
                monthly_rule_penalty_terms.append(abs_diff)
        else:
            if severity == "ERROR":
                model.Add(actual_count >= required_count)
            else:
                capped = model.NewIntVar(0, required_count, f"monthly_rule_{name}_{len(monthly_rule_terms)}")
                model.AddMinEquality(capped, [actual_count, model.NewConstant(required_count)])
                monthly_rule_terms.append(capped)

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
    if two_off_goal_terms:
        # 2連休不足の警告が出ないよう、緩和時でも強く優先する。
        obj = obj + 260 * sum(two_off_goal_terms)
    if three_off_indicators:
        # 3連休はあり得るが、必要がなければ避ける
        obj = obj - 15 * sum(three_off_indicators)
    if preferred_consecutive_off_indicators:
        # 自由記載で明示された連休希望は、通常の3連休回避ペナルティより強く優先する。
        obj = obj + 180 * sum(preferred_consecutive_off_indicators)
    if preferred_work_terms:
        obj = obj + sum(preferred_work_terms)
    if monthly_rule_terms:
        obj = obj + 160 * sum(monthly_rule_terms)
    if monthly_rule_penalty_terms:
        obj = obj - 220 * sum(monthly_rule_penalty_terms)
    if historical_actual_terms:
        obj = obj + sum(historical_actual_terms)
    if affinity_none_assignments:
        # 過去実績では「不可」扱いの店舗にも例外配置があるため、
        # すり合わせ時だけ禁止ではなく強い回避ペナルティにする。
        obj = obj - 260 * sum(affinity_none_assignments)
    if "顧問" in main_employee_names:
        # 顧問は本当に足りない時だけの最終手段。通常スタッフで解がある限り使わない。
        advisor_assignments = [x["顧問"][d][s] for d in days for s in main_stores]
        if advisor_max_days is not None:
            model.Add(sum(advisor_assignments) <= int(advisor_max_days))
        model.AddDecisionStrategy(
            advisor_assignments,
            cp_model.CHOOSE_FIRST,
            cp_model.SELECT_MIN_VALUE,
        )
        obj = obj - 50000 * sum(advisor_assignments)
    # 大宮の2名体制は最終手段。解がある限り通常の3名体制を優先する。
    obj = obj - 100 * sum(omiya_short.values())
    if higashi_unexpected_assignments:
        # 東口は土井さんまたは指定代替4名を強く優先する。
        # ただし過去月の実態確認前なので、解なしにせず大きめのペナルティに留める。
        obj = obj - 200 * sum(higashi_unexpected_assignments)
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
