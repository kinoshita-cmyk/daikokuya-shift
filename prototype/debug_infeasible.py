"""
INFEASIBLE 原因特定のためのデバッグスクリプト
================================================
制約を1つずつ無効化して試し、どの制約が問題か特定する。
"""

from .generator import generate_shift, determine_operation_modes
from .may_2026_data import (
    OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
)
from .rules import MAY_2026_HOLIDAY_OVERRIDES


def main():
    print("=== 制約を緩めて試す ===\n")

    # テスト1: 休日日数最低ライン無効化
    print("[Test 1] 休日日数最低ライン無効化（default 0、override無し）")
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS,
        work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides={},
        default_holidays=0,
        consec_exceptions=["野澤"],
        verbose=True,
    )
    if shift:
        print("  → ✅ 解あり！休日日数の最低ラインが厳しすぎる可能性")
    else:
        print("  → ❌ まだ INFEASIBLE")

    # テスト2: 出勤希望なし
    print("\n[Test 2] 出勤希望なし")
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS,
        work_requests=[],
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        consec_exceptions=["野澤"],
        verbose=True,
    )
    if shift:
        print("  → ✅ 解あり！出勤希望が問題")
    else:
        print("  → ❌ まだ INFEASIBLE")

    # テスト3: 前月持ち越しなし
    print("\n[Test 3] 前月持ち越しなし")
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS,
        work_requests=WORK_REQUESTS,
        prev_month=[],
        flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        consec_exceptions=["野澤"],
        verbose=True,
    )
    if shift:
        print("  → ✅ 解あり！前月持ち越しが問題")
    else:
        print("  → ❌ まだ INFEASIBLE")

    # テスト4: 休み希望なし
    print("\n[Test 4] 休み希望なし")
    shift = generate_shift(
        year=2026, month=5,
        off_requests={},
        work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        flexible_off=[],
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        consec_exceptions=["野澤"],
        verbose=True,
    )
    if shift:
        print("  → ✅ 解あり！休み希望が問題")
    else:
        print("  → ❌ まだ INFEASIBLE")

    # テスト5: 営業モード全て通常
    print("\n[Test 5] 営業モード強制 通常 (5/1-5 を REDUCED から NORMAL に)")
    from .models import OperationMode
    normal_modes = {d: OperationMode.NORMAL for d in range(1, 32)}
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS,
        work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        operation_modes=normal_modes,
        consec_exceptions=["野澤"],
        verbose=True,
    )
    if shift:
        print("  → ✅ 解あり！通常モードなら大丈夫")
    else:
        print("  → ❌ まだ INFEASIBLE（通常モードの方が要員が必要なのに...）")


if __name__ == "__main__":
    main()
