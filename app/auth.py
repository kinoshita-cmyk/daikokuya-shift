"""
認証モジュール（経営者：パスワード / 従業員：マジックリンク）
================================================
- 経営者: MANAGER_PASSWORD でパスワード認証
- 従業員: URL に ?token=xxx 付きでアクセスすると自動ログイン

設計方針:
- 経営者用パスワードは Streamlit Secrets に保存
- 従業員用トークンは秘密の塩から決定論的に生成（DB不要）
- ローカル開発時は BYPASS_AUTH=1 で認証スキップ可能
"""

import os
import sys
from pathlib import Path

import streamlit as st

# プロジェクトルートをパスに追加（このファイルから prototype を読むため）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype.employee_tokens import validate_token, is_salt_configured
from prototype.employees import shift_active_employees


# Streamlit Secrets のキー名
SECRET_MANAGER_PASSWORD = "MANAGER_PASSWORD"
SECRET_BYPASS_AUTH = "BYPASS_AUTH"

# セッションキー
SESSION_AUTHENTICATED = "_auth_authenticated"
SESSION_ROLE = "_auth_role"
SESSION_EMPLOYEE_NAME = "_auth_employee_name"


def _get_secret(key: str, default: str = "") -> str:
    """Streamlit Secrets または環境変数から値を取得"""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


def _is_bypass_enabled() -> bool:
    """認証をスキップするか（ローカル開発時用）"""
    bypass = _get_secret(SECRET_BYPASS_AUTH, "").lower()
    return bypass in ("1", "true", "yes")


def _get_token_from_url() -> str:
    """URL クエリパラメータから token を取得"""
    try:
        params = st.query_params
        token = params.get("token", "")
        # 古いAPI互換性
        if isinstance(token, list):
            token = token[0] if token else ""
        return str(token)
    except Exception:
        return ""


def get_user_role() -> str:
    """現在のユーザーの役割 ("manager" / "employee" / "")"""
    return st.session_state.get(SESSION_ROLE, "")


def get_logged_in_employee() -> str:
    """マジックリンクでログインしている従業員の名前"""
    return st.session_state.get(SESSION_EMPLOYEE_NAME, "")


def is_manager() -> bool:
    return get_user_role() == "manager"


def is_employee() -> bool:
    return get_user_role() == "employee"


def logout() -> None:
    """ログアウト処理"""
    st.session_state[SESSION_AUTHENTICATED] = False
    st.session_state[SESSION_ROLE] = ""
    st.session_state[SESSION_EMPLOYEE_NAME] = ""
    # URLパラメータもクリア
    try:
        st.query_params.clear()
    except Exception:
        pass


def _try_magic_link_login() -> bool:
    """
    URL の token を検証し、有効ならログイン状態にする。
    成功すれば True、失敗（または token 無し）なら False。
    """
    token = _get_token_from_url()
    if not token:
        return False
    if not is_salt_configured():
        return False
    # 全従業員（顧問・補助も含めて検証対象に）
    all_emps = shift_active_employees()
    employee_names = [e.name for e in all_emps]
    matched = validate_token(token, employee_names)
    if matched:
        st.session_state[SESSION_AUTHENTICATED] = True
        st.session_state[SESSION_ROLE] = "employee"
        st.session_state[SESSION_EMPLOYEE_NAME] = matched
        return True
    return False


def require_auth() -> bool:
    """
    認証必須化のメインエントリポイント。
    認証済みなら True を返し、アプリ本体に処理を渡す。
    未認証ならログイン画面を表示し False を返す（呼び出し側で st.stop()）。
    """
    # 既に認証済み
    if st.session_state.get(SESSION_AUTHENTICATED):
        return True

    # ローカル開発時のバイパス
    if _is_bypass_enabled():
        st.session_state[SESSION_AUTHENTICATED] = True
        st.session_state[SESSION_ROLE] = "manager"
        return True

    # マジックリンクでのログインを試みる
    if _try_magic_link_login():
        return True

    # 設定が不十分な場合
    manager_pw_set = bool(_get_secret(SECRET_MANAGER_PASSWORD))
    salt_set = is_salt_configured()
    if not manager_pw_set and not salt_set:
        st.error(
            "🔒 **認証設定が完了していません**\n\n"
            "アプリ管理者は Streamlit Cloud の Settings → Secrets で以下を設定してください：\n"
            "- `MANAGER_PASSWORD`（経営者用パスワード）\n"
            "- `MAGIC_LINK_SALT`（従業員マジックリンク用の秘密の塩）\n\n"
            "ローカル開発時は環境変数 `BYPASS_AUTH=1` で認証をスキップできます。"
        )
        return False

    # 経営者ログイン画面を表示
    _render_manager_login_form()
    return False


def _render_manager_login_form() -> None:
    """経営者用ログイン画面"""
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown(
            """
            <div style="text-align:center; margin-top:60px;">
              <div style="font-size:48px; margin-bottom:8px;">🔒</div>
              <div style="font-size:24px; font-weight:bold; color:#1e3a8a;">
                大黒屋シフト管理システム
              </div>
              <div style="font-size:14px; color:#64748b; margin-top:8px;">
                経営者用ログイン
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("login_form", clear_on_submit=False):
            password = st.text_input(
                "経営者パスワード",
                type="password",
                placeholder="パスワードを入力",
                label_visibility="collapsed",
            )
            submit = st.form_submit_button(
                "ログイン", type="primary", use_container_width=True,
            )

            if submit:
                manager_pw = _get_secret(SECRET_MANAGER_PASSWORD)
                if not password:
                    st.error("パスワードを入力してください")
                elif manager_pw and password == manager_pw:
                    st.session_state[SESSION_AUTHENTICATED] = True
                    st.session_state[SESSION_ROLE] = "manager"
                    st.success("✅ 経営者としてログインしました")
                    st.rerun()
                else:
                    st.error("❌ パスワードが正しくありません")

        # 従業員向けの案内
        st.markdown(
            """
            <div style="margin-top:24px; padding:16px; background:#f1f5f9;
                        border-radius:8px; border-left:4px solid #64748b;">
              <div style="font-weight:bold; color:#475569; margin-bottom:6px;">
                👤 従業員の方へ
              </div>
              <div style="font-size:13px; color:#475569;">
                LINE で経営者から届いた<strong>あなた専用のURL</strong>からアクセスしてください。
                URLをタップするだけで自動的に希望提出画面が開きます。
                <br>URLが分からない場合は経営者にお問い合わせください。
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_logout_button() -> None:
    """ログアウトボタンをサイドバーに描画"""
    if st.session_state.get(SESSION_AUTHENTICATED):
        if is_manager():
            st.sidebar.markdown(f"👤 ログイン中: **経営者**")
        elif is_employee():
            emp_name = get_logged_in_employee() or "従業員"
            st.sidebar.markdown(f"👤 ログイン中: **{emp_name}さん**")
        if st.sidebar.button("🚪 ログアウト", use_container_width=True):
            logout()
            st.rerun()
