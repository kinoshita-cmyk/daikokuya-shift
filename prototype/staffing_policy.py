"""生成・検証・条件表示で共有する店舗体制の原則。保存済み月設定は変更しない。"""
from dataclasses import dataclass

from .models import Store, OperationMode


ADVISOR_MAX_DAYS = 3
# 大宮の2名体制回避(400)より軽く、細かな店舗配分より重くする。
ADVISOR_DAY_PENALTY = 300
ADVISOR_DESCRIPTION = (
    "顧問は人員不足や大宮駅前の2名体制を減らすため、月最大3日まで自動配置を許容。"
    "3日出勤させる目標ではなく、不要なら0日。本人の×休み・配置不可・店舗人数は維持。"
    "手動追加後も月3日を超えた場合はERRORで確認する。"
)
SUZURAN_BACKUP_CATEGORY = "長尾不在時のすずらん担当"
SUZURAN_BACKUP_DESCRIPTION = (
    "長尾がすずらん通り店勤務ではない日（休み・他店舗勤務）は、鈴木を必ずすずらん通り店へ配置する。"
    "長尾・鈴木が両方休みの日と休業日は対象外。本人の×休みは変更しない。"
)
DECEMBER_SUPPORT_DESCRIPTION = (
    "2026年12月は大類の単独エコ担当を許容しない。田中のみ、同伴ありでは成立しないと"
    "確認できた場合に限り、すずらん・赤羽駅前で単独エコ担当を許容する。時間切れだけでは緩和しない。"
)


def add_suzuran_backup_constraint(model, x, off, days, operation_modes):
    if not {"長尾", "鈴木"}.issubset(x):
        return
    for day in days:
        if operation_modes.get(day) in (OperationMode.CLOSED, OperationMode.MINIMUM):
            continue
        # 長尾が自店なら制限なし。不在なら鈴木が自店、または両名とも休み。
        model.Add(x["長尾"][day][Store.SUZURAN] + x["鈴木"][day][Store.SUZURAN]
                  + off["長尾"][day] >= 1)
        model.Add(x["長尾"][day][Store.SUZURAN] + x["鈴木"][day][Store.SUZURAN]
                  + off["鈴木"][day] >= 1)


def suzuran_backup_violation(first, second):
    if first is None or second is None:
        return False  # 未入力は人数チェックに任せる。休みと推測しない。
    return (first != Store.SUZURAN and second != Store.SUZURAN
            and not (first == second == Store.OFF))


@dataclass(frozen=True)
class AttendanceEvent:
    id: str = "2026-12-26-year-end-attendance"
    title: str = "忘年会当日の遠方スタッフ出勤優先"
    day: int = 26
    employees: tuple = ("山本", "板倉", "今津", "鈴木", "春山", "土井", "南")

    @property
    def description(self):
        return ("12月26日：" + "・".join(self.employees)
                + "は可能な限り出勤。×休み・勤務日数・連勤上限・必要な店舗体制を優先する任意目標。"
                "山本も当日に限り追加自動配置の対象とするが、月間・連勤上限は変えない。")


ATTENDANCE_EVENT_REWARD = 80


def attendance_events(year, month, *, include_disabled=False):
    from . import rules
    event = AttendanceEvent()
    return ((event,) if (int(year), int(month)) == (2026, 12)
            and (include_disabled or event.id not in rules.DISABLED_STAFFING_EVENTS) else ())


def add_attendance_goals(model, off, x, year, month, off_requests, operation_modes,
                         yamamoto_needed, *, yamamoto_available):
    terms, extra_yamamoto = [], {}
    for event in attendance_events(year, month):
        day = event.day
        if operation_modes.get(day) == OperationMode.CLOSED:
            continue
        for name in event.employees:
            if day in off_requests.get(name, []):
                continue
            if name == "山本":
                if not yamamoto_available:
                    continue
                extra = model.NewBoolVar(f"attendance_yamamoto_{day}")
                model.Add(extra + yamamoto_needed[day] <= 1)
                from .rules import STORE_STAFFING_LIMITS
                model.Add(sum(days[day][Store.AKABANE] for days in x.values()) + 1
                          <= STORE_STAFFING_LIMITS[Store.AKABANE].max_total).OnlyEnforceIf(extra)
                extra_yamamoto[day] = extra
                terms.append(extra + yamamoto_needed[day])
            elif name in off:
                terms.append(1 - off[name][day])
    return terms, extra_yamamoto
