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
import os
from dataclasses import dataclass
from datetime import date, datetime
from calendar import monthrange
from typing import Optional
from .models import Affinity, Store, Skill, OperationMode
from .paths import CONFIG_DIR

# ============================================================
# 店舗別の必要人数（営業モードごと）
# ============================================================

@dataclass
class StoreCapacity:
    """店舗の1日の必要人数（モードごとに変動）"""
    eco_min: int            # エコ要員の最小数
    # エコ最低人数を除いた残りの必要人数。エコ担当もチケット対応できるため、
    # チケット専任者だけでこの人数を満たす必要はない。
    ticket_min: int
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
        eco_min=1,
        ticket_min=2,
        eco_max=3,
        # 通常はエコ対応1名以上+合計3名。ticket_min は残りの必要人数を表し、
        # エコ担当もチケット対応できるためチケット専任者2名を要求しない。
        # 需給調整時はエコ対応1名以上+合計2名を許容する（→人数少△）。
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
    # 2026年7月に年間基準265日へ統一。通常の社員は
    # STANDARD_265_MONTHLY_WORK_TARGETS（265日の月別配分）が自動適用される。
    # ここには「標準と異なる人」だけを書く（現在は該当者なし）。
    # 南=出勤希望のみ勤務 / 大塚=自由記載の月別指定 / 山本=補助要員。
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
MONTHLY_EXCEPTIONS_HISTORY_LIMIT = 20


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


def _store_from_config(value) -> Optional[Store]:
    """設定JSONの店舗名・記号を Store に変換する。"""
    if isinstance(value, Store):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Store[text]
    except KeyError:
        pass
    for store in Store:
        if text in (store.value, store.display_name):
            return store
    return None


def _clean_monthly_exceptions_snapshot(data: dict) -> dict:
    """履歴を再帰的に抱え込まない、月例外設定のスナップショットを返す。"""
    return {
        key: value
        for key, value in dict(data or {}).items()
        if key not in {"_history", "updated_at", "updated_by"}
    }


