"""
有給CSVの完全自動書き出しランナー（GitHub Actions 用）
================================================
アプリを誰も開かなくても、毎日決まった時刻に GitHub Actions が
このスクリプトを実行し、当月・前月の有給CSVをバックアップ
リポジトリの exports/paid_leave/ に書き出す。
その後は GAS が Google ドライブへ自動配置する（gas/ 参照）。

実行方法（GitHub Actions のワークフローから）:
    cd <シフト管理システムのコード>
    python prototype/run_paid_leave_export.py <バックアップrepoのパス>

処理内容:
1. バックアップrepoの backups/（提出データ）と config/（管理者調整・
   従業員マスタ）を、コード側の想定位置へコピーする
2. 当月と前月の有給CSVを生成する（月中の管理者調整も毎日反映される）
3. バックアップrepo側の exports/paid_leave/ へ書き出す
   （コミットとプッシュはワークフロー側で行う）
"""

from __future__ import annotations
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))

JST = timezone(timedelta(hours=9))


def _overlay_backup_data(data_root: Path) -> None:
    """バックアップrepoのデータをコード側の想定位置へ反映する。"""
    # 提出データ
    src_backups = data_root / "backups"
    if src_backups.is_dir():
        shutil.copytree(src_backups, CODE_ROOT / "backups", dirs_exist_ok=True)

    # 設定（最新版があれば上書き）
    config_dst = CODE_ROOT / "config"
    config_dst.mkdir(parents=True, exist_ok=True)
    overlays = [
        ("config/admin_paid_leave_adjustments.json",
         "admin_paid_leave_adjustments.json"),
        ("config/employees_latest.json", "employees.json"),
        ("config/rule_config_latest.json", "rule_config.json"),
    ]
    for src_rel, dst_name in overlays:
        src = data_root / src_rel
        if src.is_file():
            shutil.copyfile(src, config_dst / dst_name)


def export_month(data_root: Path, year: int, month: int) -> Path:
    """1ヶ月分の有給CSVをバックアップrepo側へ書き出す。"""
    from prototype.employees import shift_active_employees
    from prototype.sharoushi_export import (
        admin_totals_from_file,
        build_paid_leave_rows,
        paid_leave_csv_repo_path,
        rows_to_csv_bytes,
    )
    expected = [
        e.name for e in shift_active_employees() if not e.is_auxiliary
    ]
    admin_days, admin_dates = admin_totals_from_file(
        CODE_ROOT / "config" / "admin_paid_leave_adjustments.json",
        year, month,
    )
    rows = build_paid_leave_rows(
        year, month, expected,
        admin_days=admin_days, admin_dates=admin_dates,
    )
    out_path = data_root / paid_leave_csv_repo_path(year, month)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(rows_to_csv_bytes(rows))
    total = sum(int(r.get("合計有給日数", 0) or 0) for r in rows)
    print(f"書き出し: {out_path}（{len(rows)}名 / 合計{total}日）")
    return out_path


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: python prototype/run_paid_leave_export.py <バックアップrepoのパス>")
        return 1
    data_root = Path(sys.argv[1]).resolve()
    if not data_root.is_dir():
        print(f"エラー: バックアップrepoが見つかりません: {data_root}")
        return 1

    _overlay_backup_data(data_root)

    now = datetime.now(JST)
    # 当月と前月の2ヶ月分を書き出す
    # （前月分: 月初に入ってからの前月調整を反映し続けるため）
    targets = [(now.year, now.month)]
    prev_y, prev_m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    targets.append((prev_y, prev_m))

    for y, m in targets:
        export_month(data_root, y, m)

    print("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
