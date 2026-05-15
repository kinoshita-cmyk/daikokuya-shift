"""
店舗別の必要人数・ハード制約・特殊ロジック
================================================
データソース:
- /data/rules_2026_05.txt の「■2. 店舗と必要人数」「■3. ハード制約」「■4. 山本特殊ロジック」

このファイルは店舗のルールを定義します。
従業員別のルール（在勤割合）は employees.py 内に各従業員ごとに記載されています。
"""

from dataclasses import dataclass
from typing import Optional
from .models import Store, Skill, OperationMode

# ============================================================
# 店舗別の必要人数（営業モードごと）
# ============================================================

@dataclass
class StoreCapacity:
    """店舗の1日の必要人数（モードごとに変動）"""
    eco_min: int            # エコ要員の最小数
    ticket_min: int         # チケット要員の最小数
    eco_max: int = 1        # エコ要員の最大数（通常1、一部大型店のみ2）
    closed_dow: tuple[int, ...] = ()  # 休店曜日（0=月）。tuple()=休店なし


@dataclass(frozen=True)
class StoreStaffingLimit:
    """店舗ごとの標準人数と最大人数。"""
    standard_total: int          # 通常時に目指す人数
    max_total: int               # 生成で超えない最大人数
    over_standard_penalty: int   # 標準人数を1人超えるごとの回避ペナルティ


@dataclass(frozen=True)
class DailyStaffingLimit:
    """1日全体の標準人数と最大人数。"""
    standard_total: int          # 通常時に目指す総人数
    max_total: int               # 生成で超えない最大人数
    over_standard_penalty: int   # 標準人数を1人超えるごとの回避ペナルティ


# 通常モードの店舗別キャパシティ
NORMAL_CAPACITY: dict[Store, StoreCapacity] = {
    Store.AKABANE: StoreCapacity(
        eco_min=1,
        ticket_min=2,
        eco_max=2,
        # 基本：エコ1+チケット2
        # 例外：エコ2+チケット1、またはチケット対応が1名分のみの時は山本投入
    ),
    Store.HIGASHIGUCHI: StoreCapacity(
        eco_min=1,
        ticket_min=0,
        eco_max=1,           # 原則1名体制（土井メイン、休みの日は他エコが代替）
        closed_dow=(0,),     # 月曜は休店
    ),
    Store.OMIYA: StoreCapacity(
        eco_min=2,
        ticket_min=1,
        eco_max=2,
        # エコ担当はチケット対応も可。生成ではエコ対応1名以上+合計3名を基本にする。
        # 例外：人員不足時は2名体制可（→人数少△）
    ),
    Store.NISHIGUCHI: StoreCapacity(
        eco_min=1,
        ticket_min=0,
        eco_max=1,
        # 原則1名体制（楯メイン）
        # 人数が余る日・研修日・チケット補助が必要な日は追加配置の調整先にする
    ),
    Store.SUZURAN: StoreCapacity(
        eco_min=1,
        ticket_min=2,
        eco_max=2,           # エコ2名体制も可。チケットは原則2名。
        # エコ担当はチケット対応も可。合計3名以上を基本にする。
    ),
}


# 店舗ごとの標準人数・最大人数。
# 月間目標勤務日数よりも、まず店舗ごとの上限を守る。
STORE_STAFFING_LIMITS: dict[Store, StoreStaffingLimit] = {
    # 5名は原則NG。4名は例外的な増員として許容。
    Store.AKABANE: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=900),
    # 赤羽東口店は原則1名のみ。
    Store.HIGASHIGUCHI: StoreStaffingLimit(standard_total=1, max_total=1, over_standard_penalty=3000),
    # 大宮駅前店は3名を標準にし、4名は強く抑制。5名はNG。
    Store.OMIYA: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=2400),
    # 大宮西口店は原則1名、研修などで2名まで。
    Store.NISHIGUCHI: StoreStaffingLimit(standard_total=1, max_total=2, over_standard_penalty=900),
    # すずらんは3名標準、状況により4名まで。
    Store.SUZURAN: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=900),
}

