"""
手動作成された 2026年5月シフトの実証検証
================================================
顧問が手動で組んだシフトに対して、我々の Validator がどんな判定を出すか確認する。

これにより：
1. 我々の制約理解が正しいか
2. 手動シフトが実際にどの程度ルールを守れているか
3. AIが守るべき制約と、現実の運用の妥協ポイント

がわかる。
"""

from .excel_loader import load_shift_from_excel
from .paths import MAY_2026_SHIFT_XLSX
from .validator import validate
from .may_2026_data import OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER
from .rules import MAY_2026_HOLIDAY_OVERRIDES


def main():
    print("=" * 70)
    print("【2026年5月 手動シフト の制約検証】")
    print("=" * 70)

    print("\n[1/2] Excelからシフトを読み込み中...")
    shift, short_days = load_shift_from_excel(
        str(MAY_2026_SHIFT_XLSX)
    )
    print(f"  → {len(shift.assignments)} 件のシフトを読み込みました")
    print(f"  → 「人員少」マーク日: 5/{', 5/'.join(map(str, short_days))}")

    print("\n[2/2] Validator で検証中...")
    # 手動シフトの実情に合わせて max_consec を緩めて確認（4 → 5）
    for max_consec_test in [4, 5, 6, 7]:
        result = validate(
            shift=shift,
            work_requests=WORK_REQUESTS,
            off_requests=OFF_REQUESTS,
            prev_month=PREVIOUS_MONTH_CARRYOVER,
            holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
            max_consec=max_consec_test,
        )
        print(f"\n  最大{max_consec_test}連勤を許容した場合: "
              f"エラー{result.error_count}件 / 警告{result.warning_count}件")

    # 詳しく出すのは max_consec=7 (最大限緩めた場合) の結果
    print("\n" + "=" * 70)
    print("【最大7連勤許容での詳細結果】")
    print("=" * 70)
    result = validate(
        shift=shift,
        work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
        max_consec=7,
    )
    result.print_summary()


if __name__ == "__main__":
    main()
