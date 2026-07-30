"""
認証モジュール（経営者・シフト担当者：パスワード / 従業員：マジックリンク）
================================================
- 経営者: MANAGER_PASSWORD でパスワード認証
- シフト担当者: SHIFT_MANAGER_PASSWORD でシフト管理機能だけ利用
- 従業員: URL に ?token=xxx 付きでアクセスすると自動ログイン

設計方針:
- 経営者用パスワードは Streamlit Secrets に保存
- 従業員用トークンは秘密の塩から決定論的に生成（DB不要）
- ローカル開発時は BYPASS_AUTH=1 で認証スキップ可能
"""

import os
import sys
import base64
import hashlib
import hmac
import time
from pathlib import Path

import streamlit as st

# プロジェクトルートをパスに追加（このファイルから prototype を読むため）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype.employee_tokens import validate_token, is_salt_configured
from prototype.employees import get_employee, shift_active_employees


# Streamlit Secrets のキー名
SECRET_MANAGER_PASSWORD = "MANAGER_PASSWORD"
SECRET_SHIFT_MANAGER_PASSWORD = "SHIFT_MANAGER_PASSWORD"
SECRET_BYPASS_AUTH = "BYPASS_AUTH"
SECRET_MANAGER_REMEMBER_DAYS = "MANAGER_REMEMBER_DAYS"
QUERY_MANAGER_AUTH = "manager_auth"

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


def _get_query_value(key: str) -> str:
    """URL クエリパラメータを文字列として取得"""
    try:
        value = st.query_params.get(key, "")
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value)
    except Exception:
        return ""


def _get_manager_remember_days() -> int:
    """経営者ログインを保持する日数（デフォルト30日）。"""
    try:
        days = int(_get_secret(SECRET_MANAGER_REMEMBER_DAYS, "30"))
    except ValueError:
        days = 30
    return max(1, min(days, 90))


def _management_auth_secret(role: str) -> str:
    """役割別の期限付きログイントークンの署名に使う秘密値。"""
    password_key = (
        SECRET_SHIFT_MANAGER_PASSWORD
        if role == "shift_manager"
        else SECRET_MANAGER_PASSWORD
    )
    password = _get_secret(password_key)
    if not password:
        return ""
    # MAGIC_LINK_SALT があれば混ぜる。未設定でも管理用PWだけで署名できる。
    salt = _get_secret("MAGIC_LINK_SALT")
    return f"{role}:{password}:{salt}"


def _create_management_auth_token(role: str) -> str:
    """経営者・シフト担当者のログイン保持用トークンを作る。"""
    if role not in {"manager", "shift_manager"}:
        return ""
    secret = _management_auth_secret(role)
    if not secret:
        return ""
    expires_at = int(time.time()) + _get_manager_remember_days() * 24 * 60 * 60
    payload = f"{role}:{expires_at}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify_management_auth_token(token: str) -> str:
    """期限付きログイントークンを検証し、正しい役割を返す。"""
    if "." not in token:
        return ""
    encoded, signature = token.rsplit(".", 1)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        role, expires_at = payload.split(":", 1)
        if role not in {"manager", "shift_manager"}:
            return ""
        secret = _management_auth_secret(role)
        if not secret:
            return ""
        expected = hmac.new(
            secret.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return ""
        return role if int(expires_at) >= int(time.time()) else ""
    except Exception:
        return ""


def _remember_management_login(role: str) -> None:
    """管理系ログインをURLに期限付きで保持する。"""
    token = _create_management_auth_token(role)
    if token:
        try:
            st.query_params[QUERY_MANAGER_AUTH] = token
        except Exception:
            pass


def _try_management_remember_login() -> bool:
    """URLに残った期限付きトークンで管理系ログインを復元する。"""
    token = _get_query_value(QUERY_MANAGER_AUTH)
    role = _verify_management_auth_token(token) if token else ""
    if not role:
        return False
    st.session_state[SESSION_AUTHENTICATED] = True
    st.session_state[SESSION_ROLE] = role
    st.session_state[SESSION_EMPLOYEE_NAME] = ""
    return True


def get_user_role() -> str:
    """現在の役割 ("manager" / "shift_manager" / "employee" / "")"""
    return st.session_state.get(SESSION_ROLE, "")


def get_logged_in_employee() -> str:
    """マジックリンクでログインしている従業員の名前"""
    return st.session_state.get(SESSION_EMPLOYEE_NAME, "")


def is_manager() -> bool:
    return get_user_role() == "manager"


def is_shift_manager() -> bool:
    return get_user_role() == "shift_manager"


def can_manage_shifts() -> bool:
    """提出確認・生成・調整・ロック・月別条件を操作できるか。"""
    return get_user_role() in {"manager", "shift_manager"}


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
    # 通常の提出対象 + 山本さん（補助・特別枠）を検証対象にする
    all_emps = shift_active_employees()
    employee_names = [e.name for e in all_emps]
    try:
        if "山本" not in employee_names:
            employee_names.append(get_employee("山本").name)
    except Exception:
        if "山本" not in employee_names:
            employee_names.append("山本")
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

    # 経営者・シフト担当者ログインの期限付き保持を試みる
    if _try_management_remember_login():
        return True

    # マジックリンクでのログインを試みる
    if _try_magic_link_login():
        return True

    # 設定が不十分な場合
    manager_pw_set = bool(_get_secret(SECRET_MANAGER_PASSWORD))
    shift_manager_pw_set = bool(
        _get_secret(SECRET_SHIFT_MANAGER_PASSWORD)
    )
    salt_set = is_salt_configured()
    if not manager_pw_set and not shift_manager_pw_set and not salt_set:
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
    """経営者・シフト担当者用ログイン画面"""
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
                管理用ログイン
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("login_form", clear_on_submit=False):
            password = st.text_input(
                "管理用パスワード",
                type="password",
                placeholder="パスワードを入力",
                label_visibility="collapsed",
            )
            submit = st.form_submit_button(
                "ログイン", type="primary", width="stretch",
            )

            if submit:
                manager_pw = _get_secret(SECRET_MANAGER_PASSWORD)
                shift_manager_pw = _get_secret(
                    SECRET_SHIFT_MANAGER_PASSWORD
                )
                if not password:
                    st.error("パスワードを入力してください")
                elif manager_pw and password == manager_pw:
                    st.session_state[SESSION_AUTHENTICATED] = True
                    st.session_state[SESSION_ROLE] = "manager"
                    _remember_management_login("manager")
                    st.success("✅ 経営者としてログインしました")
                    st.rerun()
                elif shift_manager_pw and password == shift_manager_pw:
                    st.session_state[SESSION_AUTHENTICATED] = True
                    st.session_state[SESSION_ROLE] = "shift_manager"
                    _remember_management_login("shift_manager")
                    st.success("✅ シフト担当者としてログインしました")
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
            st.sidebar.caption(f"ログイン保持: 最大{_get_manager_remember_days()}日")
        elif is_shift_manager():
            st.sidebar.markdown("👤 ログイン中: **シフト担当者**")
            st.sidebar.caption(
                f"ログイン保持: 最大{_get_manager_remember_days()}日"
            )
        elif is_employee():
            emp_name = get_logged_in_employee() or "従業員"
            st.sidebar.markdown(f"👤 ログイン中: **{emp_name}さん**")
        if st.sidebar.button("🚪 ログアウト", width="stretch"):
            logout()
            st.rerun()