# 1日全体の人数上限。
# 通常は11人体制。13人までを通常の許容範囲、14人は過去実績上の例外として扱う。
# 15人以上は受け入れ不可。
GLOBAL_DAILY_STAFFING_LIMIT = DailyStaffingLimit(
    standard_total=11,
    max_total=14,
    over_standard_penalty=900,
)


# 月別の目標出勤日数（2025年7月〜2026年6月）。
# 従来の「年間日数÷12」ではなく、管理側の月別表を優先する。
# MAX行は暦日ではなく、12/31〜1/2の三が日休業を抜いた営業上の上限。
MONTHLY_TARGET_MONTH_ORDER: tuple[int, ...] = (7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6)
MONTHLY_MAX_WORK_DAYS: dict[int, int] = {
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 30,
    1: 29, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
}
MONTHLY_WORK_TARGETS: dict[str, dict[int, int]] = {
    "今津": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 23, 24, 23, 23, 22, 23, 24, 23, 24, 23))),
    "板倉": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 22, 24, 22, 22, 22, 21, 24, 22, 24, 22))),
    "長尾": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 22, 24, 22, 22, 21, 20, 24, 22, 24, 22))),
    "楯": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 22, 24, 22, 21, 21, 20, 24, 22, 24, 22))),
    "春山": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 21, 24, 21, 22, 21, 21, 24, 21, 24, 21))),
    "牧野": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 21, 24, 21, 22, 21, 21, 24, 21, 24, 21))),
    "鈴木": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (24, 24, 21, 24, 21, 22, 21, 20, 24, 21, 24, 21))),
    "野澤": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 22, 23, 22, 22, 21, 20, 23, 23, 23, 22))),
    "下地": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 22, 23, 21, 22, 21, 20, 23, 23, 23, 22))),
    "田中": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 22, 23, 21, 22, 21, 20, 23, 23, 23, 22))),
    "下田": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 22, 23, 21, 22, 21, 20, 23, 23, 23, 22))),
    "大類": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 22, 23, 22, 21, 21, 20, 23, 23, 23, 22))),
    "黒澤": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 21, 23, 21, 21, 20, 20, 23, 23, 23, 21))),
    "岩野": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 21, 23, 21, 21, 20, 20, 23, 23, 23, 21))),
    "土井": dict(zip(MONTHLY_TARGET_MONTH_ORDER, (23, 23, 21, 23, 21, 20, 20, 20, 23, 23, 23, 21))),
}
EMPLOYEE_TARGET_NAME_ALIASES: dict[str, str] = {
    "今津悠貴": "今津",
    "板倉七重": "板倉",
    "長尾暁洋": "長尾",
    "楯有史": "楯",
    "春山廣植": "春山",
    "春山廣直": "春山",
    "牧野怜偉": "牧野",
    "鈴木真美": "鈴木",
    "野澤絵美": "野澤",
    "下地里美": "下地",
    "田中美紅": "田中",
    "下田洋也": "下田",
    "大類麻梨亜": "大類",
    "黒澤彩夏": "黒澤",
    "岩野衣里": "岩野",
    "土井克彦": "土井",
}


def get_monthly_work_target(
    employee_name: str,
    month: int,
    annual_target_days: Optional[int] = None,
) -> Optional[int]:
    """従業員の月別目標出勤日数を返す。未登録者は年間日数からの従来計算に戻す。"""
    key = EMPLOYEE_TARGET_NAME_ALIASES.get(str(employee_name), str(employee_name))
    monthly_targets = MONTHLY_WORK_TARGETS.get(key)
    if monthly_targets is not None and int(month) in monthly_targets:
        return int(monthly_targets[int(month)])
    if annual_target_days is None:
        return None
    return round(int(annual_target_days) / 12)

# 省人員モード（GW・お盆・SW等）
REDUCED_CAPACITY: dict[Store, StoreCapacity] = {
    Store.AKABANE: StoreCapacity(eco_min=1, ticket_min=1),
    Store.HIGASHIGUCHI: StoreCapacity(eco_min=1, ticket_min=0, closed_dow=(0,)),
    Store.OMIYA: StoreCapacity(eco_min=1, ticket_min=1),
    Store.NISHIGUCHI: StoreCapacity(eco_min=1, ticket_min=0),
    Store.SUZURAN: StoreCapacity(eco_min=1, ticket_min=1),
}

