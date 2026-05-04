"""
Validator動作テスト
================================================
小さなサンプルシフトを構築し、Validator が制約違反を正しく検出できるか確認する。

これは Validator のロジック検証用のテストです。
実際の 5月確定版シフト（写真）を取り込むためには、Excel データが必要です。
"""

from .models import MonthlyShift, ShiftAssignment, Store, OperationMode
from .validator import validate
from .may_2026_data import OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER
from .rules import MAY_2026_HOLIDAY_OVERRIDES


def make_minimal_test_shift() -> MonthlyShift:
    """
    最小限のテストシフト（5/1のみ、わざと違反を含む）

    意図的に以下の違反を入れる:
    - 赤羽駅前店: エコ1, チケット1（チケット不足）
    - 大宮駅前店: 春山も下地もいない
    - 東口に楯（5/1は金曜なので休店ではないからOKなはず）
    - 5/1 下地は休み希望なのに出勤している
    """
    shift = MonthlyShift(year=2026, month=5)
    shift.assignments = [
        # 5/1（金）の配置例（わざと不完全）
        ShiftAssignment(employee="今津", day=1, store=Store.AKABANE),       # エコ
        ShiftAssignment(employee="板倉", day=1, store=Store.AKABANE),       # チケット
        ShiftAssignment(employee="土井", day=1, store=Store.HIGASHIGUCHI),  # エコ
        ShiftAssignment(employee="楯", day=1, store=Store.NISHIGUCHI),      # エコ
        ShiftAssignment(employee="長尾", day=1, store=Store.SUZURAN),       # エコ
        ShiftAssignment(employee="野澤", day=1, store=Store.SUZURAN),       # チケット（出勤希望）
        ShiftAssignment(employee="岩野", day=1, store=Store.SUZURAN),       # チケット（補填）
        ShiftAssignment(employee="牧野", day=1, store=Store.OMIYA),         # エコ
        ShiftAssignment(employee="鈴木", day=1, store=Store.OMIYA),         # エコ
        ShiftAssignment(employee="黒澤", day=1, store=Store.OMIYA),         # チケット
        # 下地は休み希望（OFF_REQUESTS で 1日あり）なのに、わざと出勤させる
        ShiftAssignment(employee="下地", day=1, store=Store.OMIYA),         # 違反！

        # 残りの人は休み（assignment 無し = 休扱い）
    ]
    shift.operation_modes = {1: OperationMode.NORMAL}
    return shift


def main():
    print("【Validator 動作テスト】\n")
    shift = make_minimal_test_shift()

    print("テストシフト 5/1:")
    for a in shift.get_day_assignments(1):
        print(f"  {a.employee:6s} → {a.store.display_name}")
    print()

    result = validate(
        shift=shift,
        work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
    )

    # 5/1のみの問題に絞って表示（休日日数等は1日だけだと意味がないので）
    day1_issues = [i for i in result.issues if i.day == 1]
    print(f"5/1 の検出問題: {len(day1_issues)}件\n")
    for issue in day1_issues:
        print(f"  {issue}")

    print(f"\n（注：1日だけのテストなので、月内合計の休日日数違反等も{result.error_count - len(day1_issues)}件出ますが正常です）")

    # 経営側可視化用のサマリー表示
    print("\n" + "=" * 60)
    print("【経営側可視化サマリー（一部抜粋）】")
    print("=" * 60)
    # 全体サマリーと一部の従業員のみ表示
    important_keys = ["📊 月間総出勤日数", "📋 目標未達のメンバー", "⚠ 人数不足日"]
    for key in important_keys:
        if key in result.summary_stats:
            print(f"  {key}: {result.summary_stats[key]}")


if __name__ == "__main__":
    main()
