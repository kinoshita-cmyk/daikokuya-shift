"""
プロジェクトのパス管理（ローカル開発・クラウドデプロイ両対応）
================================================
ハードコードされた絶対パスを使わず、プロジェクトルートを動的に検出する。

優先順位:
1. 環境変数 DAIKOKUYA_SHIFT_ROOT が指定されていればそれを使う
2. このファイルの2階層上をプロジェクトルートとする（自動検出）
"""

from __future__ import annotations
import os
from pathlib import Path


def get_project_root() -> Path:
    """プロジェクトルートディレクトリを取得"""
    # 環境変数があればそれを優先（テスト・本番運用で柔軟に切替可能）
    env_root = os.environ.get("DAIKOKUYA_SHIFT_ROOT")
    if env_root:
        return Path(env_root)
    # このファイル(prototype/paths.py)の2階層上 = プロジェクトルート
    return Path(__file__).resolve().parent.parent


# プロジェクト全体で使うパス定数
PROJECT_ROOT = get_project_root()
DATA_DIR = PROJECT_ROOT / "data"
BACKUP_DIR = PROJECT_ROOT / "backups"
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_DIR = PROJECT_ROOT / "config"
LOCK_DIR = PROJECT_ROOT / "locks"
DOCS_DIR = PROJECT_ROOT / "docs"

# Excel データへのフルパス
MAY_2026_SHIFT_XLSX = DATA_DIR / "may_2026_shift.xlsx"
SHIFT_TEMPLATE_XLSX = DATA_DIR / "shift_template.xlsx"


def ensure_dirs() -> None:
    """ランタイムで必要なディレクトリを作成"""
    for d in [BACKUP_DIR, OUTPUT_DIR, CONFIG_DIR, LOCK_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# モジュール読み込み時に自動で必要ディレクトリを作る
ensure_dirs()


if __name__ == "__main__":
    print(f"PROJECT_ROOT:    {PROJECT_ROOT}")
    print(f"DATA_DIR:        {DATA_DIR}")
    print(f"BACKUP_DIR:      {BACKUP_DIR}")
    print(f"OUTPUT_DIR:      {OUTPUT_DIR}")
    print(f"CONFIG_DIR:      {CONFIG_DIR}")
    print(f"LOCK_DIR:        {LOCK_DIR}")
