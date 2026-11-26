"""承認された日付限定の配置目標。通常の最低人数・本人希望は緩めない。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import OperationMode, Store


@dataclass(frozen=True)
class StaffingEvent:
    id: str
    year: int
    month: int
    day: int
    title: str
    store: Store
    total: int
    required: tuple[str, ...]
    candidates: tuple[str, ...]

    @property
    def description(self):
        return (
            f"{self.month}月{self.day}日・{self.store.display_name}を、"
            f"{'・'.join(self.required)}を含む合計{self.total}名体制にし、"
            f"残りのメンバーに{'または'.join(self.candidates)}を1名以上配置する目標。"
            "本人の×休み・勤務上限・他店舗の必要体制を優先し、難しければ未達を許容。"
            "山本の追加自動配置はこの目標が成立する当日だけ。"
        )


APPROVED_STAFFING_EVENTS = (
    StaffingEvent("2026-11-05-akabane-jewelry", 2026, 11, 5, "宝飾展に伴う赤羽4名体制",
                  Store.AKABANE, 4, ("鈴木", "山本"), ("田中", "大類")),
)
# 店舗不足・エコ主力の飛び石等より弱い、任意のまとまった目標。
STAFFING_EVENT_REWARD = 80
STAFFING_EVENT_CATEGORY = "月限定の配置目標"


def staffing_events(year, month, *, include_disabled=False):
    from . import rules
    return tuple(event for event in APPROVED_STAFFING_EVENTS
                 if (event.year, event.month) == (int(year), int(month))
                 and (include_disabled or event.id not in rules.DISABLED_STAFFING_EVENTS))


def event_workers(shift, event):
    return {a.employee for a in shift.get_day_assignments(event.day) if a.store == event.store}


def event_satisfied(shift, event):
    names = event_workers(shift, event)
    return (len(names) == event.total and set(event.required) <= names
            and bool(set(event.candidates) & names))


def add_staffing_event_goals(model, x, year, month, off_requests, operation_modes, *, yamamoto_available):
    """全条件が揃う場合だけ評価。目標を見送れば追加の勤務は発生しない。"""
    from .rules import STORE_STAFFING_LIMITS

    goals, extra_yamamoto = {}, {}
    for event in staffing_events(year, month):
        goal = model.NewBoolVar("event_" + event.id)
        goals[event.id] = goal
        available = (yamamoto_available and event.day not in off_requests.get("山本", [])
                     and operation_modes.get(event.day, OperationMode.NORMAL) == OperationMode.NORMAL
                     and event.total <= STORE_STAFFING_LIMITS[event.store].max_total
                     and all(name in x for name in event.required if name != "山本")
                     and any(name in x for name in event.candidates))
        if not available:
            model.Add(goal == 0)
            continue
        for name in event.required:
            if name != "山本":
                model.Add(x[name][event.day][event.store] == 1).OnlyEnforceIf(goal)
        model.Add(sum(x[name][event.day][event.store] for name in event.candidates if name in x) >= 1).OnlyEnforceIf(goal)
        model.Add(sum(days[event.day][event.store] for days in x.values()) == event.total - 1).OnlyEnforceIf(goal)
        extra_yamamoto[event.day] = goal
    return goals, extra_yamamoto
