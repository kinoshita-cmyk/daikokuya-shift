"""
最小限の制約から段階的に追加するデバッグ
================================================
"""

from calendar import monthrange
from datetime import date
from ortools.sat.python import cp_model

from .models import Store, Skill, OperationMode, Affinity
from .employees import ALL_EMPLOYEES, ECO_STAFF, TICKET_STAFF, get_employee, shift_active_employees
from .rules import NORMAL_CAPACITY, REDUCED_CAPACITY, OMIYA_ANCHOR_STAFF
from .may_2026_data import OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER


def test_with_constraints(stage: str, constraint_flags: dict) -> bool:
    """指定された制約のみで解こうとする"""
    print(f"\n=== {stage} ===")

    main_employees = [e for e in shift_active_employees() if not e.is_auxiliary]
    days = list(range(1, 32))
    main_stores = [Store.AKABANE, Store.HIGASHIGUCHI, Store.OMIYA, Store.NISHIGUCHI, Store.SUZURAN]

    model = cp_model.CpModel()
    x = {}
    off = {}
    for e in main_employees:
        x[e.name] = {d: {s: model.NewBoolVar(f"x_{e.name}_{d}_{s.name}")
                         for s in main_stores} for d in days}
        off[e.name] = {d: model.NewBoolVar(f"off_{e.name}_{d}") for d in days}

    # 必須: 排他制約
    for e in main_employees:
        for d in days:
            model.Add(sum(x[e.name][d][s] for s in main_stores) + off[e.name][d] == 1)

    # 1. Off requests
    if constraint_flags.get("off_requests", False):
        for emp_name, off_days in OFF_REQUESTS.items():
            if emp_name not in [e.name for e in main_employees]:
                continue
            for d in off_days:
                model.Add(off[emp_name][d] == 1)

    # 2. Affinity NONE
    if constraint_flags.get("affinity_none", False):
        for e in main_employees:
            for s in main_stores:
                if e.affinities.get(s) == Affinity.NONE:
                    for d in days:
                        model.Add(x[e.name][d][s] == 0)

    # 3. 南 only on request days
    if constraint_flags.get("minami_only_request", False):
        if any(e.name == "南" for e in main_employees):
            minami_work_days = set(d for n, d, _ in WORK_REQUESTS if n == "南")
            for d in days:
                if d not in minami_work_days:
                    model.Add(off["南"][d] == 1)

    # 4. 大塚 = 10 days
    if constraint_flags.get("otsuka_10days", False):
        if any(e.name == "大塚" for e in main_employees):
            model.Add(sum(1 - off["大塚"][d] for d in days) == 10)

    # 5. Store capacity (柔軟版: 大宮/すずらんは複数構成許容)
    if constraint_flags.get("capacity", False):
        eco_employees = [e for e in main_employees if e.skill == Skill.ECO]
        ticket_employees = [e for e in main_employees if e.skill == Skill.TICKET]
        omiya_short = {d: model.NewBoolVar(f"oshort_{d}") for d in days}
        for d in days:
            cap_map = REDUCED_CAPACITY if 1 <= d <= 5 else NORMAL_CAPACITY
            is_normal = not (1 <= d <= 5)
            for s, cap in cap_map.items():
                weekday = date(2026, 5, d).weekday()
                if weekday in cap.closed_dow:
                    for e in main_employees:
                        model.Add(x[e.name][d][s] == 0)
                    continue
                eco_count = sum(x[e.name][d][s] for e in eco_employees)
                ticket_count = sum(x[e.name][d][s] for e in ticket_employees)
                if s == Store.OMIYA and is_normal:
                    model.Add(eco_count + 2 * omiya_short[d] >= 2)
                    model.Add(eco_count >= 1)
                    model.Add(eco_count <= 2)
                    model.Add(ticket_count >= 1)
                elif s == Store.SUZURAN and is_normal:
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

    # 6. Omiya anchor
    if constraint_flags.get("omiya_anchor", False):
        for d in days:
            anchor = sum(x[name][d][Store.OMIYA] for name in OMIYA_ANCHOR_STAFF
                         if name in [e.name for e in main_employees])
            model.Add(anchor >= 1)

    # 7. Holiday days minimum
    if constraint_flags.get("holiday_days", False):
        for e in main_employees:
            if e.constraint_check_excluded:
                continue
            model.Add(sum(off[e.name][d] for d in days) >= 8)

    # ソルバー実行
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30
    status = solver.Solve(model)
    name = solver.StatusName(status)
    print(f"  → status: {name}")
    return status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def main():
    # 最小限から始めて段階的に追加
    test_with_constraints("Stage 1: 排他のみ", {})

    test_with_constraints("Stage 2: + 休み希望", {
        "off_requests": True,
    })

    test_with_constraints("Stage 3: + 配置不可", {
        "off_requests": True, "affinity_none": True,
    })

    test_with_constraints("Stage 4: + 南/大塚特殊", {
        "off_requests": True, "affinity_none": True,
        "minami_only_request": True, "otsuka_10days": True,
    })

    test_with_constraints("Stage 5: + 店舗キャパ", {
        "off_requests": True, "affinity_none": True,
        "minami_only_request": True, "otsuka_10days": True,
        "capacity": True,
    })

    test_with_constraints("Stage 6: + 大宮アンカー", {
        "off_requests": True, "affinity_none": True,
        "minami_only_request": True, "otsuka_10days": True,
        "capacity": True, "omiya_anchor": True,
    })

    test_with_constraints("Stage 7: + 休日日数最低", {
        "off_requests": True, "affinity_none": True,
        "minami_only_request": True, "otsuka_10days": True,
        "capacity": True, "omiya_anchor": True, "holiday_days": True,
    })


if __name__ == "__main__":
    main()
