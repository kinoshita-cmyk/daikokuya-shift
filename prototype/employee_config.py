"""
従業員マスタの動的管理（追加・変更・退職処理）
================================================
employees.py のハードコード値を「初期値」として、JSON でオーバーライドする仕組み。

操作可能な変更:
- 雇用形態の変更（正社員 → パート、退職、休職など）
- 新入社員の追加
- 既存従業員のフィールド更新（年間目標日数・スキル・店舗適性など）
- 完全削除（推奨せず、RETIRED 状態にする）

ファイル構造:
  /Users/kinoshitayoshihide/daikokuya-shift/config/
    employees.json         ← 現在のアクティブ設定
    employee_history.jsonl ← 変更履歴（追記専用）

使い方:
    from prototype.employee_config import get_active_employees
    employees = get_active_employees()  # オーバーライド適用済み
"""

from __future__ import annotations
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from .models import Employee, Skill, Role, Store, StationType, Affinity, EmploymentStatus
from . import employees as _default_employees
from .paths import CONFIG_DIR

EMPLOYEE_CONFIG_FILE = CONFIG_DIR / "employees.json"
EMPLOYEE_HISTORY_FILE = CONFIG_DIR / "employee_history.jsonl"


def _employment_status_from_value(value: str | None) -> EmploymentStatus:
    """保存済みの旧表記も含めて雇用形態を復元する。"""
    aliases = {
        "在籍": EmploymentStatus.ACTIVE,
        "ACTIVE": EmploymentStatus.ACTIVE,
        "PART_TIME": EmploymentStatus.PART_TIME,
        "ADVISOR": EmploymentStatus.ADVISOR,
        "AUXILIARY": EmploymentStatus.AUXILIARY,
        "ON_LEAVE": EmploymentStatus.ON_LEAVE,
        "RETIRED": EmploymentStatus.RETIRED,
    }
    if value in aliases:
        return aliases[value]
    return _enum_from_value(EmploymentStatus, value, EmploymentStatus.ACTIVE)


def _enum_from_value(enum_cls, value, default):
    """Enum の name/value どちらで保存されていても復元する。"""
    if value is None or value == "":
        return default
    if isinstance(value, enum_cls):
        return value
    value_str = str(value)
    try:
        return enum_cls(value_str)
    except ValueError:
        pass
    try:
        return enum_cls[value_str]
    except KeyError:
        return default


def _skill_from_value(value: str | None) -> Skill:
    """スキル欄の表記ゆれを吸収する。"""
    aliases = {
        "ECO": Skill.ECO,
        "ECO_SUPPORT": Skill.ECO_SUPPORT,
        "TICKET": Skill.TICKET,
        "エコ": Skill.ECO,
        "エコサポート": Skill.ECO_SUPPORT,
        "チケット": Skill.TICKET,
    }
    if value in aliases:
        return aliases[value]
    return _enum_from_value(Skill, value, Skill.TICKET)


def _store_from_value(value: str | None) -> Optional[Store]:
    """ホーム店舗の name/value/display_name 表記を吸収する。"""
    if not value:
        return None
    value_str = str(value)
    aliases = {
        "AKABANE": Store.AKABANE,
        "HIGASHIGUCHI": Store.HIGASHIGUCHI,
        "OMIYA": Store.OMIYA,
        "NISHIGUCHI": Store.NISHIGUCHI,
        "SUZURAN": Store.SUZURAN,
        "OFF": Store.OFF,
        "○": Store.AKABANE,
        "〇": Store.AKABANE,
        "□": Store.HIGASHIGUCHI,
        "△": Store.OMIYA,
        "☆": Store.NISHIGUCHI,
        "◆": Store.SUZURAN,
        "×": Store.OFF,
        "赤羽": Store.AKABANE,
        "赤羽駅前": Store.AKABANE,
        "赤羽駅前店": Store.AKABANE,
        "東口": Store.HIGASHIGUCHI,
        "赤羽東口": Store.HIGASHIGUCHI,
        "赤羽東口店": Store.HIGASHIGUCHI,
        "大宮": Store.OMIYA,
        "大宮駅前": Store.OMIYA,
        "大宮駅前店": Store.OMIYA,
        "西口": Store.NISHIGUCHI,
        "大宮西口": Store.NISHIGUCHI,
        "大宮西口店": Store.NISHIGUCHI,
        "すずらん": Store.SUZURAN,
        "すずらん通り": Store.SUZURAN,
        "大宮すずらん通り": Store.SUZURAN,
        "大宮すずらん通り店": Store.SUZURAN,
        "休み": Store.OFF,
    }
    return aliases.get(value_str)


# ============================================================
# シリアライズ・デシリアライズ
# ============================================================

