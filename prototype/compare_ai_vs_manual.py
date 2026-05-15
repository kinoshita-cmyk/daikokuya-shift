"""
AI生成シフト vs 手動シフトの比較
================================================
"""

from .generator import generate_shift, determine_operation_modes
from .paths import MAY_2026_SHIFT_XLSX
from .validator import validate
from .excel_loader import load_shift_from_excel
from .may_2026_data import OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS
from .rules import MAY_2026_HOLIDAY_OVERRIDES, get_monthly_work_target


def categorize_issues(result) -> dict:
    """違反を種別ごとにカウント"""
    cats = {}
    for issue in result.issues:
        key = f"{issue.severity}_{issue.category}"
        cats[key] = cats.get(key, 0) + 1
    return cats


def main():
    print("=" * 70)
    print("【2026年5月: AI生成 vs 手動シフト 比較】")
    print("=" * 70)

    # 手動シフト
    print("\n[1] 手動シフト（顧問が作成）...")
    manual_shift, manual_short = load_shift_from_excel(
        str(MAY_2026_SHIFT_XLSX)
    )
    manual_result = validate(
        shift=manual_shift, work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, max_consec=5,
    )

    # AI生成
    print("[2] AI生成シフト（OR-Tools CP-SAT）...")
    modes = determine_operation_modes(2026, 5)
    ai_shift = generate_shift(
        year=2026, month=5,
        off_requests=OFF_REQUESTS, work_requests=WORK_REQUESTS,
        prev_month=PREVIOUS_MONTH_CARRYOVER, flexible_off=FLEXIBLE_OFF_REQUESTS,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, operation_modes=modes,
        consec_exceptions=["野澤"], max_consec_override=5,
        time_limit_seconds=120, verbose=False,
    )
    ai_result = validate(
        shift=ai_shift, work_requests=WORK_REQUESTS,
        off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
        holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES, max_consec=5,
    )

    # 比較表
    print("\n" + "=" * 70)
    print("【比較サマリー】")
    print("=" * 70)
    print(f"{'指標':<20} {'手動':>10} {'AI':>10} {'差分':>10}")
    print("-" * 55)
    print(f"{'エラー件数':<20} {manual_result.error_count:>10} {ai_result.error_count:>10} {ai_result.error_count - manual_result.error_count:>+10}")
    print(f"{'警告件数':<20} {manual_result.warning_count:>10} {ai_result.warning_count:>10} {ai_result.warning_count - manual_result.warning_count:>+10}")

    # カテゴリー別比較
    manual_cats = categorize_issues(manual_result)
    ai_cats = categorize_issues(ai_result)
    all_cats = sorted(set(list(manual_cats.keys()) + list(ai_cats.keys())))

    print(f"\n{'違反カテゴリー':<30} {'手動':>10} {'AI':>10}")
    print("-" * 55)
    for cat in all_cats:
        m = manual_cats.get(cat, 0)
        a = ai_cats.get(cat, 0)
        improve = "✓" if a < m else "" if a == m else "✗"
        print(f"  {cat:<28} {m:>10} {a:>10} {improve}")

    # 統計（出勤日数）
    print(f"\n{'従業員':<10} {'手動 出勤/休':<15} {'AI 出勤/休':<15} {'目標':<10}")
    print("-" * 55)
    from .employees import ALL_EMPLOYEES
    from .models import Store
    days_in_month = 31
    for e in ALL_EMPLOYEES:
        if e.is_auxiliary or e.role.value == "顧問":
            continue
        m_work = sum(1 for d in range(1, days_in_month+1)
                     if (a := manual_shift.get_assignment(e.name, d)) and a.store != Store.OFF)
        m_off = days_in_month - m_work
        a_work = sum(1 for d in range(1, days_in_month+1)
                     if (a := ai_shift.get_assignment(e.name, d)) and a.store != Store.OFF)
        a_off = days_in_month - a_work
        target = get_monthly_work_target(e.name, 5, e.annual_target_days) or "-"
        print(f"  {e.name:<8} {f'{m_work}/{m_off}':<13} {f'{a_work}/{a_off}':<13} {target}")

    print("\n" + "=" * 70)
    print("【結論】")
    print("=" * 70)
    if ai_result.error_count < manual_result.error_count:
        print(f"✅ AI生成のほうが手動より優秀です（エラー -{manual_result.error_count - ai_result.error_count}件）")
    else:
        print(f"⚠ AI生成は手動と同等以上のエラー（エラー +{ai_result.error_count - manual_result.error_count}件）")


if __name__ == "__main__":
    main()