# 最小営業モード（赤羽駅前店・大宮駅前店のみ）
MINIMUM_CAPACITY: dict[Store, StoreCapacity] = {
    Store.AKABANE: StoreCapacity(eco_min=1, ticket_min=1),
    Store.OMIYA: StoreCapacity(eco_min=1, ticket_min=1),
    # 他の3店舗は閉店扱い
}


def get_capacity(mode: OperationMode) -> dict[Store, StoreCapacity]:
    """営業モードに対応するキャパシティを返す"""
    return {
        OperationMode.NORMAL: NORMAL_CAPACITY,
        OperationMode.REDUCED: REDUCED_CAPACITY,
        OperationMode.MINIMUM: MINIMUM_CAPACITY,
        OperationMode.CLOSED: {},
    }[mode]


# ============================================================
# ハード制約（絶対条件）
# ============================================================

# 全体ルール
HARD_CONSTRAINTS = {
    "max_consecutive_work_days": 4,        # 原則最大4連勤（例外的に5連勤あり）
    "max_consecutive_off_days": 2,         # 原則最大2連休（希望や人数過多で3連休もあり得る）
    "min_two_day_off_per_month": 1,        # 2連休を月1回以上
    "max_two_day_off_per_month": 2,        # 2連休は最大2回
    "no_eco_zero_at_any_store": True,      # エコ0NG（全店舗）
    "higashiguchi_eco_required": True,     # 東口は必ずエコ1名
    "nishiguchi_eco_required": True,       # 西口は必ずエコ1名
    "no_ticket_zero_at": [                 # チケット0NG店舗
        Store.AKABANE, Store.OMIYA, Store.SUZURAN
    ],
}

# 大宮店の追加制約：春山・下地どちらか1人は必ず在勤
OMIYA_ANCHOR_STAFF: tuple[str, ...] = ("春山", "下地")

# 赤羽東口店: 土井メイン。土井休みの日だけ指定エコスタッフが代替。
HIGASHIGUCHI_PRIMARY_STAFF = "土井"
HIGASHIGUCHI_SUBSTITUTE_STAFF: tuple[str, ...] = ("楯", "春山", "長尾", "今津")
HIGASHIGUCHI_ALLOWED_STAFF: tuple[str, ...] = (
    HIGASHIGUCHI_PRIMARY_STAFF,
    *HIGASHIGUCHI_SUBSTITUTE_STAFF,
)

# 牧野さんの研修ルール。
# 赤羽東口店・大宮西口店の単独勤務は当面NG。
# 大宮西口店は月別ルールで研修を明示した月に限り、楯君の同時配置で許可。
MAKINO_SOLO_NG_STORES: tuple[Store, ...] = (
    Store.HIGASHIGUCHI,
    Store.NISHIGUCHI,
)
MAKINO_NISHIGUCHI_TRAINING_PARTNER = "楯"

# メイン店舗以外への月内勤務必須回数。
# 本人の休み希望は最優先したうえで、生成できる解では必ず満たす。
OFF_MAIN_STORE_MINIMUMS: dict[str, tuple[Store, int]] = {
    "今津": (Store.AKABANE, 3),
    "楯": (Store.NISHIGUCHI, 3),
    "春山": (Store.OMIYA, 3),
    "長尾": (Store.SUZURAN, 3),
}

# 固定しすぎを避けるための標準巡回ルール。
# 固定店長・専属者を除き、実績で許容されている店舗へ月に数回は回す。
STORE_ROTATION_MINIMUMS: dict[str, list[tuple[tuple[Store, ...], int]]] = {
    "今津": [((Store.HIGASHIGUCHI,), 1), ((Store.SUZURAN,), 1)],
    "黒澤": [((Store.SUZURAN,), 2)],
    "牧野": [((Store.AKABANE, Store.SUZURAN), 3)],
    "長尾": [((Store.HIGASHIGUCHI,), 1), ((Store.NISHIGUCHI,), 1)],
    "下田": [((Store.AKABANE,), 1), ((Store.OMIYA,), 1)],
}

# すずらん不在時の補填要員（野澤がいない日のチケット担当）
SUZURAN_BACKUP_TICKET: tuple[str, ...] = ("岩野", "大類")

