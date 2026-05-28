"""
従業員マスタ
================================================
2026年5月時点の在籍19名（シフト稼働対象）+ 顧問1名 + 代表1名 を定義します。

データソース:
- config/employees.json の現在ルール
- /data/rules_2026_05.txt は初期資料として保管（最新チャット・設定を優先）
- 年間基準出勤日数の表（2025年7月〜2026年6月）
- 5月のシフト表（決定版）

注意:
- 通常運用では config/employees.json を優先。このファイルは設定JSONが読めない時の予備初期値
- 専務 → 5月1日付で「非常勤取締役顧問」に役職変更（緊急時のみ稼働）
- 代表（あなた）はシステム管理者で、シフトには入らない
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from .models import (
    Employee, Skill, Role, Store, StationType, Affinity, EmploymentStatus,
)

# ============================================================
# エコ担当（エコ・チケット両方対応可）8名
# ============================================================

ECO_STAFF: list[Employee] = [
    Employee(
        name="今津",
        full_name="今津悠貴",
        employee_id="076",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.AKABANE,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.MEDIUM,
            Store.NISHIGUCHI: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.OMIYA: Affinity.WEAK,
        },
        annual_target_days=280,
        notes="主担当: 赤羽。通常対応可: 東口・西口・すずらん。応援・巡回可: 大宮。絶対配置不可なし。赤羽の割合を高く。",
    ),
    Employee(
        name="鈴木",
        full_name="鈴木真美",
        employee_id="071",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.NONE,    # 配置不可
            Store.NISHIGUCHI: Affinity.NONE,      # 配置不可
            Store.OMIYA: Affinity.WEAK,
        },
        annual_target_days=267,
        notes="通常対応可: 赤羽・すずらん。応援・巡回可: 大宮。絶対配置不可: 東口・西口。",
    ),
    Employee(
        name="楯",
        full_name="楯有史",
        employee_id="024",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.NISHIGUCHI,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.NISHIGUCHI: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.MEDIUM,
            Store.AKABANE: Affinity.WEAK,
            Store.OMIYA: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=270,
        notes="主担当: 西口。通常対応可: 東口。応援・巡回可: 赤羽。配置不可なし。",
    ),
    Employee(
        name="牧野",
        full_name="牧野怜偉",
        employee_id="081",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.STRONG,
            Store.AKABANE: Affinity.MEDIUM,
            Store.OMIYA: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.NONE,    # 当面、東口1名体制は不可
            Store.NISHIGUCHI: Affinity.NONE,      # 通常不可。研修は手動・月別例外で扱う
        },
        annual_target_days=268,
        notes="主担当: すずらん。通常対応可: 赤羽・大宮。絶対配置不可: 東口・西口。月内最低巡回: 赤羽2回以上・すずらん2回以上。",
    ),
    Employee(
        name="春山",
        full_name="春山廣直",
        employee_id="010",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.OMIYA: Affinity.STRONG,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.WEAK,
            Store.NISHIGUCHI: Affinity.WEAK,
            Store.AKABANE: Affinity.WEAK,
        },
        annual_target_days=268,
        notes="主担当: 大宮。通常対応可: すずらん。応援・巡回可: 西口・東口・赤羽。配置不可なし。",
    ),
    Employee(
        name="下地",
        full_name="下地里美",
        employee_id="006",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.OMIYA,
        station_type=StationType.FIXED,
        affinities={
            Store.OMIYA: Affinity.STRONG,
            Store.AKABANE: Affinity.NONE,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=265,
        notes="大宮駅前店店長。大宮専属",
    ),
    Employee(
        name="長尾",
        full_name="長尾暁洋",
        employee_id="011",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.SUZURAN,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.WEAK,
            Store.NISHIGUCHI: Affinity.WEAK,
            Store.OMIYA: Affinity.WEAK,
            Store.AKABANE: Affinity.NONE,
        },
        annual_target_days=271,
        notes="主担当: すずらん。応援・巡回可: 東口・西口・大宮。配置不可なし。",
    ),
    Employee(
        name="土井",
        full_name="土井克彦",
        employee_id="005",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.HIGASHIGUCHI,
        station_type=StationType.FIXED,
        affinities={
            Store.HIGASHIGUCHI: Affinity.STRONG,
            Store.AKABANE: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=259,
        notes="赤羽東口店店長。東口専属",
    ),
]


# ============================================================
# チケット担当（チケットのみ）10名
# ============================================================

TICKET_STAFF: list[Employee] = [
    Employee(
        name="板倉",
        full_name="板倉七重",
        employee_id="049",
        skill=Skill.TICKET,
        home_store=Store.AKABANE,
        station_type=StationType.FIXED,
        affinities={
            Store.AKABANE: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=273,
        notes="赤羽専属",
    ),
    Employee(
        name="田中",
        full_name="田中美紅",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.STRONG,
            Store.OMIYA: Affinity.WEAK,           # 大宮+すずらん合わせて弱
            Store.SUZURAN: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=265,
        notes="主担当: 赤羽。通常対応可: すずらん。応援・巡回可: 大宮。絶対配置不可: 東口・西口。",
    ),
    Employee(
        name="岩野",
        full_name="岩野衣里",
        employee_id="077",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.OMIYA: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=260,
        notes="通常対応可: 赤羽・すずらん。応援・巡回可: 大宮。絶対配置不可: 東口・西口。",
    ),
    Employee(
        name="大類",
        full_name="大類麻梨亜",
        employee_id="079",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.MEDIUM,
            Store.OMIYA: Affinity.MEDIUM,
            Store.AKABANE: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=264,
        notes="すずらん中、大宮中、赤羽弱。野澤不在時はすずらんで補填",
    ),
    Employee(
        name="黒澤",
        full_name="黒澤彩夏",
        employee_id="067",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.OMIYA: Affinity.STRONG,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.AKABANE: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=260,
        notes="主担当: 大宮。通常対応可: すずらん。応援・巡回可: 赤羽。絶対配置不可: 東口・西口。月内最低巡回: すずらん5回以上。",
    ),
    Employee(
        name="野澤",
        full_name="野澤絵美",
        employee_id="055",  # ※画像の読取り。要確認
        skill=Skill.TICKET,
        home_store=Store.SUZURAN,
        station_type=StationType.FIXED,
        affinities={
            Store.SUZURAN: Affinity.STRONG,
            Store.AKABANE: Affinity.NONE,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=266,
        notes="すずらん専属。不在時は岩野または大類で補填",
    ),
    Employee(
        name="下田",
        full_name="下田洋也",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.STRONG,
            Store.OMIYA: Affinity.MEDIUM,
            Store.NISHIGUCHI: Affinity.NONE,
            Store.AKABANE: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
        },
        annual_target_days=265,
        notes="主担当: すずらん。通常対応可: 大宮。応援・巡回可: 赤羽。絶対配置不可: 東口・西口。月内最低巡回: 赤羽1回以上・大宮1回以上",
    ),
    Employee(
        name="南",
        full_name="南（要フルネーム確認）",  # ※未確認
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.MEDIUM,
            Store.OMIYA: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        employment_status=EmploymentStatus.PART_TIME,
        only_on_request_days=True,
        constraint_check_excluded=True,
        notes="パート。出勤希望日のみ稼働。制約チェック除外",
    ),
    Employee(
        name="大塚",
        full_name="大塚（要フルネーム確認）",  # ※未確認
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.MEDIUM,
            Store.OMIYA: Affinity.MEDIUM,
            Store.AKABANE: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        employment_status=EmploymentStatus.PART_TIME,  # パート・アルバイト
        constraint_check_excluded=True,
        notes="パート・アルバイト。通常対応可: 大宮・すずらん。応援・巡回可: 赤羽。絶対配置不可: 東口・西口。年間基準出勤日数は定めなし。最大4連勤チェックのみ適用。",
    ),
    Employee(
        name="山本",
        full_name="山本（要フルネーム確認）",  # ※未確認
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            # 山本は通常の在勤割合ロジックではなく、特殊ロジック（rules.py参照）
            Store.AKABANE: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        employment_status=EmploymentStatus.AUXILIARY,  # 補助要員枠
        constraint_check_excluded=True,
        is_auxiliary=True,
        notes="70代後半・今年退職予定の特別枠。年間基準出勤日数は定めなし。"
              "赤羽がエコ1+チケット0または1の日に補助投入される。カウント対象外",
    ),
]


# ============================================================
# 役員（顧問・代表）
# ============================================================

EXECUTIVES: list[Employee] = [
    Employee(
        name="顧問",  # 5/1以降の役職に合わせて変更（シフト表の旧表示は「専務」）
        full_name="木下俊泰",
        role=Role.ADVISOR,
        skill=Skill.ECO,  # 過去は実働しているため両対応
        station_type=StationType.FLEXIBLE,
        employment_status=EmploymentStatus.ADVISOR,
        notes="2026年5月1日付で『非常勤取締役顧問』に役職変更（先代代表取締役）。"
              "原則シフト不参加、緊急時のみ稼働。データ上は残す。"
              "5月までのシフト表では『専務』列として表示されている",
    ),
    # 代表取締役（あなた＝木下昌英）はシステム管理者として別扱い。シフトには入らない。
]


# ============================================================
# 全従業員リスト（シフト稼働対象）
# ============================================================

ALL_EMPLOYEES: list[Employee] = ECO_STAFF + TICKET_STAFF + EXECUTIVES


def get_employee(name: str) -> Employee:
    """
    名前で従業員を取得（employee_config の動的設定を優先）
    """
    # employee_config の動的設定（JSONファイル）を優先
    try:
        from .employee_config import get_all_employees_including_retired
        for e in get_all_employees_including_retired():
            if e.name == name:
                return e
    except ImportError:
        pass
    # フォールバック: employees.py のハードコード値
    for e in ALL_EMPLOYEES:
        if e.name == name:
            return e
    raise KeyError(f"従業員が見つかりません: {name}")


def _parse_iso_date(value: str | None) -> date | None:
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
    employee: Employee,
    target_year: int,
    target_month: int,
    target_day: int | None = None,
) -> bool:
    """
    入社日から2か月間の試用期間かどうか。

    試用期間者は希望提出は受けるが、自動生成・通常検証の人数対象には含めない。
    月単位の生成では、対象月に試用期間が1日でも重なれば対象外として扱う。
    """
    hired_on = _parse_iso_date(employee.hired_at)
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


def shift_active_employees() -> list[Employee]:
    """
    通常のシフト生成対象（employee_config 経由）。
    退職者・顧問・休職中は自動除外。
    """
    try:
        from .employee_config import get_all_employees_including_retired
        all_emps = get_all_employees_including_retired()
        # 在籍 or パートのみ（顧問・退職・休職・補助は除外）
        return [
            e for e in all_emps
            if e.employment_status in (EmploymentStatus.ACTIVE, EmploymentStatus.PART_TIME)
            and e.role != Role.ADVISOR
            and not e.is_auxiliary
        ]
    except ImportError:
        # フォールバック
        return [e for e in ALL_EMPLOYEES if e.role != Role.ADVISOR]


if __name__ == "__main__":
    # 動作確認
    print(f"エコ担当: {len(ECO_STAFF)}名")
    for e in ECO_STAFF:
        print(f"  - {e.name}（{e.full_name}）")
    print(f"\nチケット担当: {len(TICKET_STAFF)}名")
    for e in TICKET_STAFF:
        print(f"  - {e.name}（{e.full_name}）")
    print(f"\n役員: {len(EXECUTIVES)}名")
    for e in EXECUTIVES:
        print(f"  - {e.name}（{e.role.value}）")
    print(f"\n合計: {len(ALL_EMPLOYEES)}名（うちシフト稼働対象: {len(shift_active_employees())}名）")
