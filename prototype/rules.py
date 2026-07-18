"""
店舗別の必要人数・ハード制約・特殊ロジック
================================================
データソース:
- /data/rules_2026_05.txt の「■2. 店舗と必要人数」「■3. ハード制約」「■4. 山本特殊ロジック」

このファイルは店舗のルールを定義します。
従業員別のルール（在勤割合）は employees.py 内に各従業員ごとに記載されています。

月限定の例外（大宮アンカー緩和・境界連勤延長など）は
config/monthly_exceptions.json から読み込みます。コード内の値は
設定ファイルが無い場合のフォールバックです。運用上の月例外は
コード変更ではなく設定ファイルへの追記で対応してください。
"""

import json
from dataclasses import dataclass
from datetime import date
from typing import Optional
from .models import Store, Skill, OperationMode
from .paths import CONFIG_DIR

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
# 標準超過時の増員優先順位。左から順に「増員先として許容しやすい」店舗。
STORE_OVERAGE_PRIORITY: tuple[Store, ...] = (
    Store.SUZURAN,
    Store.NISHIGUCHI,
    Store.OMIYA,
    Store.AKABANE,
)

STORE_STAFFING_LIMITS: dict[Store, StoreStaffingLimit] = {
    # 増員優先順位は すずらん → 西口 → 大宮 → 赤羽。
    # 赤羽は標準3名、4名は必要時のみ。
    Store.AKABANE: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=1400),
    # 赤羽東口店は原則1名のみ。
    Store.HIGASHIGUCHI: StoreStaffingLimit(standard_total=1, max_total=1, over_standard_penalty=3000),
    # 大宮駅前店は3名を標準にし、4名は赤羽より優先して許容。
    Store.OMIYA: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=1100),
    # 大宮西口店は原則1名、研修などで2名まで。
    Store.NISHIGUCHI: StoreStaffingLimit(standard_total=1, max_total=2, over_standard_penalty=800),
    # すずらんは3名標準、状況により4名まで。
    Store.SUZURAN: StoreStaffingLimit(standard_total=3, max_total=4, over_standard_penalty=600),
}

# 1日全体の人数上限。
# 通常は11人体制。最大15名までを受け入れ上限として扱う。
GLOBAL_DAILY_STAFFING_LIMIT = DailyStaffingLimit(
    standard_total=11,
    max_total=15,
    over_standard_penalty=900,
)

# 月間勤務日数バランス。
# 会社側の月別基準勤務日数にできる限り一致させる。
# 不足は2日以上で警告、超過は1日以上で警告、3日以上ずれる場合はエラーにする。
WORK_TARGET_IDEAL_TOLERANCE_DAYS = 0
WORK_TARGET_SHORTFALL_WARNING_DIFF_DAYS = 2
WORK_TARGET_OVERAGE_WARNING_DIFF_DAYS = 1
WORK_TARGET_ERROR_DIFF_DAYS = 3