def validate_monthly_exceptions_data(data: dict) -> tuple[list[str], list[str]]:
    """管理画面で保存する月例外設定の矛盾・入力漏れを確認する。"""
    errors: list[str] = []
    warnings: list[str] = []

    for ym_text, employee_rules in dict(
        data.get("employee_store_overrides", {}) or {}
    ).items():
        if _parse_ym(ym_text) is None:
            errors.append(f"店舗区分の対象月「{ym_text}」が不正です。")
            continue
        if not isinstance(employee_rules, dict):
            errors.append(f"{ym_text}・店舗区分の保存形式が不正です。")
            continue
        for employee_name, rule in dict(employee_rules or {}).items():
            if not isinstance(rule, dict):
                errors.append(
                    f"{ym_text}・{employee_name}: 店舗区分の保存形式が不正です。"
                )
                continue
            category_values = {
                "主担当": [rule.get("primary_store")]
                if rule.get("primary_store") else [],
                "通常担当": list(rule.get("normal_stores") or []),
                "応援・巡回担当": list(rule.get("support_stores") or []),
                "応援・巡回から外す店舗": list(
                    rule.get("remove_support_stores") or []
                ),
            }
            normalized_categories: dict[str, set[Store]] = {}
            for label, values in category_values.items():
                parsed_values: set[Store] = set()
                for value in values:
                    store = _store_from_config(value)
                    if store is None or store == Store.OFF:
                        errors.append(
                            f"{ym_text}・{employee_name}: {label}の店舗「{value}」が不正です。"
                        )
                        continue
                    parsed_values.add(store)
                normalized_categories[label] = parsed_values

            category_labels = list(normalized_categories)
            for index, first_label in enumerate(category_labels):
                for second_label in category_labels[index + 1:]:
                    overlap = (
                        normalized_categories[first_label]
                        & normalized_categories[second_label]
                    )
                    if overlap:
                        overlap_text = "・".join(
                            store.display_name for store in sorted(
                                overlap, key=lambda item: item.name,
                            )
                        )
                        errors.append(
                            f"{ym_text}・{employee_name}: {overlap_text}が"
                            f"「{first_label}」と「{second_label}」で重複しています。"
                        )

    for ym_text, plans in dict(data.get("training_plans", {}) or {}).items():
        ym = _parse_ym(ym_text)
        if ym is None:
            errors.append(f"研修計画の対象月「{ym_text}」が不正です。")
            continue
        if not isinstance(plans, list):
            errors.append(f"{ym_text}・研修計画の保存形式が不正です。")
            continue
        max_day = monthrange(*ym)[1]
        seen_trainees: set[str] = set()
        for index, plan in enumerate(list(plans or []), start=1):
            if not isinstance(plan, dict):
                errors.append(
                    f"{ym_text}・研修計画{index}: 保存形式が不正です。"
                )
                continue
            trainee = str(plan.get("trainee") or "").strip()
            label = str(plan.get("name") or f"研修計画{index}")
            if not trainee:
                errors.append(f"{ym_text}・{label}: 研修対象者が未設定です。")
                continue
            if trainee in seen_trainees:
                warnings.append(
                    f"{ym_text}・{trainee}: 複数の研修計画があります。内容の重複を確認してください。"
                )
            seen_trainees.add(trainee)
            approved = {
                str(name) for name in (plan.get("approved_mentors") or [])
                if str(name).strip()
            }
            if trainee in approved:
                errors.append(
                    f"{ym_text}・{label}: 研修対象者自身を指導担当には指定できません。"
                )
            phases = list(plan.get("phases") or [])
            if not phases:
                errors.append(f"{ym_text}・{label}: 研修段階が1つもありません。")
                continue
            for phase_index, phase in enumerate(phases, start=1):
                phase_label = str(
                    phase.get("label") or f"第{phase_index}段階"
                )
                try:
                    start_day = int(phase.get("start_day", 1))
                    end_day = int(phase.get("end_day", max_day))
                    target_count = int(phase.get("target_count", 0))
                except (TypeError, ValueError):
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: 日付または回数が不正です。"
                    )
                    continue
                if not (1 <= start_day <= end_day <= max_day):
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: "
                        f"日付範囲は1日から{max_day}日の間で指定してください。"
                    )
                if target_count < 0 or target_count > max(0, end_day - start_day + 1):
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: "
                        "研修回数が日付範囲の日数を超えています。"
                    )
                if _store_from_config(phase.get("store")) in (None, Store.OFF):
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: 店舗が未設定です。"
                    )
                mentors = {
                    str(name) for name in (phase.get("mentors") or [])
                    if str(name).strip()
                }
                if not mentors:
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: 指導担当を1人以上指定してください。"
                    )
                if trainee in mentors:
                    errors.append(
                        f"{ym_text}・{label}・{phase_label}: "
                        "研修対象者自身を指導担当には指定できません。"
                    )
            if plan.get("require_mentor_on_workday") and not approved:
                errors.append(
                    f"{ym_text}・{label}: 全出勤日の指導担当確認を使う場合は、"
                    "月全体の指導担当を1人以上指定してください。"
                )

    for ym_text, policy in dict(data.get("yamamoto_policy", {}) or {}).items():
        if _parse_ym(ym_text) is None:
            errors.append(f"山本補助勤務の対象月「{ym_text}」が不正です。")
            continue
        if not isinstance(policy, dict):
            errors.append(f"{ym_text}・山本補助勤務の保存形式が不正です。")
            continue
        try:
            max_days = int(policy.get("max_days"))
            max_consecutive = int(policy.get("max_consecutive"))
        except (TypeError, ValueError):
            errors.append(f"{ym_text}・山本補助勤務: 日数設定が不正です。")
            continue
        if not 0 <= max_days <= 31:
            errors.append(f"{ym_text}・山本補助勤務: 月間上限は0〜31日で指定してください。")
        if not 1 <= max_consecutive <= 7:
            errors.append(f"{ym_text}・山本補助勤務: 連続勤務上限は1〜7日で指定してください。")
        if max_consecutive > max_days and max_days > 0:
            warnings.append(
                f"{ym_text}・山本補助勤務: 連続勤務上限が月間上限を超えています。"
            )
        if policy.get("auto_only_if_needed") is False:
            errors.append(
                f"{ym_text}・山本補助勤務: 自動生成は「赤羽で必要な日だけ」に固定されています。"
            )

    return errors, warnings


def summarize_monthly_exceptions_change(before: dict, after: dict) -> list[str]:
    """保存前画面に出す、変更箇所の日本語要約を返す。"""
    labels = {
        "omiya_anchor_relaxed_months": "大宮アンカー緩和",
        "tanaka_training": "従来型の研修組み合わせ",
        "training_plans": "段階別研修計画",
        "employee_store_overrides": "月限定の店舗区分",
        "carryover_consecutive_allowances": "境界連勤の延長",
        "avoid_same_off": "同時休み回避",
        "operation_modes": "営業モード",
        "yamamoto_policy": "山本の補助勤務方針",
    }
    before_clean = _clean_monthly_exceptions_snapshot(before)
    after_clean = _clean_monthly_exceptions_snapshot(after)
    changed = [
        labels.get(key, key)
        for key in sorted(set(before_clean) | set(after_clean))
        if before_clean.get(key) != after_clean.get(key)
        and not str(key).startswith("_")
    ]
    return changed or ["更新者・説明などの管理情報"]


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

# ============================================================
# 月限定の確定ルール
# ============================================================
# ここに置く値は設定ファイルが無い場合のフォールバックです。
# 実運用では config/monthly_exceptions.json を唯一の編集元とし、
# 生成・検証・画面表示が同じ設定を参照します。

def is_omiya_two_person_allowed_month(year: int, month: int) -> bool:
    """大宮駅前のエコ対応1名以上・合計2名体制を許容するか。

    現在人員では毎月一定数起こり得る需給調整なので全月で許容する。
    通常はエコ対応1名以上・合計3名体制を優先する。エコ担当は
    チケット対応もできるため、エコ・チケット専任の内訳は固定しない。
    """
    return True


