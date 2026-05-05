"""
データバックアップ - エクスポート / インポート
================================================
Streamlit Cloud のファイルシステムは揮発性のため、
コード更新時に提出データが失われる可能性がある。
そのため：
- 経営者がいつでも全データを ZIP でダウンロードできる
- 後から ZIP を再アップロードして復元できる
仕組みを提供する。

バックアップ対象:
- backups/        従業員提出データ・確定シフトのスナップショット
- config/         従業員マスタの動的変更・ルール設定
- locks/          シフトロック情報
- output/         生成された Excel/PDF（任意）

ZIP 構造:
  daikokuya-shift-backup-YYYYMMDD-HHMMSS.zip
    backups/
      YYYY-MM/
        preferences_*.json
        shift_*.json
        edits_*.jsonl
    config/
      employees.json
      employee_history.jsonl
      rule_config.json
      rule_history.jsonl
    locks/
      YYYY-MM.lock
    metadata.json    バックアップ作成情報
"""

from __future__ import annotations
import io
import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .paths import PROJECT_ROOT, BACKUP_DIR, CONFIG_DIR, LOCK_DIR, OUTPUT_DIR


# バックアップに含めるディレクトリ
BACKUP_DIRS = {
    "backups": BACKUP_DIR,
    "config": CONFIG_DIR,
    "locks": LOCK_DIR,
}


@dataclass
class BackupSummary:
    """バックアップ内容の集計"""
    total_files: int
    submission_files: int     # 従業員提出ファイル数
    finalized_shifts: int     # 確定シフト数
    locked_months: int        # ロック済み月数
    config_files: int         # 設定ファイル数
    total_size_kb: float


# ============================================================
# エクスポート（バックアップ作成）
# ============================================================

def create_backup_zip(include_output: bool = False) -> tuple[bytes, BackupSummary]:
    """
    全データをZIPにまとめてバイト列として返す（Streamlit ダウンロード用）。

    Args:
        include_output: True なら output/ ディレクトリも含める

    Returns:
        (zip_bytes, summary)
    """
    summary = BackupSummary(
        total_files=0, submission_files=0, finalized_shifts=0,
        locked_months=0, config_files=0, total_size_kb=0.0,
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 各ディレクトリを ZIP に追加
        dirs_to_backup = dict(BACKUP_DIRS)
        if include_output:
            dirs_to_backup["output"] = OUTPUT_DIR

        for label, dir_path in dirs_to_backup.items():
            if not dir_path.exists():
                continue
            for file_path in dir_path.rglob("*"):
                if not file_path.is_file():
                    continue
                # ZIP内の相対パス
                relative = file_path.relative_to(dir_path.parent)
                zf.write(file_path, str(relative))
                summary.total_files += 1
                summary.total_size_kb += file_path.stat().st_size / 1024

                # ファイル種別ごとの集計
                name = file_path.name
                if name.startswith("preferences_"):
                    summary.submission_files += 1
                elif name.startswith("shift_finalized"):
                    summary.finalized_shifts += 1
                elif name.endswith(".lock"):
                    summary.locked_months += 1
                elif file_path.parent.name == "config":
                    summary.config_files += 1

        # メタデータ
        metadata = {
            "exported_at": datetime.now().isoformat(),
            "summary": {
                "total_files": summary.total_files,
                "submission_files": summary.submission_files,
                "finalized_shifts": summary.finalized_shifts,
                "locked_months": summary.locked_months,
                "config_files": summary.config_files,
                "total_size_kb": round(summary.total_size_kb, 2),
            },
            "directories": list(dirs_to_backup.keys()),
            "version": 1,
        }
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))

    return buffer.getvalue(), summary


def get_backup_filename() -> str:
    """ダウンロード用のファイル名を生成"""
    return f"daikokuya-shift-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"


# ============================================================
# インポート（バックアップから復元）
# ============================================================

@dataclass
class RestoreResult:
    """復元結果"""
    success: bool
    restored_files: int
    skipped_files: int
    errors: list[str]
    metadata: Optional[dict] = None


