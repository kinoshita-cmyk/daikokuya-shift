"""
カレンダー掃引テスト（将来月の事前検査）
================================================
「希望提出ゼロ＋標準ルールだけ」の状態で、今後の各月が生成可能かを
順番に検査する。月末が月曜（東口休店）に当たる月など、
カレンダーの端例で基本ルール同士が衝突していないかを、
その月が来る前に検知するのが目的。

過去の障害（このテストがあれば事前に検知できたもの）:
- 2026-08-31（月曜）: 東口休店 × 店長月末固定 の衝突
- 同様の月末月曜は 2026-11-30、月初月曜は 2027-02-01 / 2027-03-01 にも発生

使い方（ローカル）:
    cd <プロジェクトルート>
    python3 -m prototype.preflight_calendar_check            # 今月から12ヶ月
    python3 -m prototype.preflight_calendar_check 2026 9 6   # 2026年9月から6ヶ月

Web画面からは「⚙️ 設定 → 診断」の掃引ボタンで実行できる。
"""

from __future__ import annotations
import sys
from calendar import monthrange
from datetime import date
from typing import Callable, Optional

from .models import OperationMode


def sweep_months(
    start_year: int,
    start_month: int,
    count: int = 12,
    time_limit_per_month: int = 30,
    progress_callback: Optional[Callable[[str], object]] = None,
) -> list[dict]:
    """希望ゼロ＋標準ルールで count ヶ月分の生成可否を検査する。

    Returns:
        list[dict]: 月ごとの {"年月", "判定", "所要秒", "検出事項"}。
        判定は「✅ 生成可能」「❌ 解なし（ルール衝突）」「⏱ 時間内に判定できず」。
    """
    from .generator import determine_operation_modes, generate_shift
    from .infeasibility_diagnosis import diagnose_known_conflicts

    results: list[dict] = []
    year, month = int(start_year), int(start_month)

    for _ in range(int(count)):
        ym_label = f"{year}年{month}月"
        if progress_callback:
            try:
                progress_callback(f"{ym_label} を検査中...")
            except Exception:
                pass

        notes: list[str] = []
        try:
            modes = determine_operation_modes(year, month)

            # 1) ソルバーを使わない即時検査（提出ゼロの状態）
            findings = diagnose_known_conflicts(
                year, month,
                operation_modes=modes,
            )
            for f in findings:
                if f.get("確度") == "確実":
                    notes.append(f"{f.get('区分')}: {f.get('内容')}")

            # 2) 実際に生成を試す
            status_box: dict = {}
            shift = generate_shift(
                year=year,
                month=month,
                off_requests={},
                work_requests=[],
                prev_month=[],
                flexible_off=[],
                holiday_overrides={},
                operation_modes=modes,
                time_limit_seconds=int(time_limit_per_month),
                verbose=False,
                status_out=status_box,
            )
            if shift is not None:
                verdict = "✅ 生成可能"
            elif status_box.get("status") == "INFEASIBLE":
                verdict = "❌ 解なし（ルール衝突）"
                if not notes:
                    notes.append(
                        "基本ルールだけで矛盾しています。"
                        "画面の原因調査ボタンで詳細を確認してください。"
                    )
            else:
                verdict = "⏱ 時間内に判定できず"
            wall = status_box.get("wall_time_seconds", "")
        except Exception as exc:
            verdict = f"⚠ 検査エラー（{type(exc).__name__}）"
            wall = ""
            notes.append(str(exc)[:120])

        # 月末月初が月曜かどうかの参考情報
        days_in_month = monthrange(year, month)[1]
        edge_info = []
        if date(year, month, 1).weekday() == 0:
            edge_info.append("月初1日が月曜")
        if date(year, month, days_in_month).weekday() == 0:
            edge_info.append(f"月末{days_in_month}日が月曜")
        if edge_info:
            notes.append("参考: " + "・".join(edge_info) + "（東口休店と重なる月）")

        results.append({
            "年月": ym_label,
            "判定": verdict,
            "所要秒": wall,
            "検出事項": " / ".join(notes) if notes else "",
        })

        month += 1
        if month > 12:
            month = 1
            year += 1

    return results


def main() -> int:
    """コマンドライン実行用。"""
    today = date.today()
    args = sys.argv[1:]
    if len(args) >= 2:
        year, month = int(args[0]), int(args[1])
    else:
        year, month = today.year, today.month
    count = int(args[2]) if len(args) >= 3 else 12

    print(f"【カレンダー掃引テスト】{year}年{month}月から{count}ヶ月分")
    print("希望提出ゼロ＋標準ルールで各月の生成可否を検査します。\n")

    results = sweep_months(
        year, month, count,
        progress_callback=lambda msg: print(f"  {msg}"),
    )

    print("\n=== 結果 ===")
    ng_count = 0
    for r in results:
        line = f"{r['年月']}: {r['判定']}"
        if r["所要秒"] != "":
            line += f" ({r['所要秒']}秒)"
        print(line)
        if r["検出事項"]:
            print(f"    {r['検出事項']}")
        if "❌" in r["判定"] or "⚠" in r["判定"]:
            ng_count += 1

    if ng_count:
        print(f"\n⚠ {ng_count}ヶ月で問題が見つかりました。該当月が来る前に対処してください。")
        return 1
    print("\n✅ 全ての月で基本ルールの衝突はありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
