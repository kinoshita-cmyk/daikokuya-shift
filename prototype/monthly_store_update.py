"""承認された2026年10月の店舗区分を、一度だけ既存設定へ追加する。"""
from copy import deepcopy

from prototype.models import Affinity, Store


UPDATE_ID = "2026-10-store-categories-20260925"
TARGET_MONTH = "2026-10"
STORE_CHANGES = {
    "鈴木": (Store.SUZURAN, (Store.AKABANE,)),
    "田中": (Store.SUZURAN, (Store.AKABANE,)),
    "牧野": (Store.AKABANE, (Store.SUZURAN, Store.OMIYA)),
}


def push_verified_monthly_settings(data):
    """履歴だけでなく、再起動で取得するlatestにも保存できたか確認する。"""
    from prototype.github_backup import push_config_to_github, fetch_config_from_github
    pushed, message = push_config_to_github("monthly_exceptions", data)
    if not pushed:
        return False, message
    loaded, remote, _ = fetch_config_from_github("monthly_exceptions")
    if not loaded or remote != data:
        return False, "起動時に使う最新版への保存を確認できませんでした。再保存してください。"
    return True, message


def prepare_store_update(data, employees):
    """他の月・スタッフ・未指定の応援区分を維持。適用済みなら再追加しない。"""
    if UPDATE_ID in data.get("applied_updates", []):
        return deepcopy(data), False
    result = deepcopy(data)
    overrides = result.setdefault("employee_store_overrides", {}).setdefault(TARGET_MONTH, {})
    by_name = {employee.name: employee for employee in employees}
    for name, (primary, normal) in STORE_CHANGES.items():
        employee = by_name.get(name)
        if employee is None:
            raise ValueError(f"10月の店舗区分を追加できません: {name}が従業員マスタにいません")
        if any(employee.affinities.get(store, Affinity.NONE) == Affinity.NONE
               for store in (primary, *normal)):
            raise ValueError(f"10月の店舗区分を追加できません: {name}の絶対配置不可と競合しています")
        previous = overrides.get(name, {})
        assigned = {store.name for store in (primary, *normal)}
        support = previous.get("support_stores")
        if support is None:
            support = [store.name for store, affinity in employee.affinities.items()
                       if affinity == Affinity.WEAK]
        removed = list(previous.get("remove_support_stores") or [])
        overrides[name] = {
            **previous,
            "primary_store": primary.name,
            "normal_stores": [store.name for store in normal],
            "support_stores": [store for store in support if store not in assigned and store not in removed],
            "remove_support_stores": [store for store in removed if store not in assigned],
        }
    result.setdefault("applied_updates", []).append(UPDATE_ID)
    return result, True
