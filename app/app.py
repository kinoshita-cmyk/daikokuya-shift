"""
大黒屋シフト管理システム - Web UI（Streamlit）
================================================
ブラウザから操作できるシフト管理画面。

実行方法:
    cd <プロジェクトルート>
    streamlit run app/app.py

機能:
- 経営者ビュー（PC）: シフト生成・確認・編集・出力
- 従業員ビュー（スマホ）: 希望提出・確定シフト閲覧
"""

import sys
import os
from pathlib import Path
from typing import Optional

# パス設定: プロジェクトルートと app ディレクトリの両方を Python パスに追加
# これにより `from prototype.X` と `from auth` 両方の形式が動く
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))   # for: from prototype.X import Y
sys.path.insert(0, str(_THIS_DIR))       # for: from auth import Z

import streamlit as st
from datetime import date, datetime
from calendar import monthrange

from prototype.paths import (
    PROJECT_ROOT, DATA_DIR, BACKUP_DIR, OUTPUT_DIR, MAY_2026_SHIFT_XLSX,
)

# 認証モジュール（同じ app/ ディレクトリに配置）
from auth import require_auth, render_logout_button, is_manager, get_user_role


def get_anthropic_api_key() -> Optional[str]:
    """
    Anthropic API キーを以下の優先順位で取得：
    1. Streamlit Secrets（Streamlit Cloud デプロイ時）
    2. 環境変数 ANTHROPIC_API_KEY（ローカル実行時）
    3. セッションに登録された値（設定画面から入力）
    """
    # 1. Streamlit Secrets（クラウド用）
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return str(st.secrets["ANTHROPIC_API_KEY"])
    except Exception:
        pass
    # 2. 環境変数（ローカル用）
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    # 3. セッション
    return st.session_state.get("api_key")
from prototype.models import Store, OperationMode, ShiftAssignment, MonthlyShift
from prototype.employees import ALL_EMPLOYEES, shift_active_employees
from prototype.generator import generate_shift, determine_operation_modes
from prototype.validator import validate
from prototype.backup import ShiftBackup
from prototype.excel_loader import load_shift_from_excel
from prototype.excel_exporter import export_shift_to_excel, EXPORT_COLUMN_ORDER
from prototype.pdf_exporter import export_shift_to_pdf
from prototype.shift_chat import ShiftChatEngine, HAS_ANTHROPIC
from prototype.shift_lock import ShiftLockManager
from prototype.rule_config import RuleConfigManager, RuleConfig, CustomRule, DEFAULT_ENABLED_CHECKS, DEFAULT_PARAMETERS
from prototype.employee_config import (
    EmployeeConfigManager, get_active_employees, get_all_employees_including_retired,
)
from prototype.models import EmploymentStatus, Skill, Role, Store, StationType, Affinity, Employee
from prototype.may_2026_data import (
    OFF_REQUESTS, WORK_REQUESTS, PREVIOUS_MONTH_CARRYOVER, FLEXIBLE_OFF_REQUESTS,
)
from prototype.rules import MAY_2026_HOLIDAY_OVERRIDES


# ============================================================
# ページ設定
# ============================================================

