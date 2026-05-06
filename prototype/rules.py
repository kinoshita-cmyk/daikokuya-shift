"""
店舗別の必要人数・ハード制約・特殊ロジック
================================================
データソース:
- /data/rules_2026_05.txt の「■2. 店舗と必要人数」「■3. ハード制約」「■4. 山本特殊ロジック」

このファイルは店舗のルールを定義します。
従業員別のルール（在勤割合）は employees.py 内に各従業員ごとに記載されています。
"""

from dataclasses import dataclass
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


# 通常モードの店舗別キャパシティ
NORMAL_CAPACITY: dict[Store, StoreCapacity] = {
    Store.AKABANE: StoreCapacity(
        eco_min=1,
        ticket_min=2,
        eco_max=1,
        # 例外：エコ1+チケット1の時は山本投入でチケット2に補正
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
        # 例外：全店舗エコ要員不足時はエコ1+チケット1可（→人数少△）
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
        eco_max=2,           # エコ2+チケット1パターンも可
        # エコ1+チケット2 または エコ2+チケット1
    ),
}

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
    "max_consecutive_work_days": 4,        # 最大4連勤
    "max_consecutive_off_days": 2,         # 最大2連休（休み希望日は除く）
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

# すずらん不在時の補填要員（野澤がいない日のチケット担当）
SUZURAN_BACKUP_TICKET: tuple[str, ...] = ("岩野", "大類")


# ============================================================
# 山本の特殊ロジック
# ============================================================

class YamamotoLogic:
    """
    山本さんのシフト決定特殊ルール

    1. 休み希望日 → ×（休み）
    2. それ以外で、その日の赤羽駅前店の構成が
       - エコ1 + チケット0、または
       - エコ1 + チケット1
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
        # 赤羽がエコ1で、チケットが0または1ならば投入
        return akabane_eco_count == 1 and akabane_ticket_count in (0, 1)


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