_DEFAULT_TANAKA_PAIR_TRAINING_RULES: dict[tuple[int, int], dict] = {
    (2026, 8): {
        "employee": "田中",
        "nishiguchi_partner": "楯",
        "nishiguchi_count": 6,
        "akabane_partner": "楯",
        "akabane_third_candidates": ("鈴木", "板倉"),
        "akabane_count": 5,
        "higashiguchi_partner": "土井",
        "higashiguchi_from_day": 20,
        "higashiguchi_count": 1,
    },
}
TANAKA_PAIR_TRAINING_RULES: dict[tuple[int, int], dict] = {
    ym: dict(rule) for ym, rule in _DEFAULT_TANAKA_PAIR_TRAINING_RULES.items()
}


def tanaka_pair_training_rule(year: int, month: int) -> Optional[dict]:
    """指定月の研修ペア条件を返す。対象外の月は None。"""
    rule = TANAKA_PAIR_TRAINING_RULES.get((int(year), int(month)))
    return dict(rule) if rule else None


def is_tanaka_pair_training_month(year: int, month: int) -> bool:
    """月限定の研修ペア勤務ルールを適用する月か。"""
    return tanaka_pair_training_rule(year, month) is not None


# 将来の研修を「対象者・期間・店舗・指導担当・回数」で設定する汎用形式。
# 8月の田中研修は確定済みシフトとの互換性を優先し、上の従来形式に残す。
_DEFAULT_MONTHLY_TRAINING_PLANS: dict[tuple[int, int], tuple[dict, ...]] = {}
MONTHLY_TRAINING_PLANS: dict[tuple[int, int], tuple[dict, ...]] = {}


def monthly_training_plans(year: int, month: int) -> tuple[dict, ...]:
    """指定月の段階別研修計画を返す。"""
    plans = MONTHLY_TRAINING_PLANS.get((int(year), int(month)), ())
    return tuple(
        {
            **dict(plan),
            "approved_mentors": tuple(plan.get("approved_mentors", ())),
            "phases": tuple(
                {
                    **dict(phase),
                    "mentors": tuple(phase.get("mentors", ())),
                }
                for phase in plan.get("phases", ())
            ),
        }
        for plan in plans
    )


def monthly_training_store_days(
    year: int,
    month: int,
    employee_name: str,
) -> dict[Store, set[int]]:
    """研修対象者について、月例外で明示的に許可した店舗と日付を返す。"""
    result: dict[Store, set[int]] = {}
    max_day = monthrange(int(year), int(month))[1]
    for plan in monthly_training_plans(year, month):
        if str(plan.get("trainee")) != str(employee_name):
            continue
        for phase in plan.get("phases", ()):
            store = phase.get("store")
            if not isinstance(store, Store) or store == Store.OFF:
                continue
            start_day = max(1, int(phase.get("start_day", 1)))
            end_day = min(max_day, int(phase.get("end_day", max_day)))
            result.setdefault(store, set()).update(
                range(start_day, end_day + 1)
            )
    return result


# 月限定の従業員別店舗区分。
# normal_stores / support_stores が None の古い設定は、基本区分を維持する。
# リスト（空リストを含む）が保存された新設定は、その月の区分を表す。
# remove_support_stores は絶対配置不可ではなく、通常候補から外す指定です。
# 人員不足時の緊急配置までは禁止しません。
_DEFAULT_MONTHLY_EMPLOYEE_STORE_OVERRIDES: dict[tuple[int, int], dict] = {
    (2026, 8): {
        "大類": {
            "primary_store": Store.OMIYA,
            "remove_support_stores": (Store.AKABANE,),
        },
    },
}
MONTHLY_EMPLOYEE_STORE_OVERRIDES: dict[tuple[int, int], dict] = {
    ym: {
        name: {
            "primary_store": rule.get("primary_store"),
            "normal_stores": rule.get("normal_stores"),
            "support_stores": rule.get("support_stores"),
            "remove_support_stores": tuple(rule.get("remove_support_stores", ())),
        }
        for name, rule in employees.items()
    }
    for ym, employees in _DEFAULT_MONTHLY_EMPLOYEE_STORE_OVERRIDES.items()
}


def monthly_employee_store_override(
    year: int,
    month: int,
    employee_name: str,
) -> dict:
    """月限定の主担当・通常・応援巡回・応援除外指定を返す。"""
    rule = MONTHLY_EMPLOYEE_STORE_OVERRIDES.get(
        (int(year), int(month)), {}
    ).get(str(employee_name), {})
    return {
        "primary_store": rule.get("primary_store"),
        "normal_stores": (
            tuple(rule.get("normal_stores", ()))
            if rule.get("normal_stores") is not None
            else None
        ),
        "support_stores": (
            tuple(rule.get("support_stores", ()))
            if rule.get("support_stores") is not None
            else None
        ),
        "remove_support_stores": tuple(rule.get("remove_support_stores", ())),
    }


