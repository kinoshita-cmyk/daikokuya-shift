"""
2026年5月の入力データ
================================================
データソース: /data/rules_2026_05.txt の「■6. 前月影響」「■7. 出勤・休日希望」

このファイルは「ある月の入力データ」のサンプルです。
本番システムでは、このような形式のデータが各月、希望提出フォームから集まります。
"""

from typing import Optional
from .models import PreviousMonthCarryover, Store


# ============================================================
# 4月末の出勤状況（連勤持ち越しチェック用）
# ============================================================

# 4月末出勤者（5/1から起算した連勤計算で考慮）
PREVIOUS_MONTH_CARRYOVER: list[PreviousMonthCarryover] = [
    PreviousMonthCarryover(employee="板倉", last_working_days=[30], last_off_days=[]),
    PreviousMonthCarryover(employee="今津", last_working_days=[29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="鈴木", last_working_days=[28, 29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="岩野", last_working_days=[29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="大塚", last_working_days=[30], last_off_days=[]),
    PreviousMonthCarryover(employee="黒澤", last_working_days=[29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="春山", last_working_days=[30], last_off_days=[]),
    PreviousMonthCarryover(employee="下地", last_working_days=[28, 29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="野澤", last_working_days=[27, 28, 29, 30], last_off_days=[]),
    PreviousMonthCarryover(employee="下田", last_working_days=[30], last_off_days=[]),
    PreviousMonthCarryover(employee="楯", last_working_days=[30], last_off_days=[]),
    PreviousMonthCarryover(employee="土井", last_working_days=[28, 29, 30], last_off_days=[]),
    # 4月末が休みだった人
    PreviousMonthCarryover(employee="田中", last_working_days=[], last_off_days=[30]),
    PreviousMonthCarryover(employee="牧野", last_working_days=[], last_off_days=[30]),
    PreviousMonthCarryover(employee="大類", last_working_days=[], last_off_days=[30]),
    PreviousMonthCarryover(employee="長尾", last_working_days=[], last_off_days=[30]),
]


# ============================================================
# 5月の出勤希望日（特定店舗指定あり）
# ============================================================

# (従業員名, 日付, 指定店舗 or None) のリスト
# Noneの場合は通常の出勤希望（任意店舗）
WORK_REQUESTS: list[tuple[str, int, Optional[Store]]] = [
    ("田中", 6, Store.AKABANE),    # 「6赤羽」
    ("野澤", 1, Store.SUZURAN),    # 「1すずらん」※5連勤になるが今月のみ許容
    ("南", 3, None),
    ("南", 4, None),
    ("南", 14, None),
    ("南", 15, None),
    ("南", 18, None),
    ("南", 20, None),
    ("南", 25, None),
    ("南", 26, None),
    ("南", 29, None),
]

# 特例：野澤は5/1出勤希望だが、4/27〜4/30も出勤しているため
# 5/1まで含めると5連勤になる。今月のみ許容。
SPECIAL_EXCEPTIONS: list[tuple[str, str]] = [
    ("野澤", "5/1出勤希望のため5連勤許容（前月4/27〜4/30出勤）"),
]


# ============================================================
# 5月の休み希望日
# ============================================================

OFF_REQUESTS: dict[str, list[int]] = {
    "山本": [6, 15, 18, 20, 23, 27],
    "今津": [12, 20, 26],
    "鈴木": [16, 23, 28],
    "楯": [5, 6, 9, 11],  # 加えて 16or17, 23or24, 30or31 のうち各1日休み
    "牧野": [3, 4, 13, 14, 23],
    "春山": [],
    "下地": [1, 7, 10, 15, 19],
    "長尾": [1, 3, 4, 5, 6, 16, 17, 30],
    "土井": [3, 4, 5, 6, 11, 18, 19, 25],
    "板倉": [10, 16, 20, 24],
    "田中": [1, 7, 11, 12],
    "岩野": [1, 5, 16, 28, 29, 30],
    "大類": [13, 20, 27],
    "黒澤": [23, 30],
    "野澤": [2, 5, 13],
    "下田": [],
    "南": [],
    "大塚": [1, 2, 3, 4, 5, 10, 17, 18, 19, 20, 21, 22, 23, 24, 31],  # 「1〜5,10,17〜24,31」
}


# ============================================================
# 楯さんの「どちらか1日休み」指定（柔軟休み希望）
# ============================================================

# (従業員名, [候補日のリスト, 必須休み日数])
FLEXIBLE_OFF_REQUESTS: list[tuple[str, list[int], int]] = [
    ("楯", [16, 17], 1),    # 16,17どちらか1日休み
    ("楯", [23, 24], 1),    # 23,24どちらか1日休み
    ("楯", [30, 31], 1),    # 30,31どちらか1日休み
]


# ============================================================
# その他のメモ（自然言語的な特記事項）
# ============================================================

NOTES: dict[str, str] = {
    "野澤": "5/1すずらん希望。4/27〜30出勤しているため5連勤になるが今月のみ許容。",
    "大塚": "5月は10日間在勤予定。連勤チェックは適用するが、休日日数チェックなどは除外。",
    "全体": "5月はGW分の出勤を考慮し、全体的に休みを増やしている。",
}


# ============================================================
# 確認・動作テスト
# ============================================================

if __name__ == "__main__":
    print("=== 4月末持ち越し ===")
    print(f"  出勤者: {sum(1 for p in PREVIOUS_MONTH_CARRYOVER if p.last_working_days)}名")
    print(f"  休み者: {sum(1 for p in PREVIOUS_MONTH_CARRYOVER if p.last_off_days)}名")

    print("\n=== 5月の希望提出状況 ===")
    print(f"  出勤希望: {len(WORK_REQUESTS)}件")
    for name, day, store in WORK_REQUESTS:
        store_str = f" → {store.display_name}" if store else ""
        print(f"    {name} {day}日{store_str}")

    print(f"\n  休み希望: {sum(len(d) for d in OFF_REQUESTS.values())}件")
    for name, days in OFF_REQUESTS.items():
        if days:
            print(f"    {name}: {days}")

    print(f"\n  柔軟希望: {len(FLEXIBLE_OFF_REQUESTS)}件")
    for name, days, n in FLEXIBLE_OFF_REQUESTS:
        print(f"    {name}: {days}のうち{n}日休み")
