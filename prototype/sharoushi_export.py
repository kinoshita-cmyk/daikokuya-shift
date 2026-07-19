"""
社労士提出用データの書き出し（出勤簿システム連携）
================================================
月初時点で確定した「その月の有給取得日数」を、従業員ごとに
CSV としてまとめる。出力は2経路で使われる:

1. 画面からのダウンロード（⚙️ 有給使用状況タブ）
2. GitHub バックアップへの自動保存
   → GAS（Google Apps Script）が定期取得して指定の
     Google ドライブフォルダへ配置し、出勤簿と連動させる
     （GAS 側のコードは gas/paid_leave_to_drive.gs 参照）

CSVの列:
    年月, 氏名, 申告有給日数, 管理者調整日数, 合計有給日数, 調整日付, 備考

- 申告有給日数   : 本人がシフト希望と一緒に提出した有給日数（月初に確定）
- 管理者調整日数 : 病欠→有給振替など、管理者が後から付けた調整分
- 合計有給日数   : 上2つの合計（社労士が使う最終値）
- 有給0日の従業員も行として出力する（0であることも社労士への情報）
"""

from __future__ import annotations
import csv
import io
from typing import Optional

from .submission_loader import load_submissions_for_month

# GitHub バックアップ内の保存パス（GAS 側もこのパスを参照する）
EXPORT_REPO_DIR = "exports/paid_leave"


def paid_leave_csv_repo_path(year: int, month: int) -> str:
    """GitHub バックアップ内での CSV パスを返す。"""
    return f"{EXPORT_REPO_DIR}/paid_leave_{int(year):04d}-{int(month):02d}.csv"


def build_paid_leave_rows(
    year: int,
    month: int,
    expected_employees: list,
    admin_days: Optional[dict] = None,
    admin_dates: Optional[dict] = None,
) -> list[dict]:
    """従業員ごとの有給日数の行データを作る。

    Args:
        expected_employees: 対象従業員名のリスト（この全員分の行を必ず出す）
        admin_days: {氏名: 管理者調整の有給日数}（app 側の集計関数から渡す）
        admin_dates: {氏名: {日, ...}} 管理者調整の対象日
    """
    admin_days = admin_days or {}
    admin_dates = admin_dates or {}
    sub = load_submissions_for_month(
        int(year), int(month), list(expected_employees),
    )
    ym_label = f"{int(year):04d}-{int(month):02d}"

    rows: list[dict] = []
    all_names = list(expected_employees)
    # 提出データ・調整データにだけ現れる名前（退職者の残調整など）も漏らさない
    for extra in list(sub.paid_leave_days.keys()) + list(admin_days.keys()):
        if extra not in all_names:
            all_names.append(extra)

    for name in all_names:
        submitted = int(sub.paid_leave_days.get(name, 0) or 0)
        adjusted = int(admin_days.get(name, 0) or 0)
        dates = sorted(admin_dates.get(name, set()) or set())
        date_text = "、".join(f"{int(month)}/{d}" for d in dates)
        note = ""
        if name not in expected_employees:
            note = "シフト対象外（調整・提出データのみ）"
        rows.append({
            "年月": ym_label,
            "氏名": str(name),
            "申告有給日数": submitted,
            "管理者調整日数": adjusted,
            "合計有給日数": submitted + adjusted,
            "調整日付": date_text,
            "備考": note,
        })
    return rows


def admin_totals_from_file(
    config_file,
    year: int,
    month: int,
) -> tuple:
    """admin_paid_leave_adjustments.json から管理者調整の日数・日付を集計する。

    app.py 内の admin_paid_leave_days_for_month / dates_for_month と
    同じ集計。アプリを起動せずに実行する自動書き出し（GitHub Actions）用。
    """
    import json
    from pathlib import Path
    days: dict = {}
    dates: dict = {}
    path = Path(config_file)
    if not path.exists():
        return days, dates
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return days, dates
    for adj in (data.get("adjustments", []) or []):
        if not isinstance(adj, dict):
            continue
        if int(adj.get("year", 0) or 0) != int(year):
            continue
        if int(adj.get("month", 0) or 0) != int(month):
            continue
        name = str(adj.get("employee", "")).strip()
        if not name:
            continue
        days[name] = days.get(name, 0) + int(adj.get("days", 0) or 0)
        dates.setdefault(name, set()).update(
            int(d) for d in adj.get("dates", []) if str(d).isdigit()
        )
    return days, dates


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    """行データを Excel でも文字化けしない CSV（UTF-8 BOM付き）にする。"""
    headers = [
        "年月", "氏名", "申告有給日数", "管理者調整日数",
        "合計有給日数", "調整日付", "備考",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8-sig")


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    from .employees import shift_active_employees
    expected = [e.name for e in shift_active_employees() if not e.is_auxiliary]
    rows = build_paid_leave_rows(2026, 8, expected)
    print(f"【社労士提出用CSV 動作テスト】2026-08 / {len(rows)}名分")
    for r in rows[:5]:
        print(f"  {r['氏名']}: 申告{r['申告有給日数']}日 + 調整{r['管理者調整日数']}日"
              f" = 合計{r['合計有給日数']}日")
    csv_bytes = rows_to_csv_bytes(rows)
    print(f"CSVサイズ: {len(csv_bytes)} bytes / 保存先: {paid_leave_csv_repo_path(2026, 8)}")