def employee_to_dict(emp: Employee) -> dict:
    """Employeeを JSON 保存用 dict に変換"""
    return {
        "name": emp.name,
        "full_name": emp.full_name,
        "employee_id": emp.employee_id,
        "role": emp.role.value if emp.role else None,
        "skill": emp.skill.value if emp.skill else None,
        "home_store": emp.home_store.name if emp.home_store else None,
        "station_type": emp.station_type.value if emp.station_type else None,
        "affinities": {
            store.name: aff.value for store, aff in emp.affinities.items()
        },
        "annual_target_days": emp.annual_target_days,
        "notes": emp.notes,
        "employment_status": emp.employment_status.value,
        "hired_at": emp.hired_at,
        "retired_at": emp.retired_at,
        "status_changed_at": emp.status_changed_at,
        "constraint_check_excluded": emp.constraint_check_excluded,
        "is_auxiliary": emp.is_auxiliary,
        "only_on_request_days": emp.only_on_request_days,
    }


def employee_from_dict(data: dict) -> Employee:
    """JSON dict から Employee を復元"""
    affinities = {}
    for store_name, aff_value in data.get("affinities", {}).items():
        store = _store_from_value(store_name)
        if store is None or store == Store.OFF:
            continue
        affinities[store] = _enum_from_value(Affinity, aff_value, Affinity.NONE)
    return Employee(
        name=data["name"],
        full_name=data.get("full_name"),
        employee_id=data.get("employee_id"),
        role=_enum_from_value(Role, data.get("role"), Role.STAFF),
        skill=_skill_from_value(data.get("skill")),
        home_store=_store_from_value(data.get("home_store")),
        station_type=_enum_from_value(
            StationType, data.get("station_type"), StationType.FLEXIBLE,
        ),
        affinities=affinities,
        annual_target_days=data.get("annual_target_days"),
        notes=data.get("notes", ""),
        employment_status=_employment_status_from_value(data.get("employment_status")),
        hired_at=data.get("hired_at"),
        retired_at=data.get("retired_at"),
        status_changed_at=data.get("status_changed_at"),
        constraint_check_excluded=data.get("constraint_check_excluded", False),
        is_auxiliary=data.get("is_auxiliary", False),
        only_on_request_days=data.get("only_on_request_days", False),
    )


# ============================================================
# マネージャクラス
# ============================================================