def effective_employee_store_affinities(
    employee,
    year: int,
    month: int,
) -> dict[Store, Optional[Affinity]]:
    """基本設定へ月限定区分を重ねた、その月の店舗適性を返す。

    Affinity.NONE（絶対配置不可）は月限定設定で解除しない。新形式の月限定
    区分でどこにも含めなかった店舗は、禁止ではなく緊急時だけ候補に残すため
    None を返す。
    """
    base_affinities = dict(getattr(employee, "affinities", {}) or {})
    override = monthly_employee_store_override(
        year, month, getattr(employee, "name", ""),
    )
    primary_store = override.get("primary_store")
    normal_stores = override.get("normal_stores")
    support_stores = override.get("support_stores")
    removed_stores = set(override.get("remove_support_stores", ()))
    full_monthly_categories = (
        normal_stores is not None or support_stores is not None
    )
    normal_set = set(normal_stores or ())
    support_set = set(support_stores or ())

    effective: dict[Store, Optional[Affinity]] = {}
    for store in Store:
        if store == Store.OFF:
            continue
        base_affinity = base_affinities.get(store, Affinity.NONE)
        if base_affinity == Affinity.NONE:
            effective[store] = Affinity.NONE
        elif store in removed_stores:
            effective[store] = None
        elif full_monthly_categories:
            if store == primary_store:
                effective[store] = Affinity.STRONG
            elif store in normal_set:
                effective[store] = Affinity.MEDIUM
            elif store in support_set:
                effective[store] = Affinity.WEAK
            else:
                effective[store] = None
        elif store == primary_store:
            # 旧形式（主担当と応援除外のみ）の既存データとの互換。
            effective[store] = Affinity.STRONG
        else:
            effective[store] = base_affinity
    return effective


_DEFAULT_MONTHLY_YAMAMOTO_POLICIES: dict[tuple[int, int], dict] = {}
MONTHLY_YAMAMOTO_POLICIES: dict[tuple[int, int], dict] = {}


def yamamoto_monthly_policy(year: int, month: int) -> dict:
    """山本さんの補助勤務方針を返す。上限は目標日数ではない。"""
    default_max = 14 if int(month) in (1, 2) else 15
    configured = MONTHLY_YAMAMOTO_POLICIES.get(
        (int(year), int(month)), {}
    )
    return {
        "max_days": int(configured.get("max_days", default_max)),
        "max_consecutive": int(configured.get("max_consecutive", 2)),
        "auto_only_if_needed": True,
        "store": Store.AKABANE,
        "manual_extra_allowed": True,
    }


def yamamoto_monthly_max_days(month: int, year: Optional[int] = None) -> int:
    """山本さんの手動調整後の上限（自動生成の目標日数ではない）。"""
    if year is None:
        return 14 if int(month) in (1, 2) else 15
    return int(yamamoto_monthly_policy(int(year), int(month))["max_days"])


def yamamoto_monthly_max_consecutive(year: int, month: int) -> int:
    """山本さんの月別連続勤務上限を返す。"""
    return int(
        yamamoto_monthly_policy(int(year), int(month))["max_consecutive"]
    )


def active_code_managed_monthly_rules(year: int, month: int) -> list:
    """設定ファイルで管理している月限定ルールの説明文を返す。"""
    notes = []
    if is_omiya_two_person_allowed_month(year, month):
        notes.append(
            "固定ルール: 大宮駅前はエコ対応1名以上・合計2名体制を許容"
            "（通常はエコ対応1名以上・合計3名体制を優先）"
        )
    tanaka_rule = tanaka_pair_training_rule(year, month)
    if tanaka_rule:
        training_employee = str(tanaka_rule.get("employee") or "対象者")
        notes.append(
            f"{training_employee}さんの月限定研修ペア勤務: "
            f"西口({tanaka_rule['nishiguchi_partner']}さんと同日)"
            f"×{tanaka_rule['nishiguchi_count']}回・"
            f"赤羽({tanaka_rule['akabane_partner']}さん＋"
            f"{'or'.join(tanaka_rule['akabane_third_candidates'])}さんと同日)"
            f"×{tanaka_rule['akabane_count']}回・"
            f"東口({tanaka_rule['higashiguchi_partner']}さんと同日・"
            f"{tanaka_rule['higashiguchi_from_day']}日以降)"
            f"×{tanaka_rule['higashiguchi_count']}回。"
            "この組み合わせ以外の日は東口・西口に入らない"
        )
    for plan in monthly_training_plans(year, month):
        phase_notes = []
        for phase in plan.get("phases", ()):
            comparison = (
                "ちょうど"
                if str(phase.get("comparison")) == "exact"
                else "以上"
            )
            phase_notes.append(
                f"{int(phase.get('start_day', 1))}〜"
                f"{int(phase.get('end_day', monthrange(year, month)[1]))}日・"
                f"{phase['store'].display_name}・"
                f"{'/'.join(phase.get('mentors', ()))}と"
                f"{int(phase.get('target_count', 0))}回{comparison}"
            )
        notes.append(
            f"{plan.get('trainee')}さんの段階別研修"
            f"（{'絶対条件' if plan.get('severity') == 'ERROR' else '強い目標'}）: "
            + "、".join(phase_notes)
        )
    for employee_name, override in MONTHLY_EMPLOYEE_STORE_OVERRIDES.get(
        (int(year), int(month)), {}
    ).items():
        primary_store = override.get("primary_store")
        normal_stores = override.get("normal_stores")
        support_stores = override.get("support_stores")
        removed = tuple(override.get("remove_support_stores", ()))
        parts = []
        if primary_store is not None:
            parts.append(f"{primary_store.display_name}を主担当")
        elif normal_stores is not None or support_stores is not None:
            parts.append("主担当なし")
        if normal_stores is not None:
            parts.append(
                "通常担当 "
                + (
                    "・".join(store.display_name for store in normal_stores)
                    or "なし"
                )
            )
        if support_stores is not None:
            parts.append(
                "応援・巡回担当 "
                + (
                    "・".join(store.display_name for store in support_stores)
                    or "なし"
                )
            )
        if removed:
            parts.append(
                "応援・巡回先から"
                + "・".join(store.display_name for store in removed)
                + "を外す"
            )
        elif normal_stores is not None or support_stores is not None:
            parts.append("応援・巡回から外す店舗なし")
        notes.append(
            f"{employee_name}さん: {'、'.join(parts)}"
            "（外した店舗も緊急時の手動配置は禁止しない）"
        )
    policy = yamamoto_monthly_policy(year, month)
    notes.append(
        "山本さん: 赤羽で通常スタッフだけでは不足する日に限り自動投入。"
        f"月間上限{policy['max_days']}日、"
        f"連続{policy['max_consecutive']}日まで。"
        "追加勤務は完成後に手動調整"
    )
    return notes