# 店舗の鍵を開け閉めできるメンバー。
# 現時点では生成のハード条件にはせず、検証・画面表示の警告として扱う。
STORE_KEYHOLDERS: dict[Store, tuple[str, ...]] = {
    Store.AKABANE: ("山本", "板倉", "今津", "鈴木", "春山", "長尾", "楯"),
    Store.HIGASHIGUCHI: ("土井", "春山", "長尾", "楯", "今津"),
    Store.OMIYA: ("下地", "春山"),
    Store.SUZURAN: ("長尾", "野澤", "春山", "今津"),
    Store.NISHIGUCHI: ("楯", "春山", "長尾", "今津"),
}
SUZURAN_KEY_SUPPORT_FROM_OMIYA: tuple[str, ...] = ("下地", "春山")


# ============================================================
# 山本の特殊ロジック
# ============================================================

class YamamotoLogic:
    """
    山本さんのシフト決定特殊ルール

    1. 休み希望日 → ×（休み）
    2. それ以外で、その日の赤羽駅前店の構成が
       - エコ1 + チケット1、または
       - エコ2のみ（チケット対応が1名分のみ）
       の場合 → 山本を○（赤羽駅前店）で投入
    3. それ以外 → 空白（出勤しない、勤務日数にカウントしない）

    特徴：
    - 通常の連勤・休日日数チェックの対象外
    - シフト総人数の集計対象外（補助要員）
    """
    EMPLOYEE_NAME = "山本"
    BACKUP_STORE = Store.AKABANE

    @staticmethod
    def should_deploy(
        akabane_eco_count: int,
        akabane_ticket_count: int,
        is_off_request: bool,
    ) -> bool:
        """山本を赤羽に投入すべきかを判定"""
        if is_off_request:
            return False
        # 赤羽は「エコ1名 + チケット2名」が基本。
        # エコが2名いる日は、エコ1名分をチケット対応として扱える。
        # ただしチケット対応が2名分に満たない場合は、山本さんを補助投入する。
        effective_ticket_coverage = (
            akabane_ticket_count + max(0, akabane_eco_count - 1)
        )
        return (
            akabane_eco_count >= 1
            and effective_ticket_coverage < 2
        )


# ============================================================
# 月別の休日日数ルール（5月例）
# ============================================================

# 基本休日日数（その月の休日日数下限）
DEFAULT_HOLIDAY_DAYS_MAY = 8

# 個別の休日日数指定（5月の場合）
MAY_2026_HOLIDAY_OVERRIDES: dict[str, int] = {
    "今津": 9,
    "鈴木": 9,
    "岩野": 9,
    "下地": 9,
    "楯": 9,
    "土井": 10,
    "長尾": 11,
}


# ============================================================
# 制約チェック対象外の従業員
# ============================================================

# 全制約チェック対象外（特例運用）
CONSTRAINT_EXCLUDED: tuple[str, ...] = (
    "山本",   # 補助要員ロジックのため
    "南",     # 出勤希望日のみ
    "大塚",   # 5月は10日間在勤のためチェック除外（ただし最大4連勤は適用）
)

# 4連勤チェックは適用される（一部の制約のみ免除）
CONSEC_WORK_CHECK_APPLIES: tuple[str, ...] = ("大塚",)


# ============================================================
# 確認・動作テスト
# ============================================================

if __name__ == "__main__":
    print("=== 通常モードの店舗別必要人数 ===")
    for store, cap in NORMAL_CAPACITY.items():
        closed = f"  休店曜日: {cap.closed_dow}" if cap.closed_dow else ""
        print(f"  {store.display_name}: エコ{cap.eco_min}〜{cap.eco_max} + チケット{cap.ticket_min}{closed}")

    total_min = sum(c.eco_min + c.ticket_min for c in NORMAL_CAPACITY.values())
    print(f"\n  合計最小人数: {total_min}名/日（仕様書通り11名）")

    print("\n=== ハード制約 ===")
    for k, v in HARD_CONSTRAINTS.items():
        print(f"  {k}: {v}")

    print("\n=== 5月の個別休日日数 ===")
    for name, days in MAY_2026_HOLIDAY_OVERRIDES.items():
        print(f"  {name}: {days}日休")