st.set_page_config(
    page_title="大黒屋シフト管理",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# 認証チェック（認証していなければログイン画面を表示してここで停止）
# ============================================================
if not require_auth():
    st.stop()

# CSS カスタマイズ（高齢者にも見やすい大きさ）
st.markdown("""
<style>
    .main { font-size: 16px; }
    .stButton > button {
        font-size: 18px;
        padding: 8px 24px;
        font-weight: bold;
    }
    h1 { color: #1e3a8a; }
    h2 { color: #2563eb; border-bottom: 2px solid #2563eb; padding-bottom: 4px; }
    .stDataFrame { font-size: 14px; }
    .shift-cell-akabane { background: #fef3c7; }
    .shift-cell-higashi { background: #dbeafe; }
    .shift-cell-omiya { background: #d1fae5; }
    .shift-cell-nishi { background: #fce7f3; }
    .shift-cell-suzuran { background: #e0e7ff; }
    .shift-cell-off { background: #f3f4f6; color: #6b7280; }
</style>
""", unsafe_allow_html=True)


# ============================================================
# サイドバー（ナビゲーション）
# ============================================================

st.sidebar.title("📅 大黒屋シフト管理")
st.sidebar.markdown("---")

# 役割に応じてアクセス可能なモードを制御
# 経営者: すべて閲覧可
# 従業員: 「従業員ビュー」と「過去シフト閲覧」のみ
if is_manager():
    available_modes = ["📊 経営者ビュー", "👤 従業員ビュー", "📁 過去シフト閲覧", "⚙️ 設定"]
else:
    # 従業員ロールは設定画面・経営者画面にアクセスできない
    available_modes = ["👤 従業員ビュー", "📁 過去シフト閲覧"]

mode = st.sidebar.radio(
    "モードを選択",
    options=available_modes,
    index=0,
)

st.sidebar.markdown("---")
# ログアウトボタン
render_logout_button()
st.sidebar.markdown("---")
st.sidebar.caption("v0.1 プロトタイプ")
st.sidebar.caption(f"今日: {date.today()}")


# ============================================================
# ヘルパー関数
# ============================================================

def detect_short_staff_days(shift: MonthlyShift) -> set[int]:
    """
    人員不足日を検出する（Validatorと同じ判定ロジックを使用）。
    判定基準:
    - 大宮駅前店のエコが1名のみ（NORMAL モードで本来2名必要）
    - 各日の総人員が必要数（モード別）を下回る
    """
    from prototype.rules import NORMAL_CAPACITY, REDUCED_CAPACITY, MINIMUM_CAPACITY
    from prototype.employees import get_employee
    from prototype.models import Skill

    short_days: set[int] = set()
    days_in_month = monthrange(shift.year, shift.month)[1]
    capacity_by_mode = {
        OperationMode.NORMAL: NORMAL_CAPACITY,
        OperationMode.REDUCED: REDUCED_CAPACITY,
        OperationMode.MINIMUM: MINIMUM_CAPACITY,
    }

    for d in range(1, days_in_month + 1):
        mode = shift.operation_modes.get(d, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        cap = capacity_by_mode.get(mode, NORMAL_CAPACITY)

        # 各店舗の実勤務者を集計
        day_assigns = shift.get_day_assignments(d)
        store_eco: dict = {}
        store_ticket: dict = {}
        for a in day_assigns:
            if a.store == Store.OFF:
                continue
            try:
                emp = get_employee(a.employee)
            except KeyError:
                continue
            if emp.is_auxiliary:
                continue
            if emp.skill == Skill.ECO:
                store_eco[a.store] = store_eco.get(a.store, 0) + 1
            else:
                store_ticket[a.store] = store_ticket.get(a.store, 0) + 1

        # 大宮 1名体制チェック（NORMAL モードのみ）
        if mode == OperationMode.NORMAL:
            omiya_eco = store_eco.get(Store.OMIYA, 0)
            if omiya_eco < 2:
                short_days.add(d)
                continue

        # 各店舗の最低人数チェック
        for store, store_cap in cap.items():
            weekday = date(shift.year, shift.month, d).weekday()
            if weekday in store_cap.closed_dow:
                continue
            eco_count = store_eco.get(store, 0)
            ticket_count = store_ticket.get(store, 0)
            if eco_count < store_cap.eco_min or ticket_count < store_cap.ticket_min:
                short_days.add(d)
                break

    return short_days


def render_shift_table(
    shift: MonthlyShift,
    short_staff_days: Optional[set[int]] = None,
) -> None:
    """シフト表をHTMLテーブルで表示"""
    days_in_month = monthrange(shift.year, shift.month)[1]
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    # 人員不足日を自動検出（指定がなければ）
    if short_staff_days is None:
        short_staff_days = detect_short_staff_days(shift)

    # ヘッダー
    html = '<div style="overflow-x:auto;"><table style="border-collapse:collapse; font-family:sans-serif; font-size:14px;">'
    html += '<thead><tr style="background:#1e3a8a; color:white;">'
    html += '<th style="padding:8px; border:1px solid #999;">日</th>'
    html += '<th style="padding:8px; border:1px solid #999;">曜</th>'
    for name in EXPORT_COLUMN_ORDER:
        html += f'<th style="padding:8px; border:1px solid #999;">{name}</th>'
    html += '<th style="padding:8px; border:1px solid #999;">人員少</th>'
    html += '</tr></thead><tbody>'

    # 各日
    color_map = {
        "○": "#fef3c7", "〇": "#fef3c7",
        "□": "#dbeafe",
        "△": "#d1fae5",
        "☆": "#fce7f3",
        "◆": "#e0e7ff",
        "×": "#f3f4f6",
    }

    for d in range(1, days_in_month + 1):
        wd = weekday_jp[date(shift.year, shift.month, d).weekday()]
        # 人員不足日は背景強調
        is_short = d in short_staff_days
        if is_short:
            bg = "#fff3cd"  # 黄色強調
        else:
            bg = "#fee2e2" if wd == "日" else ("#dbeafe" if wd == "土" else "white")
        html += f'<tr style="background:{bg};">'
        html += f'<td style="padding:6px; border:1px solid #ccc; text-align:center; font-weight:bold;">{d}</td>'
        html += f'<td style="padding:6px; border:1px solid #ccc; text-align:center;">{wd}</td>'

        # 各従業員
        day_assignments = shift.get_day_assignments(d)
        for name in EXPORT_COLUMN_ORDER:
            a = next((x for x in day_assignments if x.employee == name), None)
            if a is None:
                cell_text = ""
                cell_bg = "white"
            else:
                cell_text = a.store.value
                cell_bg = color_map.get(cell_text, "white")
            html += (
                f'<td style="padding:6px; border:1px solid #ccc; '
                f'text-align:center; background:{cell_bg}; font-size:16px;">{cell_text}</td>'
            )

        # 人員少マーク
        short_mark = "△" if is_short else ""
        short_bg = "#fff3cd" if is_short else "white"
        html += (
            f'<td style="padding:6px; border:1px solid #ccc; '
            f'text-align:center; background:{short_bg}; '
            f'font-weight:bold; font-size:18px; color:#92400e;">{short_mark}</td>'
        )
        html += '</tr>'

    html += '</tbody></table></div>'
    st.markdown(html, unsafe_allow_html=True)


def get_session_shift() -> Optional[MonthlyShift]:
    """セッションに保存されたシフトを取得"""
    return st.session_state.get("current_shift")


def save_session_shift(shift: MonthlyShift) -> None:
    """シフトをセッションに保存"""
    st.session_state["current_shift"] = shift


# ============================================================
# 経営者ビュー
# ============================================================

if mode == "📊 経営者ビュー":
    st.title("📊 シフト管理ダッシュボード（経営者用）")

    lock_mgr = ShiftLockManager()
    backup_mgr = ShiftBackup()
    rule_mgr = RuleConfigManager()
    rule_cfg = rule_mgr.load()  # 現在のアクティブルール設定

    # ============================================================
    # 対象年月の決定（デフォルト＝翌月）
    # ============================================================
    today = date.today()
    # 翌月の年・月を計算
    if today.month == 12:
        next_month_year, next_month_month = today.year + 1, 1
    else:
        next_month_year, next_month_month = today.year, today.month + 1
    # 翌々月
    if next_month_month == 12:
        nn_year, nn_month = next_month_year + 1, 1
    else:
        nn_year, nn_month = next_month_year, next_month_month + 1
    # 前月
    if today.month == 1:
        prev_month_year, prev_month_month = today.year - 1, 12
    else:
        prev_month_year, prev_month_month = today.year, today.month - 1

    # セッションに対象年月を保持（リロードしても維持）
    if "target_year" not in st.session_state:
        st.session_state["target_year"] = next_month_year
    if "target_month" not in st.session_state:
        st.session_state["target_month"] = next_month_month

    # クイック切替ボタン
    st.markdown("##### 📅 表示する対象月")
    qcol1, qcol2, qcol3, qcol4, qcol5 = st.columns([1, 1, 1, 1, 3])
    with qcol1:
        if st.button(f"前月\n({prev_month_year}/{prev_month_month})",
                     key="qb_prev", use_container_width=True):
            st.session_state["target_year"] = prev_month_year
            st.session_state["target_month"] = prev_month_month
            st.rerun()
    with qcol2:
        if st.button(f"今月\n({today.year}/{today.month})",
                     key="qb_curr", use_container_width=True):
            st.session_state["target_year"] = today.year
            st.session_state["target_month"] = today.month
            st.rerun()
    with qcol3:
        # 翌月ボタン（デフォルト＝強調表示）
        if st.button(f"📌 翌月\n({next_month_year}/{next_month_month})",
                     key="qb_next", type="primary", use_container_width=True,
                     help="通常はこちらを選択（提出締切は今月25日）"):
            st.session_state["target_year"] = next_month_year
            st.session_state["target_month"] = next_month_month
            st.rerun()
    with qcol4:
        if st.button(f"翌々月\n({nn_year}/{nn_month})",
                     key="qb_nnext", use_container_width=True):
            st.session_state["target_year"] = nn_year
            st.session_state["target_month"] = nn_month
            st.rerun()

    # 任意の年月を選択するための数値入力（折りたたみ式）
    with qcol5:
        with st.expander("🔧 任意の年月を選択", expanded=False):
            ec1, ec2 = st.columns(2)
            with ec1:
                custom_year = st.number_input(
                    "年", min_value=2024, max_value=2030,
                    value=st.session_state["target_year"],
                    key="custom_year_input",
                )
            with ec2:
                custom_month = st.number_input(
                    "月", min_value=1, max_value=12,
                    value=st.session_state["target_month"],
                    key="custom_month_input",
                )
            if (custom_year != st.session_state["target_year"]
                    or custom_month != st.session_state["target_month"]):
                st.session_state["target_year"] = int(custom_year)
                st.session_state["target_month"] = int(custom_month)
                st.rerun()

    # 確定した対象年月
    target_year = st.session_state["target_year"]
    target_month = st.session_state["target_month"]

    # 提出締切のカウントダウン（翌月分の場合は今月25日が締切）
    deadline_year, deadline_month = target_year, target_month - 1
    if deadline_month == 0:
        deadline_year, deadline_month = target_year - 1, 12
    try:
        deadline = date(deadline_year, deadline_month, 25)
    except ValueError:
        deadline = None

    # 状況サマリー（現在閲覧中の月＋締切情報）
    if deadline:
        days_to_deadline = (deadline - today).days
        if days_to_deadline > 0:
            deadline_msg = (
                f'<span style="color:#15803d; font-weight:bold;">'
                f'📅 提出締切まであと <strong>{days_to_deadline}日</strong>'
                f'（{deadline.year}/{deadline.month}/{deadline.day}）</span>'
            )
        elif days_to_deadline == 0:
            deadline_msg = (
                f'<span style="color:#dc2626; font-weight:bold;">'
                f'🚨 今日が提出締切日（{deadline.year}/{deadline.month}/{deadline.day}）</span>'
            )
        else:
            deadline_msg = (
                f'<span style="color:#dc2626; font-weight:bold;">'
                f'⚠ 提出締切（{deadline.year}/{deadline.month}/{deadline.day}）'
                f'を <strong>{abs(days_to_deadline)}日</strong>過ぎています</span>'
            )
    else:
        deadline_msg = ""

    # 翌月以外の月を選択中なら注意表示
    is_recommended_month = (
        target_year == next_month_year and target_month == next_month_month
    )
    if not is_recommended_month:
        st.warning(
            f"⚠ 通常は **翌月（{next_month_year}年{next_month_month}月）** を選択してください。"
            f"現在 **{target_year}年{target_month}月** を表示中です。"
        )

    st.markdown(
        f'<div style="background:#f8fafc; padding:10px 14px; border-radius:6px; '
        f'margin:6px 0; border-left:4px solid #475569;">'
        f'<span style="font-size:16px; font-weight:bold;">'
        f'🎯 表示中: <strong style="color:#1e3a8a;">{target_year}年{target_month}月</strong>'
        f'</span>　{deadline_msg}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ロック状態を確認・表示
    lock_info = lock_mgr.get_lock_info(int(target_year), int(target_month))
    if lock_info:
        st.markdown(
            f'<div style="background:#dbeafe; padding:10px; border-radius:6px; '
            f'border-left:4px solid #2563eb;">'
            f'🔒 <strong>{lock_info.year}年{lock_info.month}月は確定版でロック中</strong>　'
            f'<span style="font-size:13px; color:#475569;">'
            f'{lock_info.locked_at[:19]} ・ {lock_info.locked_by} ・ {lock_info.note}'
            f'</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background:#fef3c7; padding:10px; border-radius:6px; '
            'border-left:4px solid #f59e0b;">'
            '🔓 ロックなし（生成・編集可能）'
            '</div>',
            unsafe_allow_html=True,
        )

    # ============================================================
    # 希望シフト提出状況（リアルタイム）
    # ============================================================
    expected_employees = [
        e.name for e in shift_active_employees() if not e.is_auxiliary
    ]
    submission_status = backup_mgr.get_submission_status(
        int(target_year), int(target_month), expected_employees,
    )
    summary = submission_status["summary"]

    # 提出状況サマリーを目立つボックスで表示
    completion_pct = int(summary["completion_rate"] * 100)
    if summary["total_pending"] == 0:
        # 全員提出済み
        st.markdown(
            f'<div style="background:#dcfce7; padding:14px 16px; border-radius:8px; '
            f'border-left:6px solid #16a34a; margin:8px 0;">'
            f'<div style="font-size:18px; font-weight:bold; color:#14532d;">'
            f'✅ {int(target_month)}月分 全員提出済み（{summary["total_submitted"]}/{summary["total_expected"]}名）'
            f'</div>'
            f'<div style="font-size:13px; color:#166534; margin-top:4px;">'
            f'シフト生成の準備が整いました。'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    elif summary["total_submitted"] == 0:
        # 誰も提出していない
        st.markdown(
            f'<div style="background:#fee2e2; padding:14px 16px; border-radius:8px; '
            f'border-left:6px solid #dc2626; margin:8px 0;">'
            f'<div style="font-size:18px; font-weight:bold; color:#7f1d1d;">'
            f'⏳ {int(target_month)}月分 提出状況：0/{summary["total_expected"]}名（未開始）'
            f'</div>'
            f'<div style="font-size:13px; color:#991b1b; margin-top:4px;">'
            f'まだ誰も希望を提出していません。従業員にお声がけしてください。'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    else:
        # 一部提出済み
        st.markdown(
            f'<div style="background:#fef3c7; padding:14px 16px; border-radius:8px; '
            f'border-left:6px solid #f59e0b; margin:8px 0;">'
            f'<div style="font-size:18px; font-weight:bold; color:#78350f;">'
            f'⏳ {int(target_month)}月分 提出状況：{summary["total_submitted"]}/{summary["total_expected"]}名（{completion_pct}%）'
            f'</div>'
            f'<div style="background:#fde68a; height:8px; border-radius:4px; margin:8px 0; overflow:hidden;">'
            f'<div style="background:#16a34a; height:100%; width:{completion_pct}%;"></div>'
            f'</div>'
            f'<div style="font-size:13px; color:#92400e;">'
            f'⚠ 未提出 {summary["total_pending"]}名: <strong>{", ".join(submission_status["not_submitted"])}</strong>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    # 詳細表示（折りたたみ式）
    with st.expander(
        f"📥 提出状況の詳細を見る（提出済み{summary['total_submitted']}名・未提出{summary['total_pending']}名）",
        expanded=(summary["total_pending"] > 0 and summary["total_pending"] < summary["total_expected"]),
    ):
        detail_col1, detail_col2 = st.columns([3, 2])

        # 提出済み詳細
        with detail_col1:
            st.markdown("##### ✅ 提出済み")
            if not submission_status["submitted"]:
                st.caption("まだ誰も提出していません")
            else:
                submitted_data = []
                for s in submission_status["submitted"]:
                    submitted_data.append({
                        "氏名": s["employee"],
                        "提出日時": s["submitted_at"][:19].replace("T", " "),
                        "休み希望": f"{s['off_request_count']}日",
                        "△希望": f"{s['flexible_off_count']}件",
                        "備考": "📝 あり" if s["has_note"] else "",
                    })
                st.dataframe(submitted_data, use_container_width=True, hide_index=True)

                # 備考のあるものだけ展開表示
                has_notes = [s for s in submission_status["submitted"] if s["has_note"]]
                if has_notes:
                    st.markdown("**📝 自由記述コメント:**")
                    for s in has_notes:
                        st.markdown(
                            f'<div style="background:#f0f9ff; padding:6px 10px; '
                            f'margin:4px 0; border-left:3px solid #0ea5e9; font-size:13px;">'
                            f'<strong>{s["employee"]}</strong>: {s["note_excerpt"]}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

        # 未提出詳細
        with detail_col2:
            st.markdown("##### ⏳ 未提出（要催促）")
            if not submission_status["not_submitted"]:
                st.success("全員提出済みです 🎉")
            else:
                pending_html = ""
                for name in submission_status["not_submitted"]:
                    pending_html += (
                        f'<div style="background:#fef2f2; padding:6px 10px; '
                        f'margin:3px 0; border-left:3px solid #ef4444; '
                        f'font-size:14px; font-weight:bold; color:#991b1b;">'
                        f'⏳ {name}</div>'
                    )
                st.markdown(pending_html, unsafe_allow_html=True)

                # 催促用テンプレ
                st.markdown("---")
                st.caption("📨 LINE送信用テンプレート（クリックでコピー可）:")
                template = (
                    f"【{int(target_month)}月分シフト希望提出のお願い】\n"
                    f"恐れ入ります、まだ提出されていない方はお早めに提出をお願いいたします。\n"
                    f"未提出: {', '.join(submission_status['not_submitted'])}"
                )
                st.code(template, language=None)

        # 更新ボタン
        if st.button("🔄 提出状況を更新", key="refresh_submissions"):
            st.rerun()

    st.markdown("---")

    # 操作ボタン群
    st.markdown("##### 操作")
    bcol1, bcol2, bcol3, bcol4 = st.columns(4)

    with bcol1:
        # 生成ボタン: ロック中は無効、未提出者がいれば警告
        gen_disabled = lock_info is not None
        if gen_disabled:
            gen_help = "ロック解除してから再生成してください"
        elif summary["total_pending"] > 0:
            gen_help = f"⚠ {summary['total_pending']}名 未提出です。それでも生成しますか？"
        else:
            gen_help = "AIが希望データから新規シフトを作成します"

        # 確認モード（未提出者がいる場合は2段階確認）
        gen_button_label = "🔄 シフトを自動生成"
        if summary["total_pending"] > 0 and not gen_disabled:
            gen_button_label = f"⚠ シフトを自動生成（{summary['total_pending']}名未提出）"

        if st.button(
            gen_button_label,
            type="primary",
            use_container_width=True,
            disabled=gen_disabled,
            help=gen_help,
        ):
            # 未提出者がいる場合は確認ダイアログ
            if summary["total_pending"] > 0 and not st.session_state.get("confirm_gen_with_pending"):
                st.session_state["confirm_gen_with_pending"] = True
                st.warning(
                    f"⚠ {summary['total_pending']}名（{', '.join(submission_status['not_submitted'])}）が"
                    "未提出です。もう一度ボタンを押すと未提出者を含まずに生成します。"
                )
                st.stop()
            st.session_state["confirm_gen_with_pending"] = False
            with st.spinner("AIがシフト案を生成中... (1〜2分かかる場合があります)"):
                modes = determine_operation_modes(target_year, target_month)
                shift = generate_shift(
                    year=target_year,
                    month=target_month,
                    off_requests=OFF_REQUESTS,
                    work_requests=WORK_REQUESTS,
                    prev_month=PREVIOUS_MONTH_CARRYOVER,
                    flexible_off=FLEXIBLE_OFF_REQUESTS,
                    holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
                    operation_modes=modes,
                    consec_exceptions=["野澤"],
                    max_consec_override=rule_cfg.parameters.get("max_consec_work", 5),
                    time_limit_seconds=rule_cfg.parameters.get("solver_time_limit_seconds", 120),
                    random_seed=rule_cfg.parameters.get("solver_seed", 42),
                    verbose=False,
                )
                if shift is not None:
                    save_session_shift(shift)
                    st.success("✅ シフト生成完了！")
                else:
                    st.error("❌ シフト生成失敗")

    with bcol2:
        # 確定版を読み込む
        if lock_info is not None:
            if st.button(
                "📥 確定版を読み込む",
                use_container_width=True,
                help="ロック済みの確定版シフトをセッションに復元",
            ):
                snapshot_path = (
                    BACKUP_DIR
                    / f"{lock_info.year}-{lock_info.month:02d}"
                    / lock_info.snapshot_file
                )
                if snapshot_path.exists():
                    loaded = backup_mgr.load_shift(snapshot_path)
                    save_session_shift(loaded)
                    st.success("✅ 確定版を読み込みました")
                    st.rerun()
                else:
                    st.error(f"スナップショットが見つかりません: {snapshot_path}")
        else:
            st.button(
                "📥 確定版を読み込む",
                use_container_width=True,
                disabled=True,
                help="このシフトはまだロックされていません",
            )

    with bcol3:
        # ロック / 解除ボタン
        current_shift = get_session_shift()
        if lock_info is None:
            # 未ロック: ロックボタン表示
            if st.button(
                "🔒 確定版としてロック",
                use_container_width=True,
                disabled=current_shift is None,
                type="secondary",
                help="現在のシフトを確定版として保存し、編集をロックします",
            ):
                st.session_state["show_lock_dialog"] = True
        else:
            # ロック済み: 解除ボタン表示
            if st.button(
                "🔓 ロックを解除",
                use_container_width=True,
                type="secondary",
                help="編集できる状態に戻します（バックアップは残ります）",
            ):
                st.session_state["show_unlock_dialog"] = True

    with bcol4:
        # ロック一覧表示
        with st.popover("📅 ロック済み一覧", use_container_width=True):
            all_locks = lock_mgr.list_locks()
            if not all_locks:
                st.write("ロック済みシフトはありません")
            else:
                for lk in all_locks:
                    st.markdown(
                        f"🔒 **{lk.year}年{lk.month}月**　"
                        f"_{lk.locked_at[:10]}_　"
                        f"by {lk.locked_by}"
                    )
                    if lk.note:
                        st.caption(lk.note)

    # ============================================================
    # ロック・解除ダイアログ
    # ============================================================
    if st.session_state.get("show_lock_dialog"):
        with st.form("lock_form", clear_on_submit=True):
            st.markdown("### 🔒 シフトを確定版としてロック")
            st.write(
                f"**{int(target_year)}年{int(target_month)}月** のシフトを"
                f"確定版として保存します。ロック中は再生成・編集が制限されます。"
            )
            lock_note = st.text_input(
                "メモ（任意）",
                placeholder=f"例: {int(target_month)}月分 確定版（顧問承認済み）",
            )
            lock_author = st.text_input("実行者名", value="代表取締役")
            col_a, col_b = st.columns(2)
            with col_a:
                submit_lock = st.form_submit_button("✅ ロックする", type="primary", use_container_width=True)
            with col_b:
                cancel_lock = st.form_submit_button("キャンセル", use_container_width=True)
            if submit_lock and current_shift is not None:
                # バックアップ保存
                snapshot_path = backup_mgr.save_shift(
                    current_shift, kind="finalized",
                    author=lock_author, note=lock_note,
                )
                # ロック登録
                lock_mgr.lock(
                    year=int(target_year), month=int(target_month),
                    locked_by=lock_author,
                    snapshot_file=snapshot_path.name,
                    note=lock_note,
                )
                st.success(f"✅ {int(target_year)}年{int(target_month)}月をロックしました")
                st.session_state["show_lock_dialog"] = False
                st.rerun()
            if cancel_lock:
                st.session_state["show_lock_dialog"] = False
                st.rerun()

    if st.session_state.get("show_unlock_dialog"):
        with st.form("unlock_form", clear_on_submit=True):
            st.markdown("### 🔓 ロックを解除")
            st.warning(
                f"**{int(target_year)}年{int(target_month)}月** のロックを解除します。"
                "解除すると再生成・編集が可能になります（バックアップは残ります）。"
            )
            col_a, col_b = st.columns(2)
            with col_a:
                submit_unlock = st.form_submit_button("✅ 解除する", type="primary", use_container_width=True)
            with col_b:
                cancel_unlock = st.form_submit_button("キャンセル", use_container_width=True)
            if submit_unlock:
                lock_mgr.unlock(int(target_year), int(target_month))
                st.success(f"✅ ロックを解除しました")
                st.session_state["show_unlock_dialog"] = False
                st.rerun()
            if cancel_unlock:
                st.session_state["show_unlock_dialog"] = False
                st.rerun()

    st.markdown("---")

    # シフト表示
    shift = get_session_shift()
    if shift is None:
        st.info("👆 上のボタンを押してシフトを生成してください")
    else:
        # タブで切り替え
        tab1, tab2, tab3, tab4, tab5 = st.tabs(["📋 シフト表", "✅ 検証結果", "📊 統計", "📥 出力", "💬 AI対話"])

        with tab1:
            # コメント欄（表の上）— Excel出力時にも反映される
            st.markdown("##### 📝 上部コメント（Excel/PDF出力に反映）")
            col_cm1, col_cm2, col_cm3 = st.columns(3)
            with col_cm1:
                comment1 = st.text_input(
                    "1行目",
                    value=st.session_state.get("excel_comment_1", ""),
                    placeholder="例: AI 自動生成版です",
                    key="comment_1_input",
                )
                st.session_state["excel_comment_1"] = comment1
            with col_cm2:
                comment2 = st.text_input(
                    "2行目",
                    value=st.session_state.get("excel_comment_2", ""),
                    placeholder="例: 5月は全体にお休みを増やしています",
                    key="comment_2_input",
                )
                st.session_state["excel_comment_2"] = comment2
            with col_cm3:
                comment3 = st.text_input(
                    "3行目",
                    value=st.session_state.get("excel_comment_3", ""),
                    placeholder="例: 次回からGoogleフォームに入力予定",
                    key="comment_3_input",
                )
                st.session_state["excel_comment_3"] = comment3

            st.markdown("---")

            # 凡例
            st.markdown(
                "**凡例**　"
                "🟡 〇 = 赤羽駅前店　🔵 □ = 赤羽東口店　🟢 △ = 大宮駅前店　"
                "🟣 ☆ = 大宮西口店　🔷 ◆ = 大宮すずらん通り店　⚪ × = 休み"
            )

            # 人員不足日を計算
            short_days = detect_short_staff_days(shift)
            if short_days:
                st.warning(
                    f"⚠ 人員不足の日: 5/{', 5/'.join(map(str, sorted(short_days)))}"
                    f"（黄色でハイライト・人員少欄に △ 表示）"
                )

            render_shift_table(shift, short_staff_days=short_days)

            st.markdown("---")
            st.markdown("##### 📝 下部注意書き（Excel/PDF出力に反映）")
            footer_default = (
                "※25日までに翌月のお休み又は出勤希望日を、ご連絡ください。（お忘れなく！！）\n"
                "※出勤基準日数（の目安）と違いがある場合は、希望するお休み日数と消化する有給休暇日数もお願いします。\n"
                "※出勤簿は月末までに、赤羽に到着するように提出してください。"
            )
            footer_text = st.text_area(
                "注意書き（1行ごとに分けて記入）",
                value=st.session_state.get("excel_footer", footer_default),
                height=120,
                key="footer_text_input",
            )
            st.session_state["excel_footer"] = footer_text

        with tab2:
            result = validate(
                shift=shift, work_requests=WORK_REQUESTS,
                off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
                holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
                max_consec=5,
            )
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("エラー", result.error_count, delta_color="inverse")
            col_b.metric("警告", result.warning_count, delta_color="inverse")
            col_c.metric("合計問題", result.error_count + result.warning_count, delta_color="inverse")

            if result.error_count == 0:
                st.success("✨ すべてのハード制約を満たしています！")
            else:
                st.error("以下のエラーがあります：")
                for issue in result.issues:
                    if issue.severity == "ERROR":
                        st.write(f"❌ {issue}")

            with st.expander(f"⚠ 警告 {result.warning_count} 件"):
                for issue in result.issues:
                    if issue.severity == "WARNING":
                        st.write(f"⚠ {issue}")

        with tab3:
            # 出勤日数統計
            from prototype.employees import ALL_EMPLOYEES
            data = []
            for e in ALL_EMPLOYEES:
                if e.is_auxiliary:
                    continue
                days_in_month = monthrange(shift.year, shift.month)[1]
                work = sum(1 for d in range(1, days_in_month+1)
                           if (a := shift.get_assignment(e.name, d)) and a.store != Store.OFF)
                off = days_in_month - work
                target = round(e.annual_target_days / 12) if e.annual_target_days else None
                diff = (work - target) if target else None
                data.append({
                    "氏名": e.name,
                    "出勤": work,
                    "休": off,
                    "目標": target if target else "-",
                    "差分": f"{diff:+d}" if diff is not None else "-",
                })
            st.dataframe(data, use_container_width=True, hide_index=True)

        with tab4:
            output_dir = OUTPUT_DIR
            output_dir.mkdir(exist_ok=True)

            # 「📋 シフト表」タブで設定したコメント・注意書きを取得
            header_comments = [
                st.session_state.get("excel_comment_1", ""),
                st.session_state.get("excel_comment_2", ""),
                st.session_state.get("excel_comment_3", ""),
            ]
            footer_text = st.session_state.get("excel_footer", "")
            footer_notes = [line for line in footer_text.split("\n") if line.strip()]
            short_days_for_export = sorted(detect_short_staff_days(shift))

            st.info(
                "📝 「📋 シフト表」タブで入力したコメントと注意書きが反映されます。"
                "未入力の場合は空欄になります。"
            )

            col_x, col_p = st.columns(2)
            with col_x:
                st.write("**📁 Excel 形式（編集可）**")
                if st.button("Excel を生成", key="gen_xlsx"):
                    file_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.xlsx"
                    export_shift_to_excel(
                        shift, file_path,
                        header_comments=header_comments,
                        footer_notes=footer_notes if footer_notes else None,
                        short_staff_days=short_days_for_export,
                    )
                    st.success(f"✅ 保存先: {file_path}")
                xlsx_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.xlsx"
                if xlsx_path.exists():
                    with open(xlsx_path, "rb") as f:
                        st.download_button(
                            label="⬇ Excel ダウンロード",
                            data=f.read(),
                            file_name=xlsx_path.name,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="dl_xlsx",
                        )

            with col_p:
                st.write("**📄 PDF 形式（印刷用・A4横1枚）**")
                if st.button("PDF を生成", key="gen_pdf"):
                    file_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.pdf"
                    export_shift_to_pdf(
                        shift, file_path,
                        header_notes=[c for c in header_comments if c.strip()] or ["AI 自動生成版"],
                    )
                    st.success(f"✅ 保存先: {file_path}")
                pdf_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.pdf"
                if pdf_path.exists():
                    with open(pdf_path, "rb") as f:
                        st.download_button(
                            label="⬇ PDF ダウンロード",
                            data=f.read(),
                            file_name=pdf_path.name,
                            mime="application/pdf",
                            key="dl_pdf",
                        )

            st.markdown("---")
            st.write("**💾 バックアップに保存**")
            note = st.text_input("メモ（任意）", placeholder="例: 6月分の確定版")
            if st.button("バックアップ保存", key="save_backup"):
                backup = ShiftBackup()
                kind = "finalized" if note else "draft"
                path = backup.save_shift(shift, kind=kind, author="代表取締役", note=note)
                st.success(f"✅ バックアップ保存: {path.name}")

        with tab5:
            st.write("### 💬 シフトを見ながら AI と対話")
            st.caption(
                "シフト表を上に、AIとの対話を下に表示します。"
                "AIの提案に応じてシフトを変更したい場合は、対話画面で指示してください。"
            )

            api_key = get_anthropic_api_key()

            # ============================================================
            # 上部: シフト表（コンパクト版）+ 検証結果サマリー
            # ============================================================
            # まず検証を実行してエラー・警告を取得
            chat_result = validate(
                shift=shift, work_requests=WORK_REQUESTS,
                off_requests=OFF_REQUESTS, prev_month=PREVIOUS_MONTH_CARRYOVER,
                holiday_overrides=MAY_2026_HOLIDAY_OVERRIDES,
                max_consec=5,
            )

            # サマリー行（コンパクト表示）
            summary_col1, summary_col2, summary_col3 = st.columns([1, 1, 2])
            with summary_col1:
                if chat_result.error_count == 0:
                    st.markdown(
                        '<div style="background:#dcfce7; padding:8px; border-radius:6px; '
                        'text-align:center; font-weight:bold; color:#166534;">'
                        '✅ エラー 0件</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div style="background:#fee2e2; padding:8px; border-radius:6px; '
                        f'text-align:center; font-weight:bold; color:#991b1b;">'
                        f'❌ エラー {chat_result.error_count}件</div>',
                        unsafe_allow_html=True,
                    )
            with summary_col2:
                if chat_result.warning_count == 0:
                    st.markdown(
                        '<div style="background:#dcfce7; padding:8px; border-radius:6px; '
                        'text-align:center; font-weight:bold; color:#166534;">'
                        '✓ 警告 0件</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div style="background:#fef3c7; padding:8px; border-radius:6px; '
                        f'text-align:center; font-weight:bold; color:#92400e;">'
                        f'⚠ 警告 {chat_result.warning_count}件</div>',
                        unsafe_allow_html=True,
                    )
            with summary_col3:
                short_days_chat = detect_short_staff_days(shift)
                if short_days_chat:
                    st.markdown(
                        f'<div style="background:#fef3c7; padding:8px; border-radius:6px; '
                        f'text-align:center; font-weight:bold; color:#92400e;">'
                        f'👥 人員不足: 5/{", 5/".join(map(str, sorted(short_days_chat)))}</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div style="background:#dcfce7; padding:8px; border-radius:6px; '
                        'text-align:center; font-weight:bold; color:#166534;">'
                        '👥 人員充足</div>',
                        unsafe_allow_html=True,
                    )

            # エラー・警告の詳細（折りたたみ式）
            if chat_result.error_count > 0 or chat_result.warning_count > 0:
                with st.expander(
                    f"🔍 エラー・警告の詳細を見る（{chat_result.error_count + chat_result.warning_count}件）",
                    expanded=False,
                ):
                    if chat_result.error_count > 0:
                        st.markdown("**❌ エラー（要修正）**")
                        for issue in chat_result.issues:
                            if issue.severity == "ERROR":
                                st.markdown(
                                    f'<div style="background:#fef2f2; border-left:4px solid #ef4444; '
                                    f'padding:6px 10px; margin:4px 0; font-size:13px;">'
                                    f'<strong>{issue.category}</strong>'
                                    f'{" · 5/" + str(issue.day) if issue.day else ""}'
                                    f'{" · " + issue.employee if issue.employee else ""}<br>'
                                    f'<span style="color:#7f1d1d;">{issue.message}</span></div>',
                                    unsafe_allow_html=True,
                                )
                    if chat_result.warning_count > 0:
                        st.markdown("**⚠ 警告（確認推奨）**")
                        for issue in chat_result.issues:
                            if issue.severity == "WARNING":
                                st.markdown(
                                    f'<div style="background:#fefce8; border-left:4px solid #eab308; '
                                    f'padding:6px 10px; margin:4px 0; font-size:13px;">'
                                    f'<strong>{issue.category}</strong>'
                                    f'{" · 5/" + str(issue.day) if issue.day else ""}'
                                    f'{" · " + issue.employee if issue.employee else ""}<br>'
                                    f'<span style="color:#713f12;">{issue.message}</span></div>',
                                    unsafe_allow_html=True,
                                )

            st.markdown("##### 📋 現在のシフト表")

            # 凡例
            st.markdown(
                "**凡例**　"
                "🟡 〇 = 赤羽駅前店　🔵 □ = 赤羽東口店　🟢 △ = 大宮駅前店　"
                "🟣 ☆ = 大宮西口店　🔷 ◆ = 大宮すずらん通り店　⚪ × = 休み"
            )
            # 高さ制限のあるコンテナにシフト表を入れる
            with st.container(height=400, border=True):
                render_shift_table(shift, short_staff_days=short_days_chat)

            st.markdown("---")

            # ============================================================
            # 下部: AI 対話
            # ============================================================
            st.markdown("##### 💬 AI 対話")
            if not api_key:
                st.warning(
                    "⚠ Claude API キーが設定されていません。"
                    "「⚙️ 設定」画面で登録するか、環境変数 ANTHROPIC_API_KEY を設定してください。"
                )
            else:
                st.caption(
                    "💡 例: 「5/15 の大宮駅前店に田中さんを入れたい」"
                    "「鈴木さんと黒澤さんの 20 日のシフトを入れ替えるとどうなる？」"
                    "「板倉さんの月内出勤数を教えて」"
                )

                # チャットエンジン初期化（セッションに保持）
                if "chat_engine" not in st.session_state or st.session_state.get("chat_shift_id") != id(shift):
                    st.session_state.chat_engine = ShiftChatEngine(shift, api_key=api_key)
                    st.session_state.chat_shift_id = id(shift)
                    st.session_state.chat_messages = []

                # 会話履歴を高さ制限のコンテナで表示
                chat_container = st.container(height=350, border=True)
                with chat_container:
                    if not st.session_state.chat_messages:
                        st.caption("👇 下の入力欄から質問・指示してください")
                    for msg in st.session_state.chat_messages:
                        with st.chat_message(msg["role"]):
                            st.write(msg["content"])

                # 入力欄
                if prompt := st.chat_input("AIに質問・指示を入力..."):
                    st.session_state.chat_messages.append({"role": "user", "content": prompt})
                    with st.spinner("AIが考え中..."):
                        response = st.session_state.chat_engine.chat(prompt)
                    st.session_state.chat_messages.append({"role": "assistant", "content": response})
                    st.rerun()

                # 操作ボタン
                col_x, col_y = st.columns([1, 3])
                with col_x:
                    if st.button("🗑 会話履歴をクリア", key="chat_clear"):
                        st.session_state.chat_messages = []
                        st.rerun()
                with col_y:
                    pending = len(st.session_state.chat_engine.pending_changes)
                    if pending:
                        st.info(
                            f"⏳ 仮変更が {pending} 件あります。"
                            "「変更を確定して」と AI に伝えると本シフトに反映されます。"
                        )
                # チャット中にシフトが書き換えられた場合、セッションのシフトも同期
                if st.session_state.chat_engine.shift is not shift:
                    save_session_shift(st.session_state.chat_engine.shift)


# ============================================================
# 従業員ビュー（スマホ向け）
# ============================================================

elif mode == "👤 従業員ビュー":
    st.title("👤 希望シフト提出")

    # マジックリンクでログインしている場合は、その従業員に固定
    from auth import get_logged_in_employee, is_employee, is_manager
    logged_in_emp = get_logged_in_employee()

    if is_employee() and logged_in_emp:
        # 従業員モード（マジックリンク経由）: 自分に固定
        selected = logged_in_emp
        st.markdown(
            f'<div style="background:#dcfce7; padding:12px 16px; border-radius:8px; '
            f'border-left:4px solid #16a34a; margin-bottom:12px;">'
            f'👋 こんにちは、<strong>{selected}さん</strong>。<br>'
            f'<span style="font-size:13px; color:#166534;">'
            f'このページからシフト希望を提出してください。'
            f'</span></div>',
            unsafe_allow_html=True,
        )
    elif is_manager():
        # 経営者がプレビューする場合: 従業員を選択可能
        st.info(
            "💡 経営者として閲覧中です。実運用では従業員はマジックリンク経由で"
            "自動的に自分の画面が開きます。動作確認のため任意の従業員を選択できます。"
        )
        employee_names = [e.name for e in shift_active_employees() if not e.is_auxiliary]
        selected = st.selectbox(
            "【プレビュー】従業員を選択",
            options=employee_names,
        )
    else:
        # 想定外の状態（認証なしでここに来た場合）
        st.error(
            "⚠ ログイン情報が確認できません。"
            "経営者から送られたマジックリンクからアクセスし直してください。"
        )
        st.stop()

    target_year = 2026
    target_month = 6  # 翌月の希望
    days_in_month = monthrange(target_year, target_month)[1]

    st.markdown(f"### {target_year}年{target_month}月の希望")
    st.write("各日の希望を **3つのボタンから1つ** 選んでください：")
    st.markdown(
        """
        <div style="display:flex; gap:14px; margin:8px 0 16px 0; font-size:15px; flex-wrap:wrap;">
          <span style="background:#22c55e; color:white; padding:6px 14px; border-radius:6px; font-weight:bold;">○ 出勤可能</span>
          <span style="background:#ef4444; color:white; padding:6px 14px; border-radius:6px; font-weight:bold;">× 休み希望（絶対）</span>
          <span style="background:#eab308; color:white; padding:6px 14px; border-radius:6px; font-weight:bold;">△ できれば休み</span>
        </div>
        <div style="font-size:13px; color:#6b7280; margin-bottom:12px;">
          選択中のボタンは「鮮やかな色」、未選択のボタンは「薄い色」で表示されます。
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ============================================================
    # ボタン用のCSS — Streamlit が key 属性を CSS クラス st-key-{key} に変換するのを利用
    # ○ = 緑、× = 赤、△ = 黄色 で記号ごとに配色を分ける
    # ============================================================
    st.markdown(
        """
        <style>
        /* === 全ての日別ボタン共通の見た目 === */
        [class*="st-key-pref_"] button {
            width: 100% !important;
            font-size: 22px !important;
            font-weight: bold !important;
            padding: 6px 0 !important;
            margin: 2px 0 !important;
            border-radius: 8px !important;
            min-height: 44px !important;
            transition: all 0.15s ease;
        }

        /* === ○（出勤可能）= 緑系 === */
        /* 選択中 (primary) — 鮮やかな緑 */
        [class*="st-key-pref_ok_"] button[kind="primary"] {
            background-color: #16a34a !important;
            color: white !important;
            border: 3px solid #15803d !important;
            box-shadow: 0 2px 6px rgba(22,163,74,0.4) !important;
        }
        /* 未選択 (secondary) — 薄い緑 */
        [class*="st-key-pref_ok_"] button[kind="secondary"] {
            background-color: #f0fdf4 !important;
            color: #166534 !important;
            border: 2px solid #bbf7d0 !important;
        }
        [class*="st-key-pref_ok_"] button[kind="secondary"]:hover {
            background-color: #dcfce7 !important;
            border-color: #86efac !important;
        }

        /* === ×（休み希望）= 赤系 === */
        [class*="st-key-pref_off_"] button[kind="primary"] {
            background-color: #dc2626 !important;
            color: white !important;
            border: 3px solid #b91c1c !important;
            box-shadow: 0 2px 6px rgba(220,38,38,0.4) !important;
        }
        [class*="st-key-pref_off_"] button[kind="secondary"] {
            background-color: #fef2f2 !important;
            color: #991b1b !important;
            border: 2px solid #fecaca !important;
        }
        [class*="st-key-pref_off_"] button[kind="secondary"]:hover {
            background-color: #fee2e2 !important;
            border-color: #fca5a5 !important;
        }

        /* === △（できれば休み）= 黄色系 === */
        [class*="st-key-pref_maybe_"] button[kind="primary"] {
            background-color: #eab308 !important;
            color: white !important;
            border: 3px solid #ca8a04 !important;
            box-shadow: 0 2px 6px rgba(234,179,8,0.4) !important;
        }
        [class*="st-key-pref_maybe_"] button[kind="secondary"] {
            background-color: #fefce8 !important;
            color: #854d0e !important;
            border: 2px solid #fef08a !important;
        }
        [class*="st-key-pref_maybe_"] button[kind="secondary"]:hover {
            background-color: #fef9c3 !important;
            border-color: #fde047 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # カレンダー形式で入力
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    days_per_row = 7

    if "user_prefs" not in st.session_state:
        st.session_state.user_prefs = {}
    user_key = f"{selected}_{target_year}_{target_month}"
    if user_key not in st.session_state.user_prefs:
        st.session_state.user_prefs[user_key] = {d: "○" for d in range(1, days_in_month + 1)}

    prefs = st.session_state.user_prefs[user_key]

    # 従業員のインデックス（Japaneseキーを避けてASCII-safeなキーにする）
    emp_idx = employee_names.index(selected)

    for week_start in range(1, days_in_month + 1, days_per_row):
        cols = st.columns(days_per_row)
        for i in range(days_per_row):
            d = week_start + i
            if d > days_in_month:
                break
            wd = weekday_jp[date(target_year, target_month, d).weekday()]
            wd_color = "#dc2626" if wd == "日" else ("#2563eb" if wd == "土" else "#374151")
            with cols[i]:
                st.markdown(
                    f'<div style="text-align:center; font-weight:bold; color:{wd_color}; '
                    f'font-size:14px; margin-bottom:4px;">{d}日({wd})</div>',
                    unsafe_allow_html=True,
                )
                current = prefs.get(d, "○")
                # ○ 出勤可能（緑）
                if st.button(
                    "○",
                    key=f"pref_ok_{emp_idx}_{d}",
                    use_container_width=True,
                    type="primary" if current == "○" else "secondary",
                ):
                    prefs[d] = "○"
                    st.rerun()
                # × 休み希望（赤）
                if st.button(
                    "×",
                    key=f"pref_off_{emp_idx}_{d}",
                    use_container_width=True,
                    type="primary" if current == "×" else "secondary",
                ):
                    prefs[d] = "×"
                    st.rerun()
                # △ できれば休み（黄色）
                if st.button(
                    "△",
                    key=f"pref_maybe_{emp_idx}_{d}",
                    use_container_width=True,
                    type="primary" if current == "△" else "secondary",
                ):
                    prefs[d] = "△"
                    st.rerun()

    st.markdown("---")
    st.subheader("自由記述（任意）")
    st.caption(
        "💡 出勤店舗は会社側で決定するため、店舗の希望は不要です。"
        "連勤の上限・特定日の事情など、考慮してほしい点を書いてください。"
    )
    free_text = st.text_area(
        "自然言語で希望を書いてください",
        placeholder=(
            "例: 5連勤は避けたいです。月末は実家に帰るため休みたいです。\n"
            "    16日と17日のどちらか1日は休めると助かります。"
        ),
        height=120,
    )

    if st.button("📤 提出する", type="primary", use_container_width=True):
        # 入力内容を保存
        backup = ShiftBackup()
        off_requests = {selected: [d for d, m in prefs.items() if m == "×"]}
        flexible_days = [d for d, m in prefs.items() if m == "△"]
        backup.save_preferences(
            year=target_year, month=target_month,
            off_requests=off_requests,
            work_requests=[],
            flexible_off=[(selected, flexible_days, len(flexible_days) // 2)] if flexible_days else [],
            natural_language_notes={selected: free_text},
            author=selected,
        )
        st.success("✅ 希望を受け付けました！")
        st.balloons()


# ============================================================
# 過去シフト閲覧
# ============================================================

elif mode == "📁 過去シフト閲覧":
    st.title("📁 過去のシフト")

    excel_path = str(MAY_2026_SHIFT_XLSX)
    if not Path(excel_path).exists():
        st.warning("Excel ファイルがありません")
    else:
        st.info(f"📂 ソース: {excel_path}")

        # Excelからシフトを読み込んで表示
        try:
            shift, short_days = load_shift_from_excel(excel_path)
            st.markdown(f"### {shift.year}年{shift.month}月（手動作成版）")
            st.write(f"人員少マーク日: 5/{', 5/'.join(map(str, short_days))}")
            render_shift_table(shift)
        except Exception as e:
            st.error(f"読み込みエラー: {e}")


# ============================================================
# 設定
# ============================================================

elif mode == "⚙️ 設定":
    st.title("⚙️ システム設定")

    # タブで分割（情報量が多いので）
    (
        setting_tab_links,
        setting_tab1, setting_tab2,
        setting_tab3, setting_tab4, setting_tab5,
    ) = st.tabs([
        "🔗 マジックリンク",
        "🔧 ルール設定", "📜 ルール変更履歴",
        "👥 従業員マスタ", "🔑 APIキー", "💾 バックアップ",
    ])

    # ============================================================
    # タブ0: マジックリンク（従業員配布用URL）
    # ============================================================
    with setting_tab_links:
        from prototype.employee_tokens import (
            generate_token, get_magic_link, get_line_message, is_salt_configured,
        )
        from prototype.employees import shift_active_employees as _link_active_emps

        st.markdown("### 🔗 従業員マジックリンク管理")
        st.caption(
            "各従業員に LINE で送る個別URLを管理する画面です。"
            "URLにはトークンが含まれており、タップだけでログインできます。"
        )

        if not is_salt_configured():
            st.error(
                "⚠ **マジックリンク用の秘密の塩 `MAGIC_LINK_SALT` が設定されていません。**\n\n"
                "Streamlit Cloud の Settings → Secrets で設定するか、"
                "ローカル開発時は環境変数 `MAGIC_LINK_SALT` を設定してください。\n\n"
                "**例（Streamlit Secrets）**:\n"
                "```\nMAGIC_LINK_SALT = \"daikokuya-secret-salt-2026\"\n```\n\n"
                "塩を変更すると全URLが一括無効化されます（一斉再発行に使えます）。"
            )
        else:
            # アプリの URL を入力（Streamlit Cloud のURL）
            base_url = st.text_input(
                "アプリの公開URL",
                value=st.session_state.get(
                    "magic_link_base_url",
                    "https://daikokuya-shift.streamlit.app",
                ),
                help="https://〜〜.streamlit.app の形式で入力してください",
            )
            st.session_state["magic_link_base_url"] = base_url

            st.markdown("---")
            st.markdown("#### 📋 全従業員のマジックリンク一覧")

            # 在籍中の従業員のみ
            active_emps = _link_active_emps()
            display_emps = [e for e in active_emps if not e.is_auxiliary]

            # 一覧テーブル
            link_data = []
            for emp in display_emps:
                link = get_magic_link(emp.name, base_url)
                link_data.append({
                    "氏名": emp.name,
                    "フルネーム": emp.full_name or "-",
                    "雇用形態": emp.employment_status.value,
                    "マジックリンク": link,
                })
            st.dataframe(
                link_data,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "マジックリンク": st.column_config.LinkColumn(
                        "マジックリンク（クリックでテスト可）"
                    ),
                },
            )

            st.markdown("---")
            st.markdown("#### 📨 LINE送信用テンプレート")
            st.caption(
                "従業員ごとの LINE 送信用メッセージです。"
                "各カードの「コピー」アイコンを押すと、そのままLINEに貼り付けできます。"
            )

            # 個別の LINE メッセージを表示（コピー可能）
            for emp in display_emps:
                msg = get_line_message(emp.name, base_url)
                with st.expander(f"📤 {emp.name}さん（{emp.full_name or '-'}）への送信メッセージ", expanded=False):
                    st.code(msg, language=None)

            # 一括コピー用：全員分まとめて表示
            st.markdown("---")
            with st.expander("📋 全員分のリンクをまとめて見る（一括コピー用）", expanded=False):
                all_links_text = "\n\n---\n\n".join(
                    f"【{emp.name}さん】\nURL: {get_magic_link(emp.name, base_url)}"
                    for emp in display_emps
                )
                st.code(all_links_text, language=None)

            # セキュリティに関する注意
            st.markdown("---")
            with st.expander("🔐 セキュリティに関する注意", expanded=False):
                st.markdown(
                    """
                    **マジックリンクの仕組み:**
                    - 各URLには「従業員名」を秘密の塩でハッシュ化したトークンが含まれます
                    - URLを見ても他人の名前は推測できません
                    - 同じ従業員には常に同じURLが発行されます

                    **注意事項:**
                    - 各従業員には**自分専用のURL**を渡してください
                    - URLを他人と共有しないよう周知してください（特に転送・SNS投稿に注意）
                    - URLが流出した疑いがある場合は、`MAGIC_LINK_SALT` を変更すれば**全URLが無効化**されます
                      （その後、全員に新しいURLを再配布する必要があります）

                    **退職者のURL:**
                    - 退職処理した従業員のURLは「在籍中の従業員」リストから自動的に除外されます
                    - ただし `MAGIC_LINK_SALT` を変更しない限り、トークン自体は有効なままです
                    - 完全に無効化したい場合は `MAGIC_LINK_SALT` を変更して全員に再配布してください
                    """
                )

    rule_mgr = RuleConfigManager()
    cfg = rule_mgr.load()

    # ============================================================
    # タブ1: ルール設定
    # ============================================================
    with setting_tab1:
        st.markdown("### ルール設定")
        st.caption(
            "ここで設定した値は、シフト生成・検証に即座に反映されます。"
            "変更履歴は「📜 ルール変更履歴」タブで確認できます。"
        )

        # サブセクション: 検証チェックのON/OFF
        st.markdown("#### ✅ 検証チェックの ON/OFF")
        st.caption("「シフトとして問題視するルール」をチェック単位で有効化／無効化できます。")

        check_labels = {
            "store_capacity": "店舗別必要人数",
            "eco_required": "東口・西口の必須エコ要員",
            "consec_work": "最大連勤チェック",
            "holiday_days": "月内最低休日数",
            "consec_off_3": "3連休禁止",
            "two_off_per_month": "月内 2連休回数（最低1回・最大2回）",
            "off_request": "休み希望厳守",
            "work_request": "出勤希望厳守",
            "omiya_anchor": "大宮アンカー（春山 or 下地必須）",
            "higashi_monday": "東口の月曜休店",
            "omiya_short_warning": "大宮人数少（エコ1名運営）の警告表示",
        }

        new_enabled = {}
        ck_col1, ck_col2 = st.columns(2)
        keys = list(check_labels.keys())
        for i, key in enumerate(keys):
            with (ck_col1 if i < len(keys) // 2 + 1 else ck_col2):
                new_enabled[key] = st.toggle(
                    check_labels[key],
                    value=cfg.enabled_checks.get(key, True),
                    key=f"chk_{key}",
                )

        # サブセクション: 数値パラメータ
        st.markdown("---")
        st.markdown("#### 🔢 数値パラメータ")
        st.warning(
            "⚠ **注意**: ここの値を極端な値に変えると、**シフトを生成できなくなる**ことがあります。"
            "推奨範囲外の入力には自動で警告が表示されます。困った時は「⚠ デフォルトに戻す」ボタンを使ってください。"
        )

        param_col1, param_col2 = st.columns(2)
        new_params = {}

        # 各パラメータの推奨範囲
        SAFE_RANGES = {
            "max_consec_work": (4, 7),      # 4〜7が現実的
            "soft_consec_threshold": (3, 5),
            "default_holiday_days": (6, 10),
            "min_2off_per_month": (0, 2),
            "max_2off_per_month": (1, 4),
            "higashi_eco2_max_per_month": (0, 5),
        }

        def warn_if_unsafe(key: str, value: int) -> None:
            """値が推奨範囲外なら警告を表示"""
            if key in SAFE_RANGES:
                lo, hi = SAFE_RANGES[key]
                if value < lo:
                    st.caption(
                        f"⚠ 推奨範囲（{lo}〜{hi}）より小さい値です。"
                        "シフトが生成できなくなる可能性があります。"
                    )
                elif value > hi:
                    st.caption(
                        f"⚠ 推奨範囲（{lo}〜{hi}）より大きい値です。"
                        "現実的でないシフトが生成される可能性があります。"
                    )

        with param_col1:
            new_params["max_consec_work"] = st.number_input(
                "最大連勤日数（ハード上限）",
                min_value=1, max_value=10,
                value=cfg.parameters.get("max_consec_work", 5),
                help="この日数を超える連勤はエラーになります。推奨: 4〜7日",
            )
            warn_if_unsafe("max_consec_work", new_params["max_consec_work"])

            new_params["soft_consec_threshold"] = st.number_input(
                "推奨連勤上限（ソフト・ペナルティ閾値）",
                min_value=1, max_value=10,
                value=cfg.parameters.get("soft_consec_threshold", 4),
                help="この日数を超えるとシフト生成時にペナルティ加算（できる限り回避）。推奨: 3〜5日",
            )
            warn_if_unsafe("soft_consec_threshold", new_params["soft_consec_threshold"])

            new_params["default_holiday_days"] = st.number_input(
                "既定の月内最低休日数",
                min_value=0, max_value=15,
                value=cfg.parameters.get("default_holiday_days", 8),
                help="個別オーバーライドがない従業員の月内必要休日数。推奨: 6〜10日",
            )
            warn_if_unsafe("default_holiday_days", new_params["default_holiday_days"])
        with param_col2:
            new_params["min_2off_per_month"] = st.number_input(
                "2連休 月内最低回数",
                min_value=0, max_value=10,
                value=cfg.parameters.get("min_2off_per_month", 1),
                help="推奨: 0〜2回",
            )
            warn_if_unsafe("min_2off_per_month", new_params["min_2off_per_month"])

            new_params["max_2off_per_month"] = st.number_input(
                "2連休 月内最大回数",
                min_value=0, max_value=10,
                value=cfg.parameters.get("max_2off_per_month", 2),
                help="推奨: 1〜4回",
            )
            warn_if_unsafe("max_2off_per_month", new_params["max_2off_per_month"])

            new_params["higashi_eco2_max_per_month"] = st.number_input(
                "東口エコ2配置 月内最大回数",
                min_value=0, max_value=10,
                value=cfg.parameters.get("higashi_eco2_max_per_month", 3),
                help="推奨: 0〜5回",
            )
            warn_if_unsafe("higashi_eco2_max_per_month", new_params["higashi_eco2_max_per_month"])

        # 矛盾チェック: 2連休 最低 > 最大 はおかしい
        if new_params["min_2off_per_month"] > new_params["max_2off_per_month"]:
            st.error(
                f"❌ 設定矛盾: 2連休 最低{new_params['min_2off_per_month']}回 > "
                f"最大{new_params['max_2off_per_month']}回 になっています。"
                "最低 ≤ 最大 になるよう調整してください。"
            )
        # ソフト閾値 > ハード上限 はおかしい
        if new_params["soft_consec_threshold"] > new_params["max_consec_work"]:
            st.error(
                f"❌ 設定矛盾: 推奨連勤上限（{new_params['soft_consec_threshold']}）が"
                f"ハード上限（{new_params['max_consec_work']}）を超えています。"
                "推奨 ≤ ハード になるよう調整してください。"
            )

        st.markdown("##### ソルバー設定")
        param_col3, param_col4 = st.columns(2)
        with param_col3:
            new_params["solver_seed"] = st.number_input(
                "ソルバーシード",
                min_value=0, max_value=999999,
                value=cfg.parameters.get("solver_seed", 42),
                help="同じ入力に対して毎回同じシフトを生成するためのシード値。"
                     "別のパターンを試したい時は数値を変えてください。",
            )
        with param_col4:
            new_params["solver_time_limit_seconds"] = st.number_input(
                "ソルバー最大実行時間（秒）",
                min_value=10, max_value=600,
                value=cfg.parameters.get("solver_time_limit_seconds", 120),
            )

        # サブセクション: カスタムルール
        st.markdown("---")
        st.markdown("#### ➕ カスタムルール")
        st.info(
            "💡 **このセクションは安全です**：カスタムルールは現在「メモ」として記録されるだけで、"
            "シフト生成・検証ロジックには影響しません。**追加・変更・削除でバグや停止は起こりません。**"
            "将来的に検証ロジックに連動させたい場合は、技術者にご相談ください。"
        )

        new_custom_rules = list(cfg.custom_rules)

        # 既存ルールの一覧と削除
        if new_custom_rules:
            st.markdown("**既存のカスタムルール:**")
            for idx, rule in enumerate(new_custom_rules):
                rule_col1, rule_col2, rule_col3 = st.columns([5, 1, 1])
                with rule_col1:
                    st.markdown(
                        f'<div style="padding:8px; border-left:3px solid #6366f1; '
                        f'background:#f5f3ff; border-radius:4px;">'
                        f'<strong>{rule.name}</strong> '
                        f'<span style="font-size:11px; color:#6b7280;">'
                        f'({rule.severity})</span><br>'
                        f'<span style="font-size:13px;">{rule.description}</span><br>'
                        f'<span style="font-size:11px; color:#9ca3af;">'
                        f'追加: {rule.created_at[:10]} by {rule.created_by}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                with rule_col2:
                    new_custom_rules[idx].enabled = st.toggle(
                        "有効", value=rule.enabled, key=f"rule_en_{rule.id}",
                    )
                with rule_col3:
                    if st.button("🗑", key=f"del_{rule.id}"):
                        new_custom_rules = [r for r in new_custom_rules if r.id != rule.id]
                        st.rerun()

        # 新規ルールの追加フォーム
        with st.expander("➕ 新しいカスタムルールを追加", expanded=False):
            with st.form("add_rule_form", clear_on_submit=True):
                rule_name = st.text_input("ルール名", placeholder="例: 楯さんは日曜休み優先")
                rule_desc = st.text_area(
                    "詳細",
                    placeholder="例: 楯さんは家族の事情で日曜休み優先で組む",
                    height=80,
                )
                rule_severity = st.selectbox(
                    "重要度",
                    options=["WARNING", "ERROR"],
                    help="ERROR: 必ず守る／WARNING: できれば守る",
                )
                rule_actor = st.text_input("追加者", value="代表取締役")
                if st.form_submit_button("追加", type="primary"):
                    if rule_name and rule_desc:
                        new_rule = CustomRule(
                            id=f"custom_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                            name=rule_name, description=rule_desc,
                            severity=rule_severity, enabled=True,
                            created_at=datetime.now().isoformat(),
                            created_by=rule_actor,
                        )
                        new_custom_rules.append(new_rule)
                        # 即時保存
                        new_cfg = RuleConfig(
                            enabled_checks=new_enabled,
                            parameters=new_params,
                            custom_rules=new_custom_rules,
                        )
                        rule_mgr.save(new_cfg, actor=rule_actor, note=f"カスタムルール追加: {rule_name}")
                        st.success(f"✅ ルール「{rule_name}」を追加しました")
                        st.rerun()

        # 保存・リセットボタン
        st.markdown("---")
        save_col1, save_col2, save_col3 = st.columns([2, 1, 1])
        with save_col1:
            save_actor = st.text_input("保存実行者名", value="代表取締役", key="cfg_actor")
            save_note = st.text_input("変更メモ（任意）", placeholder="例: 連勤上限を6に緩和")
        with save_col2:
            if st.button("💾 設定を保存", type="primary", use_container_width=True):
                new_cfg = RuleConfig(
                    enabled_checks=new_enabled,
                    parameters=new_params,
                    custom_rules=new_custom_rules,
                )
                changes = rule_mgr.save(new_cfg, actor=save_actor, note=save_note)
                if changes:
                    st.success(f"✅ {len(changes)}件の変更を保存しました")
                else:
                    st.info("変更点はありません")
                st.rerun()
        with save_col3:
            if st.button("⚠ デフォルトに戻す", use_container_width=True):
                if st.session_state.get("confirm_reset"):
                    rule_mgr.reset_to_default(actor=save_actor)
                    st.success("✅ デフォルトに戻しました")
                    st.session_state["confirm_reset"] = False
                    st.rerun()
                else:
                    st.session_state["confirm_reset"] = True
                    st.warning("⚠ もう一度押すとリセットされます")

    # ============================================================
    # タブ2: ルール変更履歴
    # ============================================================
    with setting_tab2:
        st.markdown("### 📜 ルール変更履歴")
        st.caption("過去のルール変更履歴（新しい順）")

        history = rule_mgr.get_history(limit=200)
        if not history:
            st.info("まだ変更履歴はありません")
        else:
            history_data = []
            for ch in history:
                if ch.category == "enabled_check":
                    desc = f"{ch.target}: {'有効化' if ch.after else '無効化'}"
                elif ch.category == "parameter":
                    desc = f"{ch.target}: {ch.before} → {ch.after}"
                elif ch.category == "custom_rule_add":
                    name = ch.after.get("name", "?") if isinstance(ch.after, dict) else "?"
                    desc = f"カスタムルール追加: {name}"
                elif ch.category == "custom_rule_remove":
                    name = ch.before.get("name", "?") if isinstance(ch.before, dict) else "?"
                    desc = f"カスタムルール削除: {name}"
                elif ch.category == "custom_rule_toggle":
                    desc = f"カスタムルール {'有効化' if ch.after else '無効化'}: {ch.target}"
                else:
                    desc = f"{ch.category}: {ch.target}"
                history_data.append({
                    "日時": ch.timestamp[:19].replace("T", " "),
                    "実行者": ch.actor,
                    "変更内容": desc,
                    "メモ": ch.note,
                })
            st.dataframe(history_data, use_container_width=True, hide_index=True)

    # ============================================================
    # タブ3: 従業員マスタ（CRUD）
    # ============================================================
    with setting_tab3:
        st.markdown("### 👥 従業員マスタ")
        st.caption(
            "雇用形態の変更・新入社員の追加・退職処理ができます。"
            "退職者はデータ上は残り、シフト生成からは自動で除外されます。"
        )

        emp_mgr = EmployeeConfigManager()
        all_emps = emp_mgr.load_all()

        # サブタブで操作を分類
        emp_subtab1, emp_subtab2, emp_subtab3 = st.tabs([
            "📋 一覧・編集", "➕ 新入社員を追加", "📜 変更履歴",
        ])

        # ========================================
        # 一覧・編集
        # ========================================
        with emp_subtab1:
            # フィルタ
            filt_col1, filt_col2 = st.columns([2, 3])
            with filt_col1:
                show_filter = st.selectbox(
                    "表示する雇用形態",
                    options=["すべて", "在籍中のみ", "退職者のみ", "顧問・補助のみ"],
                    key="emp_filter",
                )

            # 表示する従業員リスト
            if show_filter == "在籍中のみ":
                display_emps = [
                    e for e in all_emps
                    if e.employment_status in (EmploymentStatus.ACTIVE, EmploymentStatus.PART_TIME)
                ]
            elif show_filter == "退職者のみ":
                display_emps = [
                    e for e in all_emps
                    if e.employment_status in (EmploymentStatus.RETIRED, EmploymentStatus.ON_LEAVE)
                ]
            elif show_filter == "顧問・補助のみ":
                display_emps = [
                    e for e in all_emps
                    if e.employment_status in (EmploymentStatus.ADVISOR, EmploymentStatus.AUXILIARY)
                ]
            else:
                display_emps = all_emps

            # サマリー
            with filt_col2:
                status_counts = {}
                for e in all_emps:
                    s = e.employment_status.value
                    status_counts[s] = status_counts.get(s, 0) + 1
                summary_str = "　".join(f"{k}: {v}名" for k, v in status_counts.items())
                st.caption(f"**全社人員**: {summary_str}")

            # 状態別の色マップ
            status_colors = {
                EmploymentStatus.ACTIVE: "#dcfce7",       # 緑
                EmploymentStatus.PART_TIME: "#fef9c3",    # 黄
                EmploymentStatus.ADVISOR: "#dbeafe",      # 青
                EmploymentStatus.AUXILIARY: "#f3e8ff",    # 紫
                EmploymentStatus.ON_LEAVE: "#fef3c7",     # オレンジ
                EmploymentStatus.RETIRED: "#f3f4f6",      # グレー
            }

            # 一覧表示（カラムビュー）
            emp_data = []
            for e in display_emps:
                emp_data.append({
                    "氏名": e.name,
                    "フルネーム": e.full_name or "-",
                    "役職": e.role.value,
                    "スキル": e.skill.value,
                    "雇用形態": e.employment_status.value,
                    "ホーム店舗": e.home_store.display_name if e.home_store else "-",
                    "年間目標日数": e.annual_target_days if e.annual_target_days else "-",
                    "入社日": e.hired_at or "-",
                    "退職日": e.retired_at or "-",
                })
            if emp_data:
                st.dataframe(emp_data, use_container_width=True, hide_index=True)
            else:
                st.info("該当する従業員がいません")

            # 編集セクション
            st.markdown("---")
            st.markdown("##### ✏ 従業員の編集・状態変更")

            edit_col1, edit_col2 = st.columns([2, 3])
            with edit_col1:
                target_name = st.selectbox(
                    "編集する従業員を選択",
                    options=[e.name for e in all_emps],
                    key="edit_target",
                )

            target = next((e for e in all_emps if e.name == target_name), None)
            if target:
                with st.form("edit_emp_form"):
                    st.markdown(f"**{target.name}**（{target.full_name or '-'}）の編集")
                    f_col1, f_col2 = st.columns(2)
                    with f_col1:
                        new_full_name = st.text_input(
                            "フルネーム", value=target.full_name or "",
                        )
                        # 役職ドロップダウン: 店長・一般スタッフのみ。
                        # 顧問・代表取締役の人を編集する場合は現在の役職も含める（変更不可レベルで表示）
                        editable_roles = [Role.MANAGER.value, Role.STAFF.value]
                        if target.role.value not in editable_roles:
                            # 顧問・代表取締役の人はその役職を保持して表示
                            role_options = [target.role.value] + editable_roles
                        else:
                            role_options = editable_roles
                        new_role = st.selectbox(
                            "役職",
                            options=role_options,
                            index=role_options.index(target.role.value),
                            help="顧問・代表取締役は1名固定のため、新規登録では選べません",
                        )
                        new_skill = st.selectbox(
                            "スキル",
                            options=[s.value for s in Skill],
                            index=[s.value for s in Skill].index(target.skill.value),
                        )
                        new_status = st.selectbox(
                            "雇用形態",
                            options=[s.value for s in EmploymentStatus],
                            index=[s.value for s in EmploymentStatus].index(target.employment_status.value),
                            help="退職を選ぶと自動的に退職日が記録されます",
                        )
                    with f_col2:
                        store_options = ["（なし）"] + [s.name for s in Store if s != Store.OFF]
                        current_home = target.home_store.name if target.home_store else "（なし）"
                        new_home_store = st.selectbox(
                            "ホーム店舗",
                            options=store_options,
                            index=store_options.index(current_home) if current_home in store_options else 0,
                        )
                        new_target_days = st.number_input(
                            "年間目標出勤日数（パートは0でOK）",
                            min_value=0, max_value=400,
                            value=target.annual_target_days or 0,
                        )
                        new_notes = st.text_area(
                            "備考", value=target.notes, height=100,
                        )
                    edit_actor = st.text_input(
                        "実行者", value="代表取締役", key="edit_actor",
                    )
                    edit_note = st.text_input(
                        "変更メモ", placeholder="例: パートに転換",
                        key="edit_note",
                    )

                    submit = st.form_submit_button("💾 変更を保存", type="primary")
                    if submit:
                        updates = {
                            "full_name": new_full_name or None,
                            "role": new_role,
                            "skill": new_skill,
                            "employment_status": new_status,
                            "home_store": (
                                None if new_home_store == "（なし）"
                                else new_home_store
                            ),
                            "annual_target_days": new_target_days if new_target_days > 0 else None,
                            "notes": new_notes,
                        }
                        success = emp_mgr.update_employee(
                            name=target_name, updates=updates,
                            actor=edit_actor, note=edit_note,
                        )
                        if success:
                            st.success(f"✅ {target_name}さんの情報を更新しました")
                            st.rerun()
                        else:
                            st.error("❌ 更新に失敗しました")

            # 退職処理ショートカット
            st.markdown("---")
            st.markdown("##### 🔚 退職処理ショートカット")
            ret_col1, ret_col2, ret_col3 = st.columns([2, 2, 1])
            with ret_col1:
                retire_target = st.selectbox(
                    "退職処理する従業員",
                    options=[
                        e.name for e in all_emps
                        if e.employment_status not in (EmploymentStatus.RETIRED,)
                    ],
                    key="retire_target",
                )
            with ret_col2:
                retire_date = st.date_input(
                    "退職日", value=date.today(), key="retire_date",
                )
            with ret_col3:
                st.write("")
                st.write("")
                if st.button("🔚 退職処理", key="retire_btn", type="secondary"):
                    if st.session_state.get("confirm_retire") == retire_target:
                        emp_mgr.retire_employee(
                            name=retire_target,
                            actor="代表取締役",
                            retired_date=retire_date.isoformat(),
                            note=f"退職処理（{retire_date}）",
                        )
                        st.success(f"✅ {retire_target}さんを退職処理しました")
                        st.session_state["confirm_retire"] = None
                        st.rerun()
                    else:
                        st.session_state["confirm_retire"] = retire_target
                        st.warning(f"⚠ もう一度押すと {retire_target} を退職処理します")

        # ========================================
        # 新入社員を追加
        # ========================================
        with emp_subtab2:
            st.markdown("### ➕ 新入社員を追加")
            st.caption(
                "新しく入社した従業員を登録します。"
                "次月のシフト生成から自動的に対象に含まれるようになります。"
            )

            with st.form("add_employee_form", clear_on_submit=True):
                a_col1, a_col2 = st.columns(2)
                with a_col1:
                    new_name = st.text_input(
                        "表示名（必須）",
                        placeholder="例: 鈴木",
                        help="シフト表で使う短い名前。重複不可",
                    )
                    new_full_name = st.text_input(
                        "フルネーム",
                        placeholder="例: 鈴木一郎",
                    )
                    new_emp_id = st.text_input(
                        "従業員番号（任意）",
                        placeholder="例: 082",
                    )
                    # 新入社員は店長か一般スタッフのみ
                    new_emp_role = st.selectbox(
                        "役職",
                        options=[Role.STAFF.value, Role.MANAGER.value],
                        index=0,
                        help="顧問・代表取締役は1名固定のため、新規登録では選べません",
                    )
                    new_emp_skill = st.selectbox(
                        "スキル",
                        options=[s.value for s in Skill],
                        index=[s.value for s in Skill].index(Skill.TICKET.value),
                        help="新入社員はまずチケット担当から開始するのが慣例",
                    )
                with a_col2:
                    new_emp_status = st.selectbox(
                        "雇用形態",
                        options=[
                            EmploymentStatus.ACTIVE.value,
                            EmploymentStatus.PART_TIME.value,
                        ],
                    )
                    home_store_options = ["（なし）"] + [s.name for s in Store if s != Store.OFF]
                    new_emp_home = st.selectbox(
                        "ホーム店舗（固定配置の場合のみ）",
                        options=home_store_options,
                    )
                    new_hired_date = st.date_input(
                        "入社日", value=date.today(),
                    )
                    new_target = st.number_input(
                        "年間目標出勤日数（パートなら0）",
                        min_value=0, max_value=400,
                        value=258,  # 新入社員の標準値
                        help="一般正社員の標準は258日（=265日 - 7日）",
                    )

                new_emp_notes = st.text_area(
                    "備考",
                    placeholder="例: 2026年5月入社。チケット担当として研修中。",
                    height=80,
                )
                add_actor = st.text_input("実行者", value="代表取締役", key="add_actor")

                submit_add = st.form_submit_button("➕ 追加する", type="primary")
                if submit_add:
                    if not new_name:
                        st.error("❌ 表示名は必須です")
                    else:
                        new_emp = Employee(
                            name=new_name.strip(),
                            full_name=new_full_name.strip() or None,
                            employee_id=new_emp_id.strip() or None,
                            role=Role(new_emp_role),
                            skill=Skill(new_emp_skill),
                            home_store=(
                                None if new_emp_home == "（なし）"
                                else Store[new_emp_home]
                            ),
                            station_type=(
                                StationType.FIXED if new_emp_home != "（なし）"
                                else StationType.FLEXIBLE
                            ),
                            employment_status=EmploymentStatus(new_emp_status),
                            annual_target_days=new_target if new_target > 0 else None,
                            hired_at=new_hired_date.isoformat(),
                            notes=new_emp_notes,
                        )
                        success = emp_mgr.add_employee(
                            new_emp, actor=add_actor,
                            note=f"新入社員追加（入社日: {new_hired_date}）",
                        )
                        if success:
                            st.success(f"✅ {new_name}さんを追加しました")
                            st.balloons()
                            st.rerun()
                        else:
                            st.error(
                                f"❌ 同名の従業員「{new_name}」が既に存在します。"
                                "別の表示名にしてください。"
                            )

            st.info(
                "💡 ヒント: 新入社員追加後、「📋 一覧・編集」タブで店舗適性（affinities）を"
                "詳細設定できます（現状の追加フォームではホーム店舗のみ）。"
                "詳細な適性は技術者にご相談ください。"
            )

        # ========================================
        # 変更履歴
        # ========================================
        with emp_subtab3:
            st.markdown("### 📜 従業員マスタ変更履歴")
            history = emp_mgr.get_history(limit=200)
            if not history:
                st.info("まだ変更履歴はありません")
            else:
                hist_data = []
                for h in history:
                    action_label = {
                        "add": "➕ 追加",
                        "update": "✏ 更新",
                        "remove": "🗑 削除",
                    }.get(h["action"], h["action"])
                    # 変更内容の要約
                    desc = ""
                    if h["action"] == "update" and h.get("before") and h.get("after"):
                        diffs = []
                        for key in h["after"]:
                            if h["before"].get(key) != h["after"].get(key):
                                if key in ("affinities",):
                                    diffs.append(f"{key} を更新")
                                else:
                                    diffs.append(
                                        f"{key}: {h['before'].get(key)} → {h['after'].get(key)}"
                                    )
                        desc = " / ".join(diffs[:3])
                        if len(diffs) > 3:
                            desc += f" ほか{len(diffs) - 3}件"
                    elif h["action"] == "add":
                        desc = "新入社員追加"
                    elif h["action"] == "remove":
                        desc = "完全削除"
                    hist_data.append({
                        "日時": h["timestamp"][:19].replace("T", " "),
                        "操作": action_label,
                        "対象": h["target"],
                        "変更内容": desc,
                        "実行者": h["actor"],
                        "メモ": h["note"],
                    })
                st.dataframe(hist_data, use_container_width=True, hide_index=True)

    # ============================================================
    # タブ4: APIキー
    # ============================================================
    with setting_tab4:
        st.markdown("### 🔑 Claude API キー")
        st.caption(
            "自然言語の希望解析・AI対話に使用します。"
            "https://console.anthropic.com/ で取得してください。"
        )
        api_key = st.text_input(
            "ANTHROPIC_API_KEY",
            type="password",
            placeholder="sk-ant-...",
        )
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            st.session_state["api_key"] = api_key
            st.success("✅ APIキーをセッションに登録しました")

    # ============================================================
    # タブ5: バックアップ状況
    # ============================================================
    with setting_tab5:
        st.markdown("### 💾 バックアップ状況")
        backup_dir = BACKUP_DIR
        if backup_dir.exists():
            month_dirs = sorted(backup_dir.iterdir())
            if month_dirs:
                for month_dir in month_dirs:
                    if month_dir.is_dir():
                        files = list(month_dir.iterdir())
                        st.write(f"📁 **{month_dir.name}**: {len(files)} ファイル")
            else:
                st.info("まだバックアップはありません")
        else:
            st.info("まだバックアップはありません")

        st.markdown("---")
        st.markdown("### 🔒 ロック状況")
        lock_mgr = ShiftLockManager()
        all_locks = lock_mgr.list_locks()
        if all_locks:
            for lk in all_locks:
                st.markdown(
                    f"🔒 **{lk.year}年{lk.month}月**　"
                    f"_{lk.locked_at[:19]}_　"
                    f"by {lk.locked_by}　- {lk.note}"
                )
        else:
            st.info("ロックされているシフトはありません")
