"""出勤自体は希望せず、出勤する場合だけ指定する店舗希望。"""
from __future__ import annotations

from calendar import monthrange

from .models import Store


def normalize_conditional_store_requests(requests, year, month, off_requests=None):
    result = []
    last_day = monthrange(year, month)[1]
    for name, day, store in requests or []:
        try:
            day = int(day)
            store = store if isinstance(store, Store) else Store[str(store)]
        except (ValueError, TypeError, KeyError):
            continue
        if not 1 <= day <= last_day or store == Store.OFF:
            continue
        if day in (off_requests or {}).get(name, []):
            continue
        item = (name, day, store)
        if item not in result:
            result.append(item)
    return result


def conditional_store_labels(requests):
    grouped = {}
    for day, store in requests:
        store = store if isinstance(store, Store) else Store[str(store)]
        grouped.setdefault(store, set()).add(int(day))
    return [
        f"出勤する場合の店舗希望: {'、'.join(f'{day}日' for day in sorted(days))}は"
        f"{store.display_name}（出勤自体の指定なし）"
        for store, days in grouped.items()
    ]


def conditional_store_penalties(x, requests, stores, weight=130):
    # 休みと希望店舗は同点。他店で働く場合だけ減点し、出勤を誘導しない。
    return [
        weight * sum(variable for store, variable in x[name][day].items()
                     if store in stores and store != preferred)
        for name, day, preferred in requests
        if name in x and day in x[name] and preferred in stores
    ]


def conditional_store_mismatches(shift, requests, off_requests=None):
    mismatches = []
    for name, day, preferred in normalize_conditional_store_requests(
        requests, shift.year, shift.month, off_requests,
    ):
        assignment = shift.get_assignment(name, day)
        if assignment and assignment.store not in (Store.OFF, preferred):
            mismatches.append((name, day, preferred, assignment.store))
    return mismatches