# 月内の最低巡回条件。
# 本人の休み希望は最優先したうえで、生成できる解では必ず満たす。
STORE_ROTATION_MINIMUMS: dict[str, list[tuple[tuple[Store, ...], int]]] = {}

# 全月共通の「すずらん主力2名の同時不在をなるべく避ける」ルール。
# 2人の×休みが重なる日や、他店舗の必要人数を崩す場合は許容する。
FIXED_SUZURAN_CORE_PRESENCE_RULES: tuple[tuple[str, str, str], ...] = (
    ("長尾", "野澤", "すずらんメイン2名のどちらも不在になる日を可能な限り避ける"),
)

# 月限定で追加する同時休み回避ルール。
_DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES: dict[
    tuple[int, int], tuple[tuple[str, str, str], ...]
] = {}
MONTHLY_AVOID_SAME_OFF_RULES: dict[tuple[int, int], tuple[tuple[str, str, str], ...]] = dict(
    _DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES
)


def avoid_same_off_rules(
    year: int,
    month: int,
) -> tuple[tuple[str, str, str], ...]:
    """管理画面でその月に設定した同時休み回避ルールを返す。"""
    combined = list(
        MONTHLY_AVOID_SAME_OFF_RULES.get((int(year), int(month)), ())
    )
    result = []
    seen_pairs = set()
    for first_name, second_name, reason in combined:
        pair_key = tuple(sorted((str(first_name), str(second_name))))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        result.append((str(first_name), str(second_name), str(reason)))
    return tuple(result)


def fixed_suzuran_core_presence_rules(
) -> tuple[tuple[str, str, str], ...]:
    """全月共通のすずらん主力同時不在回避ルールを返す。"""
    return FIXED_SUZURAN_CORE_PRESENCE_RULES


