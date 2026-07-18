"""
GitHub同期チェック
================================================
ローカルのソースファイルと GitHub（デプロイに使われる main ブランチ）を
比較し、「修正したのにアップロードし忘れているファイル」を検出する。

背景:
    このプロジェクトは GitHub への手動アップロードでデプロイしている。
    過去に「ローカルでは修正済みなのに本番に反映されておらず、
    直したはずの障害が再発する」事故が複数回起きた。
    ファイルを修正したら、アップロード前後にこのスクリプトを実行して
    差分ゼロを確認する。

使い方:
    cd <プロジェクトルート>
    python3 check_github_sync.py

出力:
    - ✅ 一致            : ローカルと GitHub が同じ
    - ❌ 差分あり        : アップロードが必要（ローカルの方が新しい想定）
    - ⚠ GitHubに無い     : GitHub側に存在しないファイル
    - （終了コード: 全て一致なら 0、差分があれば 1）

外部ライブラリ不要（Python標準ライブラリのみ / Python 3.9+）。
"""

import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO = "kinoshita-cmyk/daikokuya-shift"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

PROJECT_ROOT = Path(__file__).resolve().parent

# 比較対象: デプロイに影響するソース・設定ファイル
TARGET_PATTERNS = [
    ("app", "*.py"),
    ("prototype", "*.py"),
    ("config", "*.json"),
    (".streamlit", "*.toml"),
    ("", "requirements.txt"),
    ("", "check_github_sync.py"),
]

# 比較しないファイル（ローカル専用・実行時生成物）
EXCLUDE_NAMES = {
    "__pycache__",
    ".DS_Store",
}


def collect_local_files() -> list:
    files = []
    for folder, pattern in TARGET_PATTERNS:
        base = PROJECT_ROOT / folder if folder else PROJECT_ROOT
        if not base.exists():
            continue
        for path in sorted(base.glob(pattern)):
            if path.name in EXCLUDE_NAMES or not path.is_file():
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            files.append(rel)
    return files


def fetch_remote(rel_path: str):
    url = f"{RAW_BASE}/{rel_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "sync-check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return None


def main() -> int:
    print(f"【GitHub同期チェック】{REPO} ({BRANCH}) と比較します\n")

    local_files = collect_local_files()
    if not local_files:
        print("⚠ 比較対象ファイルが見つかりません。プロジェクト直下で実行してください。")
        return 1

    matched = []
    differing = []
    missing_remote = []
    errors = []

    for rel in local_files:
        try:
            remote = fetch_remote(rel)
        except Exception as exc:
            errors.append((rel, f"{type(exc).__name__}: {exc}"))
            continue
        local_bytes = (PROJECT_ROOT / rel).read_bytes()
        if remote is None:
            missing_remote.append(rel)
        elif remote == local_bytes:
            matched.append(rel)
        else:
            differing.append(rel)

    print(f"✅ 一致: {len(matched)} ファイル")
    if differing:
        print(f"\n❌ 差分あり（アップロードが必要）: {len(differing)} ファイル")
        for rel in differing:
            print(f"   - {rel}")
    if missing_remote:
        print(f"\n⚠ GitHubに無い（新規ファイル？アップロード忘れ？）: {len(missing_remote)} ファイル")
        for rel in missing_remote:
            print(f"   - {rel}")
    if errors:
        print(f"\n⚠ 取得エラー: {len(errors)} ファイル")
        for rel, msg in errors:
            print(f"   - {rel}: {msg}")

    if differing or missing_remote:
        print(
            "\n📤 上記のファイルを GitHub にアップロードし、"
            "反映後にもう一度このスクリプトを実行して「全て一致」を確認してください。"
        )
        print("   （アップロード直後は反映まで1〜2分かかることがあります）")
        return 1
    if errors:
        print("\n⚠ 一部のファイルを比較できませんでした。ネットワークを確認して再実行してください。")
        return 1
    print("\n🎉 全ファイルが GitHub と一致しています。アップロード漏れはありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