class EmployeeConfigManager:
    """従業員マスタの読み書き・履歴管理"""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "employees.json"
        self.history_file = self.config_dir / "employee_history.jsonl"

    # ============================================================
    # 読み込み
    # ============================================================

    def load_all(self) -> list[Employee]:
        """
        全従業員リストを返す。
        優先順位:
          1. JSONファイルがあればそれを使う
          2. ない場合は employees.py のデフォルトを使う
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, encoding="utf-8") as f:
                    data = json.load(f)
                return [employee_from_dict(d) for d in data.get("employees", [])]
            except (json.JSONDecodeError, OSError):
                pass
        # デフォルトを返す
        return list(_default_employees.ALL_EMPLOYEES)

    def initialize_from_default(self, actor: str = "system") -> Path:
        """employees.py のデフォルトを JSON ファイルとして初期化"""
        return self._save_all(
            list(_default_employees.ALL_EMPLOYEES),
            actor=actor,
            note="初期化（employees.pyから）",
        )

    # ============================================================
    # 書き込み
    # ============================================================

    def _save_all(
        self, employees: list[Employee], actor: str, note: str = "",
    ) -> Path:
        """全従業員リストを保存（内部）"""
        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "updated_by": actor,
            "employees": [employee_to_dict(e) for e in employees],
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return self.config_file

    def add_employee(
        self, new_emp: Employee, actor: str, note: str = "",
    ) -> bool:
        """新入社員を追加"""
        all_emps = self.load_all()
        # 同名チェック
        if any(e.name == new_emp.name for e in all_emps):
            return False
        new_emp.hired_at = new_emp.hired_at or datetime.now().date().isoformat()
        new_emp.status_changed_at = datetime.now().isoformat()
        all_emps.append(new_emp)
        self._save_all(all_emps, actor, note)
        self._log_change(
            action="add", target=new_emp.name,
            after=employee_to_dict(new_emp),
            actor=actor, note=note,
        )
        return True

    def update_employee(
        self, name: str, updates: dict, actor: str, note: str = "",
    ) -> bool:
        """既存従業員のフィールドを更新"""
        all_emps = self.load_all()
        idx = next((i for i, e in enumerate(all_emps) if e.name == name), -1)
        if idx == -1:
            return False
        before = employee_to_dict(all_emps[idx])
        # フィールドを更新
        for k, v in updates.items():
            if k == "employment_status":
                if isinstance(v, str):
                    v = _employment_status_from_value(v)
                # 退職処理の場合は退職日を記録
                if v == EmploymentStatus.RETIRED and not all_emps[idx].retired_at:
                    all_emps[idx].retired_at = datetime.now().date().isoformat()
                # 状態変更時刻を更新
                all_emps[idx].status_changed_at = datetime.now().isoformat()
            elif k == "role" and isinstance(v, str):
                v = Role(v)
            elif k == "skill" and isinstance(v, str):
                v = Skill(v)
            elif k == "home_store" and isinstance(v, str):
                v = _store_from_value(v)
            elif k == "station_type" and isinstance(v, str):
                v = _enum_from_value(StationType, v, StationType.FLEXIBLE)
            elif k == "affinities" and isinstance(v, dict):
                aff_dict = {}
                for store_name, aff_value in v.items():
                    store = _store_from_value(store_name)
                    if store is None or store == Store.OFF:
                        continue
                    aff_dict[store] = _enum_from_value(Affinity, aff_value, Affinity.NONE)
                v = aff_dict
            setattr(all_emps[idx], k, v)
        after = employee_to_dict(all_emps[idx])
        self._save_all(all_emps, actor, note)
        self._log_change(
            action="update", target=name,
            before=before, after=after,
            actor=actor, note=note,
        )
        return True

    def retire_employee(
        self, name: str, actor: str, retired_date: Optional[str] = None,
        note: str = "",
    ) -> bool:
        """退職処理（雇用形態を RETIRED にする。データは残す）"""
        return self.update_employee(
            name=name,
            updates={
                "employment_status": EmploymentStatus.RETIRED,
                "retired_at": retired_date or datetime.now().date().isoformat(),
            },
            actor=actor,
            note=note or f"退職処理（{retired_date or '本日付'}）",
        )

    def change_status(
        self, name: str, new_status: EmploymentStatus, actor: str, note: str = "",
    ) -> bool:
        """雇用形態の変更（パート転換など）"""
        return self.update_employee(
            name=name,
            updates={"employment_status": new_status},
            actor=actor,
            note=note or f"雇用形態変更: {new_status.value}",
        )

    def remove_employee(
        self, name: str, actor: str, note: str = "",
    ) -> bool:
        """従業員を完全に削除（非推奨。通常は退職処理を使う）"""
        all_emps = self.load_all()
        idx = next((i for i, e in enumerate(all_emps) if e.name == name), -1)
        if idx == -1:
            return False
        before = employee_to_dict(all_emps[idx])
        all_emps.pop(idx)
        self._save_all(all_emps, actor, note)
        self._log_change(
            action="remove", target=name,
            before=before,
            actor=actor, note=note,
        )
        return True

    # ============================================================
    # 履歴
    # ============================================================

    def _log_change(
        self, action: str, target: str, actor: str, note: str = "",
        before: Any = None, after: Any = None,
    ) -> None:
        """変更履歴を1件記録"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "actor": actor,
            "action": action,  # "add" / "update" / "remove"
            "target": target,
            "before": before,
            "after": after,
            "note": note,
        }
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_history(self, limit: int = 100) -> list[dict]:
        """変更履歴を取得（新しい順）"""
        if not self.history_file.exists():
            return []
        history = []
        with open(self.history_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    history.append(json.loads(line))
        return list(reversed(history))[:limit]


# ============================================================
# 公開API
# ============================================================

_global_mgr: Optional[EmployeeConfigManager] = None


def get_manager() -> EmployeeConfigManager:
    """マネージャのシングルトン取得"""
    global _global_mgr
    if _global_mgr is None:
        _global_mgr = EmployeeConfigManager()
    return _global_mgr


def get_active_employees() -> list[Employee]:
    """
    現在アクティブな従業員リストを取得（雇用形態を考慮）。
    退職者は除外、シフト対象外も除外。
    """
    mgr = get_manager()
    all_emps = mgr.load_all()
    return [e for e in all_emps if e.is_shift_eligible]


def get_all_employees_including_retired() -> list[Employee]:
    """退職者・休職者を含む全従業員リスト"""
    mgr = get_manager()
    return mgr.load_all()


def get_active_for_shift_generation() -> list[Employee]:
    """
    シフト生成エンジンが使うべき従業員リスト。
    - 退職者は完全除外
    - 顧問は除外（緊急時のみ手動で追加）
    - パートタイマーと一般正社員は含む
    """
    mgr = get_manager()
    all_emps = mgr.load_all()
    return [
        e for e in all_emps
        if e.employment_status.is_shift_eligible
        and not e.is_auxiliary
        and e.role != Role.REPRESENTATIVE
        and e.role != Role.ADVISOR
    ]


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【EmployeeConfigManager 動作テスト】\n")

    mgr = EmployeeConfigManager()

    print(f"[1] 現在の従業員数（デフォルト or 設定ファイル）...")
    emps = mgr.load_all()
    print(f"  → {len(emps)}名")
    for e in emps[:3]:
        print(f"    - {e.name} / {e.role.value} / {e.employment_status.value}")
    print(f"    ... ほか{len(emps) - 3}名")

    print(f"\n[2] アクティブな従業員（シフト対象）...")
    active = get_active_employees()
    print(f"  → {len(active)}名")

    print(f"\n[3] シフト生成対象（退職・顧問・補助除外）...")
    gen = get_active_for_shift_generation()
    print(f"  → {len(gen)}名")
    print(f"    エコ担当: {sum(1 for e in gen if e.skill == Skill.ECO)}名")
    print(f"    チケット担当: {sum(1 for e in gen if e.skill == Skill.TICKET)}名")

    print("\n✅ EmployeeConfigManager 動作確認完了")