def monthly_avoid_same_off_rules(
    year: int,
    month: int,
) -> tuple[tuple[str, str, str], ...]:
    """管理画面でその月に明示設定した同時休み回避ルールだけを返す。"""
    return tuple(
        MONTHLY_AVOID_SAME_OFF_RULES.get((int(year), int(month)), ())
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

# 営業モードの月別設定（{(年, 月): {日: OperationMode}}）。
# 経営方針: 基本は全日通常体制。省人員・休業は経営判断として
# 画面（📅 月例外）から明示的に設定した日だけ適用する。
# （旧仕様の GW・お盆・SW の自動適用は廃止した）
_DEFAULT_MONTHLY_OPERATION_MODES: dict = {}
MONTHLY_OPERATION_MODES: dict = dict(_DEFAULT_MONTHLY_OPERATION_MODES)

_MODE_LABEL_TO_ENUM = {m.value: m for m in OperationMode}


def monthly_operation_mode_overrides(year: int, month: int) -> dict:
    """指定月の営業モード設定（{日: OperationMode}）を返す。"""
    return dict(MONTHLY_OPERATION_MODES.get((int(year), int(month)), {}))


def reload_monthly_exceptions() -> str:
    """設定ファイルを読み直し、月例外ルールを実行中のシステムに反映する。

    画面から月例外を保存した直後にも呼ばれ、再起動なしで反映される。
    設定ファイルが無い・壊れている場合はコード内デフォルトに戻る。
    戻り値は読み込み状態の説明文字列（MONTHLY_EXCEPTIONS_STATUS と同じ）。
    """
    global OMIYA_ANCHOR_RELAXED_MONTHS
    global MONTHLY_AVOID_SAME_OFF_RULES
    global MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES
    global MONTHLY_OPERATION_MODES
    global TANAKA_PAIR_TRAINING_RULES
    global MONTHLY_TRAINING_PLANS
    global MONTHLY_EMPLOYEE_STORE_OVERRIDES
    global MONTHLY_YAMAMOTO_POLICIES

    data = _load_monthly_exceptions()

    # まずデフォルトへ戻す（ファイル削除・キー削除にも追随できるように）
    OMIYA_ANCHOR_RELAXED_MONTHS = _DEFAULT_OMIYA_ANCHOR_RELAXED_MONTHS
    MONTHLY_AVOID_SAME_OFF_RULES = dict(_DEFAULT_MONTHLY_AVOID_SAME_OFF_RULES)
    MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES = dict(
        _DEFAULT_MONTHLY_CARRYOVER_CONSECUTIVE_ALLOWANCES
    )
    MONTHLY_OPERATION_MODES = dict(_DEFAULT_MONTHLY_OPERATION_MODES)
    TANAKA_PAIR_TRAINING_RULES = {
        ym: dict(rule)
        for ym, rule in _DEFAULT_TANAKA_PAIR_TRAINING_RULES.items()
    }
    MONTHLY_TRAINING_PLANS = {
        ym: tuple(dict(plan) for plan in plans)
        for ym, plans in _DEFAULT_MONTHLY_TRAINING_PLANS.items()
    }
    MONTHLY_EMPLOYEE_STORE_OVERRIDES = {
        ym: {
            name: {
                "primary_store": rule.get("primary_store"),
                "remove_support_stores": tuple(
                    rule.get("remove_support_stores", ())
                ),
            }
            for name, rule in employees.items()
        }
        for ym, employees in _DEFAULT_MONTHLY_EMPLOYEE_STORE_OVERRIDES.items()
    }
    MONTHLY_YAMAMOTO_POLICIES = {
        ym: dict(policy)
        for ym, policy in _DEFAULT_MONTHLY_YAMAMOTO_POLICIES.items()
    }
    if not data:
        return MONTHLY_EXCEPTIONS_STATUS

    if "operation_modes" in data:
        modes_parsed: dict = {}
        for ym_text, day_map in dict(data["operation_modes"] or {}).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(day_map, dict):
                continue
            entries3 = {}
            for day_text, mode_label in day_map.items():
                mode = _MODE_LABEL_TO_ENUM.get(str(mode_label))
                try:
                    day_num = int(day_text)
                except (TypeError, ValueError):
                    continue
                if mode is not None and 1 <= day_num <= 31:
                    entries3[day_num] = mode
            if entries3:
                modes_parsed[ym] = entries3
        MONTHLY_OPERATION_MODES = modes_parsed

    if "omiya_anchor_relaxed_months" in data:
        OMIYA_ANCHOR_RELAXED_MONTHS = tuple(
            ym for ym in (
                _parse_ym(t) for t in (data["omiya_anchor_relaxed_months"] or [])
            )
            if ym is not None
        )

    if "tanaka_training" in data:
        training_parsed: dict = {}
        for ym_text, raw_rule in dict(data["tanaka_training"] or {}).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(raw_rule, dict):
                continue
            third_candidates = tuple(
                str(name) for name in (
                    raw_rule.get("akabane_third_candidates") or []
                )
                if str(name).strip()
            )
            try:
                parsed_rule = {
                    "employee": str(raw_rule.get("employee") or "田中"),
                    "nishiguchi_partner": str(
                        raw_rule.get("nishiguchi_partner") or "楯"
                    ),
                    "nishiguchi_count": int(
                        raw_rule.get("nishiguchi_count", 0)
                    ),
                    "akabane_partner": str(
                        raw_rule.get("akabane_partner") or "楯"
                    ),
                    "akabane_third_candidates": third_candidates,
                    "akabane_count": int(raw_rule.get("akabane_count", 0)),
                    "higashiguchi_partner": str(
                        raw_rule.get("higashiguchi_partner") or "土井"
                    ),
                    "higashiguchi_from_day": int(
                        raw_rule.get("higashiguchi_from_day", 1)
                    ),
                    "higashiguchi_count": int(
                        raw_rule.get("higashiguchi_count", 0)
                    ),
                }
            except (TypeError, ValueError):
                continue
            if (
                parsed_rule["nishiguchi_count"] >= 0
                and parsed_rule["akabane_count"] >= 0
                and parsed_rule["higashiguchi_count"] >= 0
                and parsed_rule["akabane_third_candidates"]
            ):
                training_parsed[ym] = parsed_rule
        TANAKA_PAIR_TRAINING_RULES = training_parsed

    if "training_plans" in data:
        plans_parsed: dict = {}
        for ym_text, raw_plans in dict(data["training_plans"] or {}).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(raw_plans, list):
                continue
            max_day = monthrange(*ym)[1]
            month_plans = []
            for index, raw_plan in enumerate(raw_plans):
                if not isinstance(raw_plan, dict):
                    continue
                trainee = str(raw_plan.get("trainee") or "").strip()
                if not trainee:
                    continue
                phases = []
                for raw_phase in list(raw_plan.get("phases") or []):
                    if not isinstance(raw_phase, dict):
                        continue
                    store = _store_from_config(raw_phase.get("store"))
                    mentors = tuple(
                        str(name) for name in (raw_phase.get("mentors") or [])
                        if str(name).strip() and str(name) != trainee
                    )
                    try:
                        start_day = max(1, int(raw_phase.get("start_day", 1)))
                        end_day = min(
                            max_day, int(raw_phase.get("end_day", max_day))
                        )
                        target_count = max(
                            0, int(raw_phase.get("target_count", 0))
                        )
                    except (TypeError, ValueError):
                        continue
                    if (
                        store in (None, Store.OFF)
                        or not mentors
                        or start_day > end_day
                    ):
                        continue
                    phases.append({
                        "label": str(
                            raw_phase.get("label")
                            or f"第{len(phases) + 1}段階"
                        ),
                        "start_day": start_day,
                        "end_day": end_day,
                        "store": store,
                        "mentors": mentors,
                        "target_count": min(
                            target_count, end_day - start_day + 1
                        ),
                        "comparison": (
                            "exact"
                            if str(raw_phase.get("comparison")) == "exact"
                            else "min"
                        ),
                        "severity": (
                            "ERROR"
                            if str(raw_phase.get("severity")).upper() == "ERROR"
                            else "WARNING"
                        ),
                    })
                if not phases:
                    continue
                approved_mentors = tuple(
                    str(name)
                    for name in (raw_plan.get("approved_mentors") or [])
                    if str(name).strip() and str(name) != trainee
                )
                month_plans.append({
                    "id": str(
                        raw_plan.get("id")
                        or f"{ym_text}-{trainee}-{index + 1}"
                    ),
                    "name": str(
                        raw_plan.get("name") or f"{trainee}研修"
                    ),
                    "trainee": trainee,
                    "severity": (
                        "ERROR"
                        if str(raw_plan.get("severity")).upper() == "ERROR"
                        else "WARNING"
                    ),
                    "require_mentor_on_workday": bool(
                        raw_plan.get("require_mentor_on_workday", False)
                    ),
                    "approved_mentors": approved_mentors,
                    "phases": tuple(phases),
                })
            if month_plans:
                plans_parsed[ym] = tuple(month_plans)
        MONTHLY_TRAINING_PLANS = plans_parsed

    if "employee_store_overrides" in data:
        overrides_parsed: dict = {}
        for ym_text, employee_rules in dict(
            data["employee_store_overrides"] or {}
        ).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(employee_rules, dict):
                continue
            parsed_employees = {}
            for employee_name, raw_rule in employee_rules.items():
                if not isinstance(raw_rule, dict):
                    continue
                primary_store = _store_from_config(
                    raw_rule.get("primary_store")
                )
                normal_stores = (
                    tuple(
                        store for store in (
                            _store_from_config(value)
                            for value in (raw_rule.get("normal_stores") or [])
                        )
                        if store is not None and store != Store.OFF
                    )
                    if "normal_stores" in raw_rule
                    else None
                )
                support_stores = (
                    tuple(
                        store for store in (
                            _store_from_config(value)
                            for value in (raw_rule.get("support_stores") or [])
                        )
                        if store is not None and store != Store.OFF
                    )
                    if "support_stores" in raw_rule
                    else None
                )
                removed_stores = tuple(
                    store for store in (
                        _store_from_config(value)
                        for value in (
                            raw_rule.get("remove_support_stores") or []
                        )
                    )
                    if store is not None and store != Store.OFF
                )
                if (
                    primary_store is not None
                    or normal_stores is not None
                    or support_stores is not None
                    or removed_stores
                ):
                    parsed_employees[str(employee_name)] = {
                        "primary_store": primary_store,
                        "normal_stores": normal_stores,
                        "support_stores": support_stores,
                        "remove_support_stores": removed_stores,
                    }
            if parsed_employees:
                overrides_parsed[ym] = parsed_employees
        MONTHLY_EMPLOYEE_STORE_OVERRIDES = overrides_parsed

    if "yamamoto_policy" in data:
        yamamoto_parsed: dict = {}
        for ym_text, raw_policy in dict(
            data["yamamoto_policy"] or {}
        ).items():
            ym = _parse_ym(ym_text)
            if ym is None or not isinstance(raw_policy, dict):
                continue
            try:
                max_days = int(raw_policy.get("max_days"))
                max_consecutive = int(
                    raw_policy.get("max_consecutive", 2)
                )
            except (TypeError, ValueError):
                continue
            if 0 <= max_days <= monthrange(*ym)[1] and 1 <= max_consecutive <= 7:
                yamamoto_parsed[ym] = {
                    "max_days": max_days,
                    "max_consecutive": max_consecutive,
                    "auto_only_if_needed": True,
                }
        MONTHLY_YAMAMOTO_POLICIES = yamamoto_parsed

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
        "tanaka_training": {
            f"{y:04d}-{m:02d}": {
                **dict(rule),
                "akabane_third_candidates": list(
                    rule.get("akabane_third_candidates", ())
                ),
            }
            for (y, m), rule in TANAKA_PAIR_TRAINING_RULES.items()
        },
        "training_plans": {
            f"{y:04d}-{m:02d}": [
                {
                    **{
                        key: value
                        for key, value in dict(plan).items()
                        if key not in {"phases", "approved_mentors"}
                    },
                    "approved_mentors": list(
                        plan.get("approved_mentors", ())
                    ),
                    "phases": [
                        {
                            **{
                                key: value
                                for key, value in dict(phase).items()
                                if key not in {"store", "mentors"}
                            },
                            "store": phase["store"].name,
                            "mentors": list(phase.get("mentors", ())),
                        }
                        for phase in plan.get("phases", ())
                    ],
                }
                for plan in plans
            ]
            for (y, m), plans in MONTHLY_TRAINING_PLANS.items()
        },
        "employee_store_overrides": {
            f"{y:04d}-{m:02d}": {
                name: {
                    "primary_store": (
                        rule["primary_store"].name
                        if rule.get("primary_store") is not None
                        else None
                    ),
                    **(
                        {
                            "normal_stores": [
                                store.name
                                for store in rule.get("normal_stores", ())
                            ]
                        }
                        if rule.get("normal_stores") is not None
                        else {}
                    ),
                    **(
                        {
                            "support_stores": [
                                store.name
                                for store in rule.get("support_stores", ())
                            ]
                        }
                        if rule.get("support_stores") is not None
                        else {}
                    ),
                    "remove_support_stores": [
                        store.name
                        for store in rule.get("remove_support_stores", ())
                    ],
                }
                for name, rule in employee_rules.items()
            }
            for (y, m), employee_rules
            in MONTHLY_EMPLOYEE_STORE_OVERRIDES.items()
        },
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
        "yamamoto_policy": {
            f"{y:04d}-{m:02d}": dict(policy)
            for (y, m), policy in MONTHLY_YAMAMOTO_POLICIES.items()
        },
    }


