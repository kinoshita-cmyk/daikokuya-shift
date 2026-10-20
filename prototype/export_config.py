"""
画面・Excel・PDFで共有する軽量な出力設定。

重い Excel/PDF ライブラリを読み込まずに、画面側で列順だけ参照できるよう
独立させている。
"""

from __future__ import annotations


EXPORT_COLUMN_ORDER = [
    "山本", "板倉", "今津", "鈴木", "田中", "岩野", "大塚", "南",
    "黒澤", "牧野", "春山", "下地", "大類", "長尾", "野澤",
    "楯", "土井", "顧問",
]


def export_column_order(year: int, month: int, assignments=()) -> list[str]:
    """退職後の空列は出さず、保存済みシフトの実データは隠さない。"""
    from .employees import get_employee, employee_is_in_service

    recorded = {a.employee for a in assignments}
    names = []
    for name in EXPORT_COLUMN_ORDER:
        try:
            active = employee_is_in_service(get_employee(name), year, month)
        except KeyError:
            active = False
        if active or name in recorded:
            names.append(name)
    names.extend(sorted(recorded - set(names)))
    return names
