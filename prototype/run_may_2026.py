"""
2026年5月のシフトを自動生成するエンドツーエンドのテスト
================================================
1. 5月の希望データを読み込み
2. Generator で自動シフトを生成
3. Validator で結果を検証
4. 結果を表示

実行: python3 -m prototype.run_may_2026
"""

import time

from .generator import generate_shift, determine_operation_modes
from .validator import validate
from .may_2026_data import (
    OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
)
from .rules import MAY_2026_HOLIDAY_OVERRIDES
from .models import Store
from .employees import ALL_EMPLOYEES


def print_shift_table(shift, max_employees: int = 20):
    """シフト表を見やすく出力（5月確定版と同じレイアウト）"""
    if shift is None:
        print("シフトが生成されませんでした")
        return

    employees = [e.name for e in ALL_EMPLOYEES if e.role.value != "顧問"]

    # ヘッダー
    header = "日  曜 |"
    for name in employees:
        header += f" {name:^4}|"
    print(header)
    print("-" * len(header))

    # 各日
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    from datetime import date
    days_in_month = 31 if shift.month == 5 else 30
    for d in range(1, days_in_month + 1):
        wd = weekday_jp[date(shift.year, shift.month, d).weekday()]
        row = f"{d:2d} ({wd}) |"
        for name in employees:
            a = shift.get_assignment(name, d)
            if a is None:
                row += "  ・  |"  # 未配置（山本が空白の場合等）
            else:
                row += f"  {a.store.value}   |"
        print(row)


def main():
    print("=" * 70)
    print("【2026年5月 シフト自動生成テスト】")
    print("=" * 70)

    print("\n[1/3] 営業モードの自動判定...")
    modes = determine_operation_modes(2026, 5)
    reduced_days = [d for d, m in modes.items() if m.value == "省人員"]
    print(f"  → 省人員モード: 5/{reduced_days[0]}〜5/{reduced_days[-1]} ({len(reduced_days)}日)")

    print("\n[2/3] シフトを生成中...")
    print("  方針: 5連勤までハード許容、4連勤超えはソフトペナルティで最小化")
    start = time.time()
    shift = generate_shift(
        year=2026,
        month=5,
        off_requests=OFF_REQUESTS,
        work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        operation_modes=modes,
        # 5月の特例: 野澤さんは前月4連勤+5/1出勤希望のため境界スキップ（今月のみ）
        consec_exceptions=["野澤"],
        max_consec_override=5,
        time_limit_seconds=120,
        verbose=True,
    )
    elapsed = time.time() - start
    print(f"  → 処理時間: {elapsed:.1f}秒")
    actual_max_consec = 5

    if shift is None:
        print("\n❌ シフト生成に失敗しました。制約を緩めるか、希望データを見直してください。")
        return

    print("\n[3/3] 生成されたシフトを検証中...")
    result = validate(
        shift=shift,
        work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        max_consec=actual_max_consec,
    )

    print("\n" + "=" * 70)
    print("【生成されたシフト表】")
    print("凡例: ○赤羽 □東口 △大宮 ☆西口 ◆すずらん ×休 ・空白(山本のみ)")
    print("=" * 70)
    print_shift_table(shift)

    # 検証結果
    print("\n")
    result.print_summary()


if __name__ == "__main__":
    main()
