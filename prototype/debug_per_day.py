"""
1日単位でキャパシティ制約のフィージビリティを検証
================================================
"""

from calendar import monthrange
from datetime import date
from ortools.sat.python import cp_model

from .models import Store, Skill, OperationMode, Affinity
from .employees import shift_active_employees
from .rules import NORMAL_CAPACITY, REDUCED_CAPACITY, OMIYA_ANCHOR_STAFF
from .may_2026_data import OFF_REQUESTS, WORK_REQUESTS


def check_day(day: int) -> tuple[str, list[str]]:
    """指定日のシフトが組めるかチェック"""
    main_employees = [e for e in shift_active_employees() if not e.is_auxiliary]
    main_stores = [Store.AKABANE, Store.HIGASHIGUCHI, Store.OMIYA, Store.NISHIGUCHI, Store.SUZURAN]

    # 5/1-5 は REDUCED, それ以外 NORMAL
    cap_map = REDUCED_CAPACITY if 1 <= day <= 5 else NORMAL_CAPACITY

    model = cp_model.CpModel()
    x = {e.name: {s: model.NewBoolVar(f"x_{e.name}_{s.name}") for s in main_stores} for e in main_employees}
    off = {e.name: model.NewBoolVar(f"off_{e.name}") for e in main_employees}

    # 排他
    for e in main_employees:
        model.Add(sum(x[e.name][s] for s in main_stores) + off[e.name] == 1)

    # Off requests for this day
    for emp_name, off_days in OFF_REQUESTS.items():
        if emp_name not in [e.name for e in main_employees]:
            continue
        if day in off_days:
            model.Add(off[emp_name] == 1)

    # Work requests
    minami_request_days = set(d for n, d, _ in WORK_REQUESTS if n == "南")
    if "南" in [e.name for e in main_employees]:
        if day not in minami_request_days:
            model.Add(off["南"] == 1)
    for n, d, store in WORK_REQUESTS:
        if d != day or n not in [e.name for e in main_employees]:
            continue
        model.Add(off[n] == 0)
        if store is not None:
            model.Add(x[n][store] == 1)

    # Affinity NONE
    for e in main_employees:
        for s in main_stores:
            if e.affinities.get(s) == Affinity.NONE:
                model.Add(x[e.name][s] == 0)

    # Capacity (with flexibility for 大宮 and すずらん)
    eco_employees = [e for e in main_employees if e.skill == Skill.ECO]
    ticket_employees = [e for e in main_employees if e.skill == Skill.TICKET]
    weekday = date(2026, 5, day).weekday()
    omiya_short = model.NewBoolVar(f"omiya_short_{day}")
    for s, cap in cap_map.items():
        if weekday in cap.closed_dow:
            for e in main_employees:
                model.Add(x[e.name][s] == 0)
            continue
        eco_count = sum(x[e.name][s] for e in eco_employees)
        ticket_count = sum(x[e.name][s] for e in ticket_employees)

        if s == Store.OMIYA and (1 <= day <= 5) is False:
            # NORMAL mode 大宮: eco 2 OR (eco 1 with omiya_short)
            model.Add(eco_count + 2 * omiya_short >= 2)
            model.Add(eco_count >= 1)
            model.Add(eco_count <= 2)
            model.Add(ticket_count >= 1)
        elif s == Store.SUZURAN and (1 <= day <= 5) is False:
            model.Add(eco_count >= 1)
            model.Add(eco_count <= 2)
            model.Add(ticket_count >= 1)
            model.Add(ticket_count <= 2)
            model.Add(eco_count + ticket_count >= 3)
        else:
            model.Add(eco_count >= cap.eco_min)
            model.Add(ticket_count >= cap.ticket_min)
            if s != Store.HIGASHIGUCHI:
                model.Add(eco_count <= cap.eco_max)

    # Omiya anchor
    anchor = sum(x[name][Store.OMIYA] for name in OMIYA_ANCHOR_STAFF
                 if name in [e.name for e in main_employees])
    model.Add(anchor >= 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments = []
        for e in main_employees:
            if solver.Value(off[e.name]) == 1:
                assignments.append(f"{e.name}:×")
            else:
                for s in main_stores:
                    if solver.Value(x[e.name][s]) == 1:
                        assignments.append(f"{e.name}:{s.value}")
                        break
        return ("OK", assignments)
    return (solver.StatusName(status), [])


def main():
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    bad_days = []
    for day in range(1, 32):
        wd = weekday_jp[date(2026, 5, day).weekday()]
        status, assignments = check_day(day)
        if status != "OK":
            bad_days.append(day)
            print(f"❌ 5/{day}({wd}): {status}")
        # else:
        #     print(f"✅ 5/{day}({wd}): OK")

    if bad_days:
        print(f"\n問題のある日: {bad_days}")
        # 詳細表示
        print(f"\n=== 5/{bad_days[0]} の詳細 ===")
        # 該当日の希望状況を表示
        d = bad_days[0]
        print(f"Off requests:")
        for name, days in OFF_REQUESTS.items():
            if d in days:
                print(f"  {name}: OFF")
        print(f"Work requests:")
        for n, dd, s in WORK_REQUESTS:
            if dd == d:
                print(f"  {n}: 出勤希望 ({s.display_name if s else '任意'})")
    else:
        print("\nすべての日が単独では組めます")


if __name__ == "__main__":
    main()
