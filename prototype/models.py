"""
大黒屋シフト管理システム - データモデル定義
================================================
シフト管理に必要なすべてのデータ型をここで定義します。
コードの他の部分はすべてここで定義した型を使うので、
変更する場合はこのファイルから始めてください。
"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ============================================================
# 店舗（5店舗）
# ============================================================

class Store(str, Enum):
    """店舗の識別子。値は確定シフト表で使う記号。"""
    AKABANE = "○"        # 赤羽駅前店
    HIGASHIGUCHI = "□"   # 赤羽東口店
    OMIYA = "△"          # 大宮駅前店
    NISHIGUCHI = "☆"     # 大宮西口店
    SUZURAN = "◆"        # 大宮すずらん通り店
    OFF = "×"            # 休み

    @property
    def display_name(self) -> str:
        return {
            Store.AKABANE: "赤羽駅前店",
            Store.HIGASHIGUCHI: "赤羽東口店",
            Store.OMIYA: "大宮駅前店",
            Store.NISHIGUCHI: "大宮西口店",
            Store.SUZURAN: "大宮すずらん通り店",
            Store.OFF: "休み",
        }[self]


# ============================================================
# スキル・役職
# ============================================================

class Skill(str, Enum):
    """対応可能な業務スキル"""
    ECO = "エコ"               # エコ・チケット両方対応可（店頭で買取応対できる）
    ECO_SUPPORT = "エコサポート"  # エコ業務の補助は可能だが、店頭直接応対はしない
    TICKET = "チケット"          # チケットのみ対応

    @property
    def can_handle_eco(self) -> bool:
        """エコ業務を担当できるか（チケット担当に該当しないか）"""
        return self in (Skill.ECO, Skill.ECO_SUPPORT)

    @property
    def can_be_eco_at_storefront(self) -> bool:
        """店頭でエコ要員として配置可能か（必須エコ要員に数えるか）"""
        # ECO_SUPPORT は店頭直接応対しないため、必須エコにはカウントしない
        return self == Skill.ECO


class Role(str, Enum):
    """組織上の役職"""
    REPRESENTATIVE = "代表取締役"        # システム管理者・シフト不参加
    ADVISOR = "顧問"                     # 非常勤取締役顧問・原則シフト不参加
    MANAGER = "店長"                     # 店舗の店長
    STAFF = "一般スタッフ"               # 一般従業員


class EmploymentStatus(str, Enum):
    """雇用形態（シフト稼働可否を決定）"""
    ACTIVE = "在籍"           # 通常勤務（正社員・店長）。年間目標日数あり
    PART_TIME = "パート"      # パート・アルバイト。年間目標日数なし、限定的稼働
    ADVISOR = "顧問"          # 非常勤・緊急時のみ稼働
    AUXILIARY = "補助"        # 山本さんなどの特別枠。シフトに数えない補助要員
    ON_LEAVE = "休職中"       # 一時的にシフト対象外（復帰予定あり）
    RETIRED = "退職"          # シフト対象外。データ保持のみ（履歴のため）

    @property
    def is_shift_eligible(self) -> bool:
        """通常のシフト生成対象か（生成ロジックに含めるか）"""
        return self in (
            EmploymentStatus.ACTIVE,
            EmploymentStatus.PART_TIME,
        )

    @property
    def is_archived(self) -> bool:
        """完全にシフト対象外（退職など）"""
        return self in (
            EmploymentStatus.RETIRED,
            EmploymentStatus.ON_LEAVE,
        )


class StationType(str, Enum):
    """配置タイプ"""
    FIXED = "固定"     # ホーム店舗専属
    FLEXIBLE = "流動"  # 複数店舗で稼働


# ============================================================
# 在勤要望（在勤割合）
# ============================================================

class Affinity(str, Enum):
    """店舗ごとの在勤要望度"""
    STRONG = "強"   # 6〜8割
    MEDIUM = "中"   # 3〜7割
    WEAK = "弱"     # 0〜3割
    NONE = "不可"   # 配置不可


# 在勤要望の割合（最小値, 最大値）
AFFINITY_RANGES: dict[Affinity, tuple[float, float]] = {
    Affinity.STRONG: (0.6, 0.8),
    Affinity.MEDIUM: (0.3, 0.7),
    Affinity.WEAK: (0.0, 0.3),
    Affinity.NONE: (0.0, 0.0),
}


# ============================================================
# 営業モード
# ============================================================

class OperationMode(str, Enum):
    """1日の営業モード（自動判定 or 手動上書き）"""
    NORMAL = "通常"       # 全5店舗、1日11名
    REDUCED = "省人員"    # 全5店舗、1日9〜10名（GW・お盆・SW等）
    MINIMUM = "最小営業"  # 赤羽駅前店・大宮駅前店のみ
    CLOSED = "営業停止"   # 12/31〜1/2


# ============================================================
# 従業員
# ============================================================

@dataclass
class Employee:
    """従業員1人を表すデータ"""
    name: str                                          # 表示名（板倉、楯など）
    full_name: Optional[str] = None                    # 正式氏名
    employee_id: Optional[str] = None                  # 従業員番号（"049"等）
    role: Role = Role.STAFF                            # 役職
    skill: Skill = Skill.TICKET                        # スキル
    home_store: Optional[Store] = None                 # ホーム店舗（固定配置の場合）
    station_type: StationType = StationType.FLEXIBLE   # 配置タイプ
    can_substitute_at: list[Store] = field(default_factory=list)  # 代行担当可能な1名体制店舗
    affinities: dict[Store, Affinity] = field(default_factory=dict)  # 店舗ごとの在勤要望
    annual_target_days: Optional[int] = None           # 年間基準出勤日数
    notes: str = ""                                    # 備考

    # 雇用形態（シフト稼働の可否を決定）
    employment_status: EmploymentStatus = EmploymentStatus.ACTIVE
    hired_at: Optional[str] = None                     # 入社日（ISO形式 YYYY-MM-DD）
    retired_at: Optional[str] = None                   # 退職日（退職済の場合）
    status_changed_at: Optional[str] = None            # 状態最終更新日

    # シフト計算上の特殊フラグ
    constraint_check_excluded: bool = False  # 制約チェック対象外
    is_auxiliary: bool = False               # 補助要員（カウント対象外）
    only_on_request_days: bool = False       # 出勤希望日のみ稼働

    def __repr__(self) -> str:
        return f"Employee({self.name})"

    @property
    def is_shift_eligible(self) -> bool:
        """シフト生成対象か（雇用形態と補助要員フラグで判定）"""
        if self.is_auxiliary:
            return False
        if self.role == Role.REPRESENTATIVE:
            return False
        return self.employment_status.is_shift_eligible


# ============================================================
# 希望提出
# ============================================================

class PreferenceMark(str, Enum):
    """カレンダー上の希望記号"""
    AVAILABLE = "○"         # 出勤可
    OFF_REQUEST = "×"       # 休み希望（絶対）
    PREFER_OFF = "△"        # できれば休み（優先度低）
    REQUEST_AKABANE = "○赤羽"  # 「6赤羽」のような店舗指定付き出勤希望
    REQUEST_SUZURAN = "○すずらん"


@dataclass
class DayPreference:
    """ある日についての従業員の希望"""
    employee: str          # 従業員名
    day: int               # 日付（月内の何日か）
    mark: PreferenceMark   # 希望記号
    note: str = ""         # 備考（自然言語）


# ============================================================
# シフトの確定状態
# ============================================================

@dataclass
class ShiftAssignment:
    """ある日の従業員1人の配属"""
    employee: str
    day: int
    store: Store           # 配属店舗 or 休み
    is_paid_leave: bool = False  # 有給休暇消化日


@dataclass
class MonthlyShift:
    """1ヶ月分のシフト全体"""
    year: int
    month: int
    assignments: list[ShiftAssignment] = field(default_factory=list)
    operation_modes: dict[int, OperationMode] = field(default_factory=dict)  # 日 → モード

    def get_assignment(self, employee: str, day: int) -> Optional[ShiftAssignment]:
        for a in self.assignments:
            if a.employee == employee and a.day == day:
                return a
        return None

    def get_day_assignments(self, day: int) -> list[ShiftAssignment]:
        return [a for a in self.assignments if a.day == day]


# ============================================================
# 店舗別の必要人数（モード別）
# ============================================================

@dataclass
class StoreRequirement:
    """店舗の1日の必要人数（モードごとに変動）"""
    store: Store
    eco_required: int          # 必要なエコ要員数
    ticket_required: int       # 必要なチケット要員数
    eco_max: Optional[int] = None      # エコ最大数（東口の月3回エコ2など）
    closed_days_of_week: list[int] = field(default_factory=list)  # 休店曜日（0=月）

    @property
    def total_required(self) -> int:
        return self.eco_required + self.ticket_required


# ============================================================
# 個別ルール（休日日数の特例など）
# ============================================================

@dataclass
class MonthlyHolidayException:
    """ある月の特定従業員の休日日数指定"""
    employee: str
    holiday_days: int  # その月に必要な休日日数


@dataclass
class PreviousMonthCarryover:
    """前月末の連勤情報（連勤持ち越しチェック用）"""
    employee: str
    last_working_days: list[int]   # 前月の最後の出勤日（例：[28,29,30]）
    last_off_days: list[int]       # 前月末の休み（例：[30]）