def restore_from_zip(
    zip_bytes: bytes,
    overwrite: bool = False,
    dry_run: bool = False,
) -> RestoreResult:
    """
    ZIPファイルからバックアップを復元する。

    Args:
        zip_bytes: アップロードされたZIPファイルのバイト列
        overwrite: True なら既存ファイルを上書き、False ならスキップ
        dry_run: True なら実際には書き込まずにシミュレーションのみ

    Returns:
        RestoreResult（処理結果の詳細）
    """
    result = RestoreResult(
        success=True, restored_files=0, skipped_files=0,
        errors=[],
    )
    try:
        buffer = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buffer, "r") as zf:
            # メタデータ読み込み（任意）
            try:
                with zf.open("metadata.json") as f:
                    result.metadata = json.load(f)
            except KeyError:
                pass

            # 各ファイルを抽出
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # メタデータは除外
                if info.filename == "metadata.json":
                    continue
                # 安全性チェック（パストラバーサル防止）
                if ".." in info.filename or info.filename.startswith("/"):
                    result.errors.append(f"不正なパスを拒否: {info.filename}")
                    continue
                # 想定外のディレクトリは拒否
                first_part = info.filename.split("/", 1)[0]
                if first_part not in ("backups", "config", "locks", "output"):
                    result.errors.append(f"想定外のディレクトリ: {info.filename}")
                    continue

                # 出力先パス
                target_path = PROJECT_ROOT / info.filename
                if target_path.exists() and not overwrite:
                    result.skipped_files += 1
                    continue

                if not dry_run:
                    # 親ディレクトリ作成
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    # ファイル抽出
                    with zf.open(info.filename) as src:
                        with open(target_path, "wb") as dst:
                            dst.write(src.read())
                result.restored_files += 1
    except zipfile.BadZipFile:
        result.success = False
        result.errors.append("ZIPファイルとして読み込めません（破損または不正な形式）")
    except Exception as e:
        result.success = False
        result.errors.append(f"予期しないエラー: {type(e).__name__}: {e}")

    return result


# ============================================================
# 簡易チェックサム（提出データ保護用）
# ============================================================

def get_submission_count(year: Optional[int] = None, month: Optional[int] = None) -> int:
    """
    現在保存されている従業員提出データの件数をカウントする。
    マネージャ画面で「現在何件のデータがあるか」を表示するのに使う。
    """
    count = 0
    if not BACKUP_DIR.exists():
        return 0
    if year and month:
        target_dirs = [BACKUP_DIR / f"{year}-{month:02d}"]
    else:
        target_dirs = [d for d in BACKUP_DIR.iterdir() if d.is_dir()]
    for d in target_dirs:
        if not d.exists():
            continue
        for f in d.glob("preferences_*.json"):
            count += 1
    return count


def get_all_data_summary() -> dict:
    """
    保存されている全データのサマリーを返す（ダッシュボード表示用）。
    """
    summary = {
        "submissions_total": 0,
        "submissions_by_month": {},  # "2026-05" -> count
        "finalized_shifts": 0,
        "locked_months": 0,
        "employees_modified": 0,
    }

    # 提出データ
    if BACKUP_DIR.exists():
        for month_dir in BACKUP_DIR.iterdir():
            if not month_dir.is_dir():
                continue
            month_count = 0
            for f in month_dir.glob("preferences_*.json"):
                month_count += 1
                summary["submissions_total"] += 1
            for f in month_dir.glob("shift_finalized*.json"):
                summary["finalized_shifts"] += 1
            if month_count > 0:
                summary["submissions_by_month"][month_dir.name] = month_count

    # ロック数
    if LOCK_DIR.exists():
        summary["locked_months"] = len(list(LOCK_DIR.glob("*.lock")))

    # 従業員マスタの変更回数
    history_file = CONFIG_DIR / "employee_history.jsonl"
    if history_file.exists():
        with open(history_file) as f:
            summary["employees_modified"] = sum(1 for _ in f)

    return summary


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【データエクスポート/インポート 動作テスト】\n")

    # 現状サマリー
    print("[1] 現状のデータサマリー")
    s = get_all_data_summary()
    print(f"  従業員提出データ: {s['submissions_total']} 件")
    print(f"  月別: {s['submissions_by_month']}")
    print(f"  確定シフト: {s['finalized_shifts']} 件")
    print(f"  ロック済み月: {s['locked_months']} 件")
    print(f"  従業員マスタ変更履歴: {s['employees_modified']} 件")

    # ZIP生成
    print(f"\n[2] バックアップZIP作成中...")
    zip_bytes, summary = create_backup_zip()
    print(f"  ファイル数: {summary.total_files}")
    print(f"  サイズ: {summary.total_size_kb:.2f} KB")
    print(f"  ZIP圧縮後: {len(zip_bytes) / 1024:.2f} KB")

    # 復元テスト（dry-run）
    print(f"\n[3] 復元テスト（dry-run = 実際には書かない）...")
    restore_result = restore_from_zip(zip_bytes, dry_run=True)
    print(f"  処理対象ファイル: {restore_result.restored_files}")
    print(f"  スキップ: {restore_result.skipped_files}")
    print(f"  エラー: {len(restore_result.errors)}")

    print("\n✅ データエクスポート機構 動作確認完了")
