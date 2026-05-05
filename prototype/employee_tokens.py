"""
従業員マジックリンク用のトークン生成・検証
================================================
各従業員に固有の URL トークンを発行し、URL 経由でログインできるようにする。

設計方針:
- トークンは「従業員名 + 秘密の塩（salt）」のハッシュで決定論的に計算
- 塩は Streamlit Secrets に保存（外部に漏れなければ安全）
- データベース不要、ファイル保存不要、再デプロイで消えない
- 塩を変えれば全トークン無効化（一括失効）

トークンURL の形式:
    https://daikokuya-shift.streamlit.app/?token=abc123def456

セキュリティ:
- 塩を秘密にする限り、トークンを推測できない
- 推測攻撃に強いSHA256ベース、16文字（64bit）のトークン
- HTTPS 通信なので URL 自体は暗号化されて送られる
"""

from __future__ import annotations
import hashlib
import os
from typing import Optional

import streamlit as st


# Streamlit Secrets / 環境変数のキー名
SECRET_MAGIC_LINK_SALT = "MAGIC_LINK_SALT"

# トークンの長さ（16文字 = 64bit）
TOKEN_LENGTH = 16


def _get_salt() -> str:
    """秘密の塩を取得（Streamlit Secrets > 環境変数）"""
    try:
        if SECRET_MAGIC_LINK_SALT in st.secrets:
            return str(st.secrets[SECRET_MAGIC_LINK_SALT])
    except Exception:
        pass
    return os.environ.get(SECRET_MAGIC_LINK_SALT, "")


def is_salt_configured() -> bool:
    """塩が設定されているか"""
    return bool(_get_salt())


def generate_token(employee_name: str, salt: Optional[str] = None) -> str:
    """
    従業員名から決定論的にトークンを生成。
    同じ (名前, 塩) からは常に同じトークンが返る。
    """
    if salt is None:
        salt = _get_salt()
    if not salt:
        return ""
    raw = f"{salt}::{employee_name}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return digest[:TOKEN_LENGTH]


def validate_token(token: str, all_employee_names: list[str]) -> Optional[str]:
    """
    トークンを検証し、対応する従業員名を返す。
    無効なトークンなら None。
    """
    if not token or len(token) != TOKEN_LENGTH:
        return None
    salt = _get_salt()
    if not salt:
        return None
    for name in all_employee_names:
        if generate_token(name, salt=salt) == token:
            return name
    return None


def get_magic_link(employee_name: str, base_url: str) -> str:
    """マジックリンクURLを生成"""
    token = generate_token(employee_name)
    if not token:
        return ""
    base_url = base_url.rstrip("/")
    return f"{base_url}/?token={token}"


def get_line_message(employee_name: str, base_url: str) -> str:
    """LINE送信用のメッセージテンプレートを生成"""
    link = get_magic_link(employee_name, base_url)
    if not link:
        return ""
    return (
        f"【大黒屋シフト管理システム ご案内】\n"
        f"\n"
        f"{employee_name}さん専用のリンクをお送りします。\n"
        f"\n"
        f"▼ こちらをタップしてください\n"
        f"{link}\n"
        f"\n"
        f"このURLは{employee_name}さん専用ですので、他の方には共有しないでください。\n"
        f"スマホのお気に入りやLINEのトークノートに保存しておくと便利です。\n"
        f"\n"
        f"毎月25日までに翌月分のシフト希望をご提出ください。\n"
        f"よろしくお願いいたします。"
    )


# ============================================================
# 動作テスト
# ============================================================

if __name__ == "__main__":
    import os
    os.environ[SECRET_MAGIC_LINK_SALT] = "test-salt-do-not-use-in-production"

    print("【マジックリンク 動作テスト】\n")

    # トークン生成
    name = "楯"
    token = generate_token(name)
    print(f"トークン生成: {name} → {token}")

    # 同じ名前から同じトークン
    token2 = generate_token(name)
    assert token == token2
    print(f"  決定論的: {name} は常に同じトークン → ✓")

    # 異なる名前は異なるトークン
    other_token = generate_token("板倉")
    assert token != other_token
    print(f"  別の従業員: 板倉 → {other_token}（異なる）✓")

    # 検証
    employees = ["楯", "板倉", "今津", "鈴木"]
    found = validate_token(token, employees)
    print(f"\n検証: {token} → {found}")
    assert found == "楯"

    # 無効トークン
    invalid = validate_token("invalid-token", employees)
    assert invalid is None
    print(f"検証: invalid-token → {invalid}")

    # マジックリンク生成
    link = get_magic_link("楯", "https://daikokuya-shift.streamlit.app")
    print(f"\nマジックリンク（楯）:\n  {link}")

    # LINE メッセージ
    msg = get_line_message("楯", "https://daikokuya-shift.streamlit.app")
    print(f"\nLINEメッセージ:\n{msg}")

    print("\n✅ マジックリンク機構 動作確認完了")