# 月別の目標出勤日数。
# 従来の「年間日数÷12」ではなく、管理側の月別表を優先する。
# プロスタ営業日は店舗運用上の営業日数、出勤日数は正社員系の統一基準。
MONTHLY_TARGET_MONTH_ORDER: tuple[int, ...] = (7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6)
MONTHLY_MAX_WORK_DAYS: dict[int, int] = {
    7: 31, 8: 30, 9: 30, 10: 31, 11: 30, 12: 30,
    1: 29, 2: 28, 3: 31, 4: 30, 5: 30, 6: 30,
}
STANDARD_265_MONTHLY_WORK_TARGETS: dict[int, int] = dict(
    zip(MONTHLY_TARGET_MONTH_ORDER, (23, 22, 22, 23, 22, 22, 21, 21, 23, 22, 22, 22))
)
MONTHLY_WORK_TARGETS: dict[str, dict[int, int]] = {
    name: dict(STANDARD_265_MONTHLY_WORK_TARGETS)
    for name in (
        "今津", "鈴木", "楯", "牧野", "春山", "下地", "長尾", "土井",
        "板倉", "田中", "岩野", "大類", "黒澤", "野澤",
    )
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
    if annual_target_days == 265 and int(month) in STANDARD_265_MONTHLY_WORK_TARGETS:
        return int(STANDARD_265_MONTHLY_WORK_TARGETS[int(month)])
    if annual_target_days is None:
        return None
    return round(int(annual_target_days) / 12)


def get_monthly_required_holiday_days(
    employee_name: str,
    month: int,
    days_in_month: int,
    annual_target_days: Optional[int],
    default_holidays: int,
) -> int:
    """月別基準出勤日数から、その月に必要な休日数を返す。"""
    target = get_monthly_work_target(employee_name, month, annual_target_days)
    if target is None:
        return int(default_holidays)
    return max(0, int(days_in_month) - int(target))

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


def is_store_open_on_day(
    year: int,
    month: int,
    day: int,
    store: Store,
    mode: OperationMode,
) -> bool:
    """営業モードと定休日を踏まえて、指定店舗が営業する日か判定する。"""
    store_capacity = get_capacity(mode).get(store)
    if store_capacity is None:
        return False
    return date(int(year), int(month), int(day)).weekday() not in store_capacity.closed_dow




# ============================================================
# ハード制約（絶対条件）
# ============================================================

# 全体ルール
HARD_CONSTRAINTS = {
    "max_consecutive_work_days": 4,        # 原則最大4連勤（例外的に5連勤あり）
    "max_consecutive_off_days": 2,         # 最大2連休。3連休は絶対条件として禁止
    "min_two_day_off_per_month": 1,        # 2連休を月1回以上
    "max_two_day_off_per_month": 2,        # 2連休は最大2回
    "no_eco_zero_at_any_store": True,      # エコ0NG（全店舗）
    "higashiguchi_eco_required": True,     # 東口は必ずエコ1名
    "nishiguchi_eco_required": True,       # 西口は必ずエコ1名
    "forbidden_same_store_pairings": True, # 指定メンバー同士の同店舗勤務NG
    "forbidden_same_store_groups": True,   # 指定グループ内の同店舗勤務NG
    "mandatory_work_on_request": True,     # 指定スタッフの出勤希望日は必ず出勤
    "month_end_start_omiya": True,         # 下地・春山・黒澤は大宮、店長は自店舗（×希望日は除外）
    "no_ticket_zero_at": [                 # チケット0NG店舗
        Store.AKABANE, Store.OMIYA, Store.SUZURAN
    ],
}

# 大宮店の追加制約：春山・下地どちらか1人は必ず在勤
OMIYA_ANCHOR_STAFF: tuple[str, ...] = ("春山", "下地")

# ============================================================
# 月限定例外の設定ファイル読み込み
# ============================================================
# config/monthly_exceptions.json があれば、そこに書かれたキーだけ
# コード内デフォルトを置き換える。ファイル破損時は本体を止めず
# デフォルトで動き、状態は MONTHLY_EXCEPTIONS_STATUS で確認できる。

MONTHLY_EXCEPTIONS_FILE = CONFIG_DIR / "monthly_exceptions.json"
MONTHLY_EXCEPTIONS_STATUS = "未読み込み"


def _parse_ym(text: str) -> Optional[tuple]:
    """'2026-07' 形式を (2026, 7) に変換する。不正な形式は None。"""
    try:
        y_str, m_str = str(text).strip().split("-", 1)
        y, m = int(y_str), int(m_str)
        if 2024 <= y <= 2099 and 1 <= m <= 12:
            return (y, m)
    except (ValueError, AttributeError):
        pass
    return None


def _load_monthly_exceptions() -> Optional[dict]:
    """月限定例外の設定を読み込む。失敗しても例外を投げない。"""
    global MONTHLY_EXCEPTIONS_STATUS
    try:
        with open(MONTHLY_EXCEPTIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            MONTHLY_EXCEPTIONS_STATUS = "形式エラー（辞書ではない）→デフォルト使用"
            return None
        MONTHLY_EXCEPTIONS_STATUS = "読み込み成功"
        return data
    except FileNotFoundError:
        MONTHLY_EXCEPTIONS_STATUS = "ファイルなし→コード内デフォルト使用"
        return None
    except Exception as exc:
        MONTHLY_EXCEPTIONS_STATUS = f"読み込み失敗（{type(exc).__name__}）→デフォルト使用"
        return None


# 2026年7月は大型連休の重なりが大きい超イレギュラー月。
# 本人の×休み希望を守るため、この月だけ大宮駅前アンカー条件を外し、
# 店舗ごとの最低エコ・最低人数で運用可とする。
# （コード内の値はフォールバック。実際の値は設定ファイルが優先され、
#   reload_monthly_exceptions() で読み込まれる）
_DEFAULT_OMIYA_ANCHOR_RELAXED_MONTHS: tuple[tuple[int, int], ...] = ((2026, 7),)
OMIYA_ANCHOR_RELAXED_MONTHS: tuple[tuple[int, int], ...] = (
    _DEFAULT_OMIYA_ANCHOR_RELAXED_MONTHS
)


def is_omiya_anchor_relaxed_month(year: int, month: int) -> bool:
    """大宮駅前アンカー条件を月限定で緩和するか。"""
    return (int(year), int(month)) in OMIYA_ANCHOR_RELAXED_MONTHS


# 月末月初の大宮駅前固定メンバー。
# 本人の×休み希望がある日は休み希望を最優先し、強制配置しない。
MONTH_END_START_OMIYA_STAFF: tuple[str, ...] = ("下地", "春山", "黒澤")

# 月末月初の店長自店舗固定。
# 本人の×休み希望がある日は休み希望を最優先し、強制配置しない。
MONTH_EDGE_HOME_STORE_ASSIGNMENTS: dict[str, Store] = {
    "今津": Store.AKABANE,
    "土井": Store.HIGASHIGUCHI,
    "下地": Store.OMIYA,
    "長尾": Store.SUZURAN,
    "楯": Store.NISHIGUCHI,
}

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

# 月内の最低巡回条件。
# 本人の休み希望は最優先したうえで、生成できる解では必ず満たす。
STORE_ROTATION_MINIMUMS: dict[str, list[tuple[tuple[Store, ...], int]]] = {}

# 月限定の「同時休みをなるべく避ける」ルール。
# ソフト制約なので、本人の×休み希望や解の成立を優先する。
_DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES: dict[tuple[int, int], tuple[tuple[str, str, str], ...]] = {
    (2026, 7): (
        ("長尾", "野澤", "すずらんメイン2名の同時休みは可能な限り避ける"),
    ),
}
MONTHLY_AVOID_SAME_OFF_RULES: dict[tuple[int, int], tuple[tuple[str, str, str], ...]] = dict(
    _DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES
)

# 前月末から月初へまたがる連勤だけに適用する月別例外。
# 月内の連勤上限は緩めず、前月確定シフト・月初固定配置・本人の×休みが
# 同時に成立しない場合に限って、境界部分の上限を指定日数だけ延長する。
_DEFAULT_MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES: dict[
    tuple[int, int], dict[str, int]
] = {
    (2026, 8): {"下地": 1},
}
MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES: dict[
    tuple[int, int], dict[str, int]
] = dict(_DEFAULT_MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES)


def reload_monthly_exceptions() -> str:
    """設定ファイルを読み直し、月例外ルールを実行中のシステムに反映する。

    画面から月例外を保存した直後にも呼ばれ、再起動なしで反映される。
    設定ファイルが無い・壊れている場合はコード内デフォルトに戻る。
    戻り値は読み込み状態の説明文字列（MONTHLY_EXCEPTIONS_STATUS と同じ）。
    """
    global OMIYA_ANCHOR_RELAXED_MONTHS
    global MONTHLY_AVOID_SAME_OFF_RULES
    global MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES

    data = _load_monthly_exceptions()

    # まずデフォルトへ戻す（ファイル削除・キー削除にも追随できるように）
    OMIYA_ANCHOR_RELAXED_MONTHS = _DEFAULT_OMIYA_ANCHOR_RELAXED_MONTHS
    MONTHLY_AVOID_SAME_OFF_RULES = dict(_DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES)
    MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES = dict(
        _DEFAULT_MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES
    )
    if not data:
        return MONTHLY_EXCEPTIONS_STATUS

    if "omiya_anchor_relaxed_months" in data:
        OMIYA_ANCHOR_RELAXED_MONTHS = tuple(
            ym for ym in (
                _parse_ym(t) for t in (data["omiya_anchor_relaxed_months"] or [])
            )
            if ym is not None
        )

    if "avoid_same_off" in data:
        avoid_parsed: dict = {}
        for ym_text, rules_list in dict(data["avoid_same_off"] or {}).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(rules_list, list):
                continue
            entries = []
            for r in rules_list:
                if isinstance(r, dict) and r.get("a") and r.get("b"):
                    entries.append(
                        (str(r["a"]), str(r["b"]), str(r.get("note", "")))
                    )
            if entries:
                avoid_parsed[ym] = tuple(entries)
        MONTHLY_AVOID_SAME_OFF_RULES = avoid_parsed

    if "carryover_consecutive_allowances" in data:
        carry_parsed: dict = {}
        for ym_text, allow in dict(
            data["carryover_consecutive_allowances"] or {}
        ).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(allow, dict):
                continue
            entries2 = {}
            for name, days in allow.items():
                try:
                    if int(days) > 0:
                        entries2[str(name)] = int(days)
                except (TypeError, ValueError):
                    continue
            if entries2:
                carry_parsed[ym] = entries2
        MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES = carry_parsed

    return MONTHLY_EXCEPTIONS_STATUS


def load_monthly_exceptions_raw() -> dict:
    """設定ファイルの生データを返す（画面での一覧表示・編集用）。

    ファイルが無い場合は現在有効な値（デフォルト含む）から組み立てる。
    """
    data = _load_monthly_exceptions()
    if data:
        return data
    return {
        "omiya_anchor_relaxed_months": [
            f"{y:04d}-{m:02d}" for (y, m) in OMIYA_ANCHOR_RELAXED_MONTHS
        ],
        "carryover_consecutive_allowances": {
            f"{y:04d}-{m:02d}": dict(allow)
            for (y, m), allow in MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES.items()
        },
        "avoid_same_off": {
            f"{y:04d}-{m:02d}": [
                {"a": a, "b": b, "note": note} for (a, b, note) in rules_t
            ]
            for (y, m), rules_t in MONTHLY_AVOID_SAME_OFF_RULES.items()
        },
    }


def save_monthly_exceptions(data: dict, actor: str = "管理者") -> tuple:
    """月例外設定を保存し、実行中のシステムへ即時反映する。

    Returns:
        (成功したか: bool, 状態メッセージ: str)
    """
    payload = dict(data)
    payload["_説明"] = (
        "月限定の例外ルール。画面（⚙️ 設定 → 📅 月例外）から編集できます。"
        "書式は『YYYY-MM』。このファイルにキーがある場合、"
        "コード内のデフォルト値よりこちらが優先されます。"
    )
    from datetime import datetime as _dt
    payload["updated_at"] = _dt.now().isoformat(timespec="seconds")
    payload["updated_by"] = str(actor or "管理者")
    try:
        MONTHLY_EXCEPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MONTHLY_EXCEPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return False, f"保存失敗（{type(exc).__name__}: {exc}）"
    status = reload_monthly_exceptions()
    return True, status


def monthly_carryover_consecutive_allowances(
    year: int,
    month: int,
) -> dict[str, int]:
    """指定月の前月境界にだけ許可する追加連勤日数を返す。"""
    return {
        str(name): max(0, int(days))
        for name, days in MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES.get(
            (int(year), int(month)), {}
        ).items()
        if int(days) > 0
    }


# 起動時（import時）に設定ファイルを読み込んで反映する
reload_monthly_exceptions()


# ============================================================
# 月末月初の固定配置（共有ロジック）
# ============================================================
# 生成（generator）・検証（validator）・未完成下書き（app）の3実装は
# 必ずこの2関数を使い、免除判定を完全に一致させる。
# 過去の障害:
#   2026-08: 月末最終日(月曜)×東口休店の衝突 → 免除2で恒久対応
#   2026-08: 前月末5連勤×月初固定出勤の衝突 → 免除3で恒久対応

def compute_prev_consecutive_run(
    prev_month: Optional[list],
    year: int,
    month: int,
) -> dict[str, int]:
    """前月持ち越しデータから「前月末まで続いた連勤日数」を人ごとに返す。

    prev_month の各要素は employee / last_working_days 属性を持てばよい
    （models.PreviousMonthCarryover を想定）。
    """
    from calendar import monthrange as _monthrange
    result: dict[str, int] = {}
    for p in prev_month or []:
        last_working_days = getattr(p, "last_working_days", None)
        employee = getattr(p, "employee", None)
        if not last_working_days or not employee:
            continue
        prev_month_num = int(month) - 1 if int(month) > 1 else 12
        prev_year = int(year) if int(month) > 1 else int(year) - 1
        last_day = _monthrange(prev_year, prev_month_num)[1]
        consec = 0
        expected = last_day
        for dd in sorted(last_working_days, reverse=True):
            if dd == expected:
                consec += 1
                expected -= 1
            else:
                break
        if consec > 0:
            result[str(employee)] = consec
    return result


def month_edge_forced_assignments(
    year: int,
    month: int,
    days_in_month: int,
    off_requests: Optional[dict] = None,
    operation_modes: Optional[dict] = None,
    prev_consec_map: Optional[dict] = None,
    hard_max_consec: int = 5,
    employee_max_consecutive_work: Optional[dict] = None,
    consec_exceptions: Optional[list] = None,
    include_names: Optional[set] = None,
    valid_stores: Optional[set] = None,
) -> tuple:
    """月末月初の固定配置（強制出勤）を、全免除条件を適用して返す。

    Returns:
        (forced, notes)
        forced: list[(employee_name, day, Store)] 強制出勤として確定した組
        notes:  list[str] 自動免除の日本語説明（画面表示・ログ用）

    免除条件（判定順）:
        1. 本人の×休み希望がある日
        2. 対象店舗が休店の日（定休日・営業モード）
        3. 前月からの連勤持ち越しで月初1日に出勤すると連勤上限を
           超える場合（monthly_carryover_consecutive_allowances の
           延長許可がある人は免除しない＝固定配置を維持する）
    """
    off_requests = off_requests or {}
    operation_modes = operation_modes or {}
    prev_consec_map = prev_consec_map or {}
    emp_max = employee_max_consecutive_work or {}
    consec_exceptions = list(consec_exceptions or [])
    allowances = monthly_carryover_consecutive_allowances(year, month)

    edge_days = (1, int(days_in_month))
    candidate_pairs = []
    for _name in MONTH_END_START_OMIYA_STAFF:
        for _d in edge_days:
            candidate_pairs.append((_name, _d, Store.OMIYA))
    for _name, _home in MONTH_EDGE_HOME_STORE_ASSIGNMENTS.items():
        for _d in edge_days:
            candidate_pairs.append((_name, _d, _home))

    forced = []
    notes = []
    seen = set()
    for name, d, store in candidate_pairs:
        if (name, d, store) in seen:
            continue
        seen.add((name, d, store))
        if include_names is not None and name not in include_names:
            continue
        if valid_stores is not None and store not in valid_stores:
            continue
        # 免除1: 本人の×休み希望
        if d in set(off_requests.get(name, [])):
            continue
        # 免除2: 店舗休店日
        mode = operation_modes.get(d, OperationMode.NORMAL)
        if not is_store_open_on_day(year, month, d, store, mode):
            continue
        # 免除3: 前月持ち越し連勤との衝突（月初1日のみ）
        if d == 1 and name not in consec_exceptions:
            prev = int(prev_consec_map.get(name, 0) or 0)
            if prev > 0:
                try:
                    personal_max = int(emp_max.get(name, hard_max_consec))
                except (TypeError, ValueError):
                    personal_max = int(hard_max_consec)
                limit = min(int(hard_max_consec), personal_max)
                limit += int(allowances.get(name, 0))
                if prev + 1 > limit:
                    notes.append(
                        f"{name}: 前月末から{prev}連勤のため、"
                        f"{int(month)}月1日の固定配置を自動免除しました"
                        "（連勤上限と衝突するため休みを優先。"
                        "出勤させたい場合は「⚙️ 設定 → 📅 月例外」で"
                        "境界連勤の延長を許可してください）。"
                    )
                    continue
        forced.append((name, d, store))
    return forced, notes

# 個別に少し寄せたい店舗。絶対条件ではなく、生成時の追加スコアとして扱う。
STORE_ASSIGNMENT_EXTRA_WEIGHTS: dict[tuple[str, Store], int] = {
    ("今津", Store.AKABANE): 6,
}

# 出勤希望日を必ず勤務にする従業員。
# 希望していない日まで無条件に配置するのではなく、提出された出勤希望を絶対扱いにする。
MANDATORY_WORK_ON_REQUEST_EMPLOYEES: tuple[str, ...] = ("南",)

# 店舗限定の同店舗同勤務NGルール。
# 現在の同店舗NGは下のグループ制約で表現できるため、ここは空にしている。
FORBIDDEN_SAME_STORE_PAIRINGS: tuple[tuple[Store, str, tuple[str, ...]], ...] = (
)

# このグループ内のメンバー同士は、同じ日に同じ店舗へ配置しない。
FORBIDDEN_SAME_STORE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("下地", "今津", "長尾", "楯", "土井"),
    ("今津", "長尾", "楯", "土井", "春山"),
)

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
    "大塚",   # パート運用のため一部制約を除外（ただし最大4連勤は適用）
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
