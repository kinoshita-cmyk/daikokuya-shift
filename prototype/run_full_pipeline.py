"""
エンドツーエンドのフルパイプラインテスト
================================================
1. シフト自動生成
2. 制約検証
3. AI vs 手動比較
4. Excel/PDF出力
5. バックアップ保存
"""

import time
from pathlib import Path

from .generator import generate_shift, determine_operation_modes
from .paths import MAY_2026_SHIFT_XLSX, OUTPUT_DIR
from .validator import validate
from .excel_loader import load_shift_from_excel
from .excel_exporter import export_shift_to_excel
from .pdf_exporter import export_shift_to_pdf
from .backup import ShiftBackup
from .may_2026_data import (
    OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
)
from .rules import MAY_2026_HOLIDAY_OVERRIDES


def main():
    print("=" * 70)
    print("  大黒屋シフト管理システム - フルパイプラインテスト")
    print("=" * 70)

    output_dir = OUTPUT_DIR
    output_dir.mkdir(exist_ok=True)

    # ========== Step 1: シフト生成 ==========
    print("\n[1/6] AI でシフトを自動生成中...")
    t0 = time.time()
    modes = determine_operation_modes(2026, 5)
    shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS, work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER, flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, operation_modes=modes,
        consec_exceptions=["野澤"], max_consec_override=5,
        time_limit_seconds=120, verbose=False,
    )
    elapsed = time.time() - t0
    if shift is None:
        print("  ❌ 失敗")
        return
    print(f"  ✅ 成功（{elapsed:.1f}秒）")

    # ========== Step 2: 検証 ==========
    print("\n[2/6] 制約検証中...")
    result = validate(
        shift=shift, work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, max_consec=5,
    )
    print(f"  ✅ エラー {result.error_count} 件 / 警告 {result.warning_count} 件")

    # ========== Step 3: 手動シフト比較 ==========
    print("\n[3/6] 手動シフトとの比較...")
    manual_path = str(MAY_2026_SHIFT_XLSX)
    if Path(manual_path).exists():
        manual_shift, _ = load_shift_from_excel(manual_path)
        manual_result = validate(
            shift=manual_shift, work_requests=WORK_REQUESTS,
            off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
            holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, max_consec=5,
        )
        print(f"  手動: エラー {manual_result.error_count} 件")
        print(f"  AI:   エラー {result.error_count} 件")
        diff = manual_result.error_count - result.error_count
        if diff > 0:
            print(f"  ✅ AI のほうが {diff} 件少ない")
    else:
        print("  ⚠ 手動シフトのファイルがないためスキップ")

    # ========== Step 4: Excel 出力 ==========
    print("\n[4/6] Excel 出力中...")
    xlsx_path = output_dir / "test_2026年5月_AI生成シフト.xlsx"
    export_shift_to_excel(shift, xlsx_path)
    print(f"  ✅ {xlsx_path.name} ({xlsx_path.stat().st_size:,} bytes)")

    # ========== Step 5: PDF 出力 ==========
    print("\n[5/6] PDF 出力中...")
    pdf_path = output_dir / "test_2026年5月_AI生成シフト.pdf"
    export_shift_to_pdf(
        shift, pdf_path,
        header_notes=["AI 自動生成版（OR-Tools CP-SAT）"],
    )
    print(f"  ✅ {pdf_path.name} ({pdf_path.stat().st_size:,} bytes)")

    # ========== Step 6: バックアップ ==========
    print("\n[6/6] バックアップ保存中...")
    backup = ShiftBackup()
    backup_path = backup.save_shift(
        shift, kind="test", author="フルパイプライン",
        note="エンドツーエンドテスト",
    )
    print(f"  ✅ {backup_path.relative_to(backup.backup_dir)}")

    # ========== 完了 ==========
    print("\n" + "=" * 70)
    print("  ✨ すべてのステップが正常に完了しました！")
    print("=" * 70)
    print(f"\n  生成シフト: エラー {result.error_count} 件")
    print(f"  Excel: {xlsx_path}")
    print(f"  PDF:   {pdf_path}")
    print(f"\n  Web UI を起動するには:")
    print(f"    cd /Users/kinoshitayoshihide/daikokuya-shift")
    print(f"    ./start_app.sh")


if __name__ == "__main__":
    main()