def save_monthly_exceptions(
    data: dict,
    actor: str = "管理者",
    action: str = "設定変更",
) -> tuple:
    """月例外設定を保存し、実行中のシステムへ即時反映する。

    Returns:
        (成功したか: bool, 状態メッセージ: str)
    """
    errors, _warnings = validate_monthly_exceptions_data(data)
    if errors:
        return False, " / ".join(errors)

    before = _load_monthly_exceptions() or {}
    before_snapshot = _clean_monthly_exceptions_snapshot(before)
    payload = _clean_monthly_exceptions_snapshot(data)
    payload["_説明"] = (
        "月限定の例外ルール。画面（⚙️ 設定 → 📅 月例外）から編集できます。"
        "書式は『YYYY-MM』。このファイルにキーがある場合、"
        "コード内のデフォルト値よりこちらが優先されます。"
    )
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["updated_by"] = str(actor or "管理者")
    local_history = list(before.get("_history", []) or [])
    incoming_history = list(dict(data or {}).get("_history", []) or [])
    # Streamlit再起動時にGitHub最新版を復元する場合は、リモート側の履歴も引き継ぐ。
    prior_history = (
        incoming_history
        if len(incoming_history) >= len(local_history)
        else local_history
    )
    if before_snapshot != _clean_monthly_exceptions_snapshot(payload):
        prior_history.append({
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "saved_at": payload["updated_at"],
            "actor": str(actor or "管理者"),
            "action": str(action or "設定変更"),
            "changed_sections": summarize_monthly_exceptions_change(
                before_snapshot, payload,
            ),
            "snapshot": before_snapshot,
        })
    payload["_history"] = prior_history[-MONTHLY_EXCEPTIONS_HISTORY_LIMIT:]
    try:
        MONTHLY_EXCEPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = MONTHLY_EXCEPTIONS_FILE.with_suffix(".json.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, MONTHLY_EXCEPTIONS_FILE)
    except Exception as exc:
        return False, f"保存失敗（{type(exc).__name__}: {exc}）"
    status = reload_monthly_exceptions()
    return True, status


def monthly_exceptions_history(limit: int = 10) -> list[dict]:
    """月例外設定に埋め込まれた直近の変更履歴を新しい順で返す。"""
    data = _load_monthly_exceptions() or {}
    entries = [
        dict(entry)
        for entry in list(data.get("_history", []) or [])
        if isinstance(entry, dict) and isinstance(entry.get("snapshot"), dict)
    ]
    entries.reverse()
    return entries[:max(1, int(limit))]


def restore_monthly_exceptions_history(
    history_id: str,
    actor: str = "管理者",
) -> tuple[bool, str]:
    """指定した変更履歴の直前状態へ戻す。復元操作自体も履歴に残す。"""
    for entry in monthly_exceptions_history(MONTHLY_EXCEPTIONS_HISTORY_LIMIT):
        if str(entry.get("id")) != str(history_id):
            continue
        return save_monthly_exceptions(
            dict(entry["snapshot"]),
            actor=actor,
            action=f"履歴から復元（{entry.get('saved_at', '')}の変更前）",
        )
    return False, "指定した履歴が見つかりません。"


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
        print(
            f"  {store.display_name}: エコ最低{cap.eco_min}名"
            f" + 合計最低{cap.eco_min + cap.ticket_min}名{closed}"
        )

    total_min = sum(c.eco_min + c.ticket_min for c in NORMAL_CAPACITY.values())
    print(f"\n  合計最小人数: {total_min}名/日（仕様書通り11名）")

    print("\n=== ハード制約 ===")
    for k, v in HARD_CONSTRAINTS.items():
        print(f"  {k}: {v}")

    print("\n=== 5月の個別休日日数 ===")
    for name, days in MAY_2026_HOLIDAY_OVERRIDES.items():
        print(f"  {name}: {days}日休")
