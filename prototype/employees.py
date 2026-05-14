"""
従業員マスタ
================================================
2026年5月時点の在籍19名（シフト稼働対象）+ 顧問1名 + 代表1名 を定義します。

データソース:
- /data/rules_2026_05.txt の「■5. 在勤割合」セクション
- 年間基準出勤日数の表（2025年7月〜2026年6月）
- 5月のシフト表（決定版）

注意:
- 専務 → 5月1日付で「非常勤取締役顧問」に役職変更（緊急時のみ稼働）
- 代表（あなた）はシステム管理者で、シフトには入らない
"""

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
            Store.HIGASHIGUCHI: Affinity.WEAK,    # 月1回のみ
            Store.NISHIGUCHI: Affinity.WEAK,      # 不足時補填
            Store.OMIYA: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=280,
        notes="赤羽駅前店店長。赤羽強、月1回東口、西口不足時補填",
    ),
    Employee(
        name="鈴木",
        full_name="鈴木真美",
        employee_id="071",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.AKABANE: Affinity.MEDIUM,
            Store.SUZURAN: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,    # 配置不可
            Store.NISHIGUCHI: Affinity.NONE,      # 配置不可
            Store.OMIYA: Affinity.NONE,
        },
        annual_target_days=268,
        notes="赤羽中、すずらん弱、東口・西口不可",
    ),
    Employee(
        name="楯",
        full_name="楯有史",
        employee_id="024",
        role=Role.MANAGER,
        skill=Skill.ECO,
        home_store=Store.NISHIGUCHI,
        station_type=StationType.FIXED,  # 西口店長
        affinities={
            Store.NISHIGUCHI: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.WEAK,    # 月1回のみ
            Store.AKABANE: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=270,
        notes="大宮西口店店長。西口強、月1回東口（牧野とのペア）",
    ),
    Employee(
        name="牧野",
        full_name="牧野怜偉",
        employee_id="081",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.OMIYA: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.NONE,    # 当面、東口1名体制は不可
            Store.NISHIGUCHI: Affinity.WEAK,      # 月次ルールで研修配置する候補
            Store.AKABANE: Affinity.NONE,
            Store.SUZURAN: Affinity.NONE,
        },
        annual_target_days=268,
        notes="大宮強。東口1名体制は当面不可。月次ルールで西口研修配置の候補",
    ),
    Employee(
        name="春山",
        full_name="春山廣直",
        employee_id="010",
        skill=Skill.ECO,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.OMIYA: Affinity.STRONG,
            Store.HIGASHIGUCHI: Affinity.WEAK,    # 月2回
            Store.SUZURAN: Affinity.WEAK,         # 不足時補填
            Store.AKABANE: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.WEAK,      # 楯不在時・研修時の代替
        },
        annual_target_days=268,
        notes="大宮強、東口2回、すずらん不足時補填。楯不在時・研修時は西口代替あり",
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
        annual_target_days=266,
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
            Store.HIGASHIGUCHI: Affinity.WEAK,    # 月2回
            Store.NISHIGUCHI: Affinity.WEAK,      # 月2回
            Store.AKABANE: Affinity.NONE,
            Store.OMIYA: Affinity.NONE,
        },
        annual_target_days=271,
        notes="大宮すずらん通り店店長。すずらん強、東口2回、西口2回",
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
            Store.SUZURAN: Affinity.WEAK,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=265,  # 新入社員。将来的には265-7=258日に調整予定
        notes="赤羽強、大宮+すずらん合わせて弱。新入社員（将来的には258日に調整予定）",
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
            Store.OMIYA: Affinity.NONE,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=260,
        notes="赤羽中、すずらん中。野澤不在時はすずらんで補填",
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
        annual_target_days=265,
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
            Store.SUZURAN: Affinity.WEAK,
            Store.AKABANE: Affinity.NONE,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        annual_target_days=264,
        notes="大宮強、すずらん弱",
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
        annual_target_days=267,
        notes="すずらん専属。不在時は岩野または大類で補填",
    ),
    Employee(
        name="下田",
        full_name="下田洋也",
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.STRONG,
            Store.OMIYA: Affinity.WEAK,
            Store.NISHIGUCHI: Affinity.WEAK,    # 1日の総人数過剰時の調整先
            Store.AKABANE: Affinity.NONE,
            Store.HIGASHIGUCHI: Affinity.NONE,
        },
        annual_target_days=265,  # 新入社員。将来的には265-7=258日に調整予定
        notes="すずらん強、大宮弱、1日の総人数過剰時は西口へ移動。新入社員（将来的には258日に調整予定）",
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
        only_on_request_days=True,
        constraint_check_excluded=True,
        notes="出勤希望日のみ稼働。制約チェック除外",
    ),
    Employee(
        name="大塚",
        full_name="大塚（要フルネーム確認）",  # ※未確認
        skill=Skill.TICKET,
        station_type=StationType.FLEXIBLE,
        affinities={
            Store.SUZURAN: Affinity.MEDIUM,
            Store.OMIYA: Affinity.MEDIUM,
            Store.AKABANE: Affinity.MEDIUM,
            Store.HIGASHIGUCHI: Affinity.NONE,
            Store.NISHIGUCHI: Affinity.NONE,
        },
        employment_status=EmploymentStatus.PART_TIME,  # パート・アルバイト
        constraint_check_excluded=True,
        notes="パート・アルバイト。年間基準出勤日数は定めなし。最大4連勤チェックのみ適用",
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
