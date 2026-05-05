"""
GitHub 自動バックアップ
================================================
従業員が希望を提出するたびに、自動的に GitHub のプライベートリポジトリへ
バックアップする。Streamlit Cloud のファイルが消えてもデータが残るよう保護。

設定:
    Streamlit Secrets に以下を追加:
        GITHUB_TOKEN = "ghp_xxx..."           Personal Access Token (Contents:Read+Write)
        GITHUB_BACKUP_REPO = "user/repo-name"  バックアップ先リポジトリ（Private 推奨）

設計:
- 失敗してもアプリ動作は止めない（ベストエフォート）
- HTTP リクエストのタイムアウトを短く設定
- 既存ファイルがあれば SHA を取得して上書き
- バックアップ先のディレクトリ構造:
    preferences/YYYY-MM/{従業員名}_TIMESTAMP.json
    shifts/YYYY-MM/finalized_TIMESTAMP.json
    config/employees_TIMESTAMP.json
"""

from __future__ import annotations
import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# requests は標準では入っていないが、anthropic SDK が依存しているので利用可能
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# Streamlit Secrets / 環境変数のキー名
SECRET_GITHUB_TOKEN = "GITHUB_TOKEN"
SECRET_GITHUB_BACKUP_REPO = "GITHUB_BACKUP_REPO"


def _get_secret(key: str, default: str = "") -> str:
    """Streamlit Secrets または環境変数から値を取得"""
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


def is_github_backup_enabled() -> bool:
    """GitHub バックアップが利用可能か（設定済みか）"""
    return bool(
        HAS_REQUESTS
        and _get_secret(SECRET_GITHUB_TOKEN)
        and _get_secret(SECRET_GITHUB_BACKUP_REPO)
    )


def _get_token() -> str:
    return _get_secret(SECRET_GITHUB_TOKEN)


def _get_repo() -> str:
    return _get_secret(SECRET_GITHUB_BACKUP_REPO)


# ============================================================
# GitHub API ラッパー
# ============================================================

def _push_file(
    file_path_in_repo: str,
    content: bytes,
    commit_message: str,
    timeout: int = 8,
) -> tuple[bool, str]:
    """
    ファイルを GitHub の指定パスに作成または更新する。

    Returns:
        (success: bool, message: str)
    """
    if not HAS_REQUESTS:
        return False, "requests library not installed"

    token = _get_token()
    repo = _get_repo()
    if not token or not repo:
        return False, "GITHUB_TOKEN または GITHUB_BACKUP_REPO 未設定"

    url = f"https://api.github.com/repos/{repo}/contents/{file_path_in_repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 既存ファイルの SHA 取得（上書き時に必要）
    existing_sha: Optional[str] = None
    try:
        check = requests.get(url, headers=headers, timeout=timeout)
        if check.status_code == 200:
            existing_sha = check.json().get("sha")
    except Exception:
        pass  # 失敗したら新規作成として扱う

    # base64 エンコード
    content_b64 = base64.b64encode(content).decode("utf-8")
    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch": "main",
    }
    if existing_sha:
        payload["sha"] = existing_sha

    # PUT 送信
    try:
        response = requests.put(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code in (200, 201):
            return True, f"OK ({response.status_code})"
        else:
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "タイムアウト"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ============================================================
# 公開 API（用途別バックアップ関数）
# ============================================================

def push_preference_to_github(
    local_file_path: Path,
    employee_name: str,
    year: int,
    month: int,
) -> tuple[bool, str]:
    """
    従業員の希望提出データを GitHub にプッシュ。

    保存先: preferences/YYYY-MM/{従業員名}_TIMESTAMP.json
    """
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        local_file_path = Path(local_file_path)
        if not local_file_path.exists():
            return False, "ローカルファイルが見つからない"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        # ファイル名から特殊文字を除く
        safe_name = "".join(c if c.isalnum() else "_" for c in employee_name)
        repo_path = f"preferences/{year:04d}-{month:02d}/{safe_name}_{ts}.json"
        content = local_file_path.read_bytes()
        commit_msg = f"Preference: {employee_name} for {year}-{month:02d}"
        return _push_file(repo_path, content, commit_msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def push_shift_to_github(
    local_file_path: Path,
    year: int,
    month: int,
    kind: str = "finalized",  # "draft" / "finalized"
) -> tuple[bool, str]:
    """
    確定シフトを GitHub にプッシュ。

    保存先: shifts/YYYY-MM/{kind}_TIMESTAMP.json
    """
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        local_file_path = Path(local_file_path)
        if not local_file_path.exists():
            return False, "ローカルファイルが見つからない"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        repo_path = f"shifts/{year:04d}-{month:02d}/{kind}_{ts}.json"
        content = local_file_path.read_bytes()
        commit_msg = f"Shift {kind}: {year}-{month:02d}"
        return _push_file(repo_path, content, commit_msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def push_config_to_github(
    config_name: str,
    config_data: dict,
) -> tuple[bool, str]:
    """
    設定ファイル（従業員マスタ、ルール設定など）を GitHub にプッシュ。

    保存先: config/{config_name}_TIMESTAMP.json
    """
    if not is_github_backup_enabled():
        return False, "未設定"
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        repo_path = f"config/{config_name}_{ts}.json"
        content = json.dumps(
            config_data, ensure_ascii=False, indent=2,
        ).encode("utf-8")
        commit_msg = f"Config update: {config_name}"
        return _push_file(repo_path, content, commit_msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_connection() -> tuple[bool, str]:
    """
    GitHub への接続を試験する（認証OK・リポジトリアクセスOK確認）。

    Returns:
        (success, message)
    """
    if not HAS_REQUESTS:
        return False, "requests ライブラリが未インストール"
    token = _get_token()
    repo = _get_repo()
    if not token:
        return False, "GITHUB_TOKEN が未設定"
    if not repo:
        return False, "GITHUB_BACKUP_REPO が未設定"

    url = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code == 200:
            data = response.json()
            return True, (
                f"✅ 接続OK！リポジトリ: {data.get('full_name')} "
                f"({'Private' if data.get('private') else 'Public'})"
            )
        elif response.status_code == 401:
            return False, "認証エラー: GITHUB_TOKEN が無効か期限切れ"
        elif response.status_code == 404:
            return False, (
                f"リポジトリが見つかりません: {repo}\n"
                "（リポジトリ名のスペル違い、または PAT に権限がない可能性）"
            )
        else:
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "タイムアウト（GitHub にアクセスできない）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    print("【GitHub バックアップ 動作テスト】\n")

    if not HAS_REQUESTS:
        print("❌ requests ライブラリが未インストール")
        exit(1)

    print("[1] 設定確認")
    print(f"  GITHUB_TOKEN: {'✓ 設定済み' if _get_token() else '✗ 未設定'}")
    print(f"  GITHUB_BACKUP_REPO: {_get_repo() or '✗ 未設定'}")
    print(f"  is_github_backup_enabled: {is_github_backup_enabled()}")

    print("\n[2] 接続テスト")
    success, msg = test_connection()
    print(f"  結果: {msg}")

    if success:
        print("\n[3] テストファイル送信")
        ok, msg = push_config_to_github(
            "test_connection",
            {"timestamp": datetime.now().isoformat(), "test": True},
        )
        print(f"  結果: {'✅' if ok else '❌'} {msg}")
