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
import json
import inspect
from pathlib import Path
from typing import Optional
from html import escape

# パス設定: プロジェクトルートと app ディレクトリの両方を Python パスに追加
# これにより `from prototype.X` と `from auth` 両方の形式が動く
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))   # for: from prototype.X import Y
sys.path.insert(0, str(_THIS_DIR))       # for: from auth import Z

import streamlit as st
import pandas as pd
from datetime import date, datetime
from calendar import monthrange
from urllib.parse import quote, unquote

try:
    from st_aggrid import AgGrid, JsCode
    try:
        from st_aggrid import GridUpdateMode
    except Exception:
        GridUpdateMode = None
    HAS_AGGRID = True
except Exception:
    HAS_AGGRID = False

from prototype.paths import (
    PROJECT_ROOT, DATA_DIR, BACKUP_DIR, OUTPUT_DIR, CONFIG_DIR, MAY_2026_SHIFT_XLSX,
)

# 認証モジュール（同じ app/ ディレクトリに配置）
from auth import require_auth, render_logout_button, is_manager, get_user_role


def get_anthropic_api_key_source() -> str:
    """Claude API キーの取得元を返す。"""
    try:
        if "ANTHROPIC_API_KEY" in st.secrets and str(st.secrets["ANTHROPIC_API_KEY"]).strip():
            return "secrets"
    except Exception:
        pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment"
    if st.session_state.get("api_key"):
        return "session"
    return ""


def get_anthropic_api_key() -> Optional[str]:
    """
    Anthropic API キーを以下の優先順位で取得：
    1. Streamlit Secrets（Streamlit Cloud デプロイ時）
    2. 環境変数 ANTHROPIC_API_KEY（ローカル実行時）
    3. セッションに登録された値（設定画面から入力）
    """
    source = get_anthropic_api_key_source()
    if source == "secrets":
        return str(st.secrets["ANTHROPIC_API_KEY"]).strip()
    if source == "environment":
        return os.environ.get("ANTHROPIC_API_KEY")
    if source == "session":
        return st.session_state.get("api_key")
    return None


ADMIN_PAID_LEAVE_FILE = CONFIG_DIR / "admin_paid_leave_adjustments.json"
RULE_LEDGER_FILE = CONFIG_DIR / "rule_ledger_v1_0.json"


def load_rule_ledger_v1() -> dict:
    """ルール台帳 v1.0 を読み込む。壊れている場合は空の台帳を返す。"""
    fallback = {
        "version": "1.0",
        "title": "大黒屋シフト作成ルール台帳",
        "rules": [],
        "employee_store_suitability": [],
        "numeric_parameters": [],
    }
    if not RULE_LEDGER_FILE.exists():
        return fallback
    try:
        with open(RULE_LEDGER_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    return {**fallback, **data}


def build_employee_suitability_rows_from_master() -> list[dict]:
    """台帳の店舗適性欄が空の場合、現在の従業員マスターから表示用に作る。"""
    try:
        from prototype.employee_config import get_all_employees_including_retired
        from prototype.models import Affinity, Role, Store
    except Exception:
        return []

    store_order = [
        Store.AKABANE,
        Store.HIGASHIGUCHI,
        Store.OMIYA,
        Store.NISHIGUCHI,
        Store.SUZURAN,
    ]
    affinity_bucket = {
        Affinity.STRONG: "強",
        Affinity.MEDIUM: "中",
        Affinity.WEAK: "弱",
        Affinity.NONE: "不可",
    }
    rows = []
    try:
        employees = get_all_employees_including_retired()
    except Exception:
        return []

    for emp in employees:
        if getattr(emp, "role", None) in (Role.REPRESENTATIVE, Role.ADVISOR):
            continue
        if getattr(emp, "is_auxiliary", False):
            continue
        grouped = {"強": [], "中": [], "弱": [], "不可": []}
        affinities = getattr(emp, "affinities", {}) or {}
        for store in store_order:
            bucket = affinity_bucket.get(affinities.get(store, Affinity.NONE), "不可")
            grouped[bucket].append(store.display_name)
        rows.append({
            "氏名": getattr(emp, "name", ""),
            "主担当・強": "、".join(grouped["強"]) or "-",
            "通常対応・中": "、".join(grouped["中"]) or "-",
            "応援・巡回・弱": "、".join(grouped["弱"]) or "-",
            "原則不可": "、".join(grouped["不可"]) or "-",
            "備考": getattr(emp, "notes", "") or "",
        })
    return rows


def build_numeric_ledger_rows_from_parameters(parameters: dict) -> list[dict]:
    """台帳の数値基準欄が空の場合、現在の本設定から表示用に作る。"""
    return [
        {
            "分類": "絶対条件",
            "項目": "最大連勤日数",
            "現在値": str(parameters.get("max_consec_work", 5)),
            "備考": "この日数を超える連勤はエラー扱い。",
        },
        {
            "分類": "強い目標",
            "項目": "推奨連勤上限",
            "現在値": str(parameters.get("soft_consec_threshold", 4)),
            "備考": "この日数を超えると、生成時にできるだけ避ける。",
        },
        {
            "分類": "強い目標",
            "項目": "既定の月内最低休日数",
            "現在値": str(parameters.get("default_holiday_days", 8)),
            "備考": "個別指定がない従業員の基本休日数。",
        },
        {
            "分類": "強い目標",
            "項目": "2連休 月内最低回数",
            "現在値": str(parameters.get("min_2off_per_month", 1)),
            "備考": "原則として月内に確保したい2連休の最低回数。",
        },
        {
            "分類": "強い目標",
            "項目": "2連休 月内最大回数",
            "現在値": str(parameters.get("max_2off_per_month", 2)),
            "備考": "取りすぎ確認用の目安。",
        },
        {
            "分類": "強い目標",
            "項目": "店舗標準人数",
            "現在値": "赤羽3 / 東口1 / 大宮3 / すずらん3 / 西口1",
            "備考": "生成時は標準人数に寄せる。研修や不足時は例外あり。",
        },
        {
            "分類": "絶対条件",
            "項目": "店舗最大人数",
            "現在値": "赤羽4 / 東口1 / 大宮4 / すずらん4 / 西口2",
            "備考": "赤羽・大宮の5名は原則NG。4月3日などの特殊日は月別例外で扱う。",
        },
        {
            "分類": "絶対条件",
            "項目": "1日全体人数上限",
            "現在値": "通常許容13名 / 例外14名 / 15名以上不可",
            "備考": "通常は11名体制。14名は特殊日扱い。",
        },
        {
            "分類": "運用設定",
            "項目": "ソルバー最大実行時間",
            "現在値": f"{parameters.get('solver_time_limit_seconds', 180)}秒",
            "備考": "シフト自動生成に使う最大時間。",
        },
        {
            "分類": "運用設定",
            "項目": "ソルバーシード",
            "現在値": str(parameters.get("solver_seed", 42)),
            "備考": "同じ入力で同じ結果に近づけるための値。",
        },
    ]


def load_admin_paid_leave_data() -> dict:
    """管理者が後から付けた有給調整を読み込む。"""
    if not ADMIN_PAID_LEAVE_FILE.exists():
        return {"version": 1, "adjustments": []}
    try:
        with open(ADMIN_PAID_LEAVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "adjustments": []}
    if not isinstance(data, dict):
        return {"version": 1, "adjustments": []}
    adjustments = data.get("adjustments", [])
    if not isinstance(adjustments, list):
        adjustments = []
    data["version"] = int(data.get("version", 1) or 1)
    data["adjustments"] = adjustments
    return data


def save_admin_paid_leave_data(data: dict, actor: str = "管理者") -> Path:
    """管理者有給調整を保存する。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(),
        "updated_by": actor,
        "adjustments": data.get("adjustments", []),
    }
    with open(ADMIN_PAID_LEAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return ADMIN_PAID_LEAVE_FILE


def push_admin_paid_leave_to_github() -> None:
    """設定済みなら管理者有給調整をGitHubへ保存する。"""
    try:
        from prototype.github_backup import push_local_file_to_github

        push_local_file_to_github(
            ADMIN_PAID_LEAVE_FILE,
            "config/admin_paid_leave_adjustments.json",
            "Update admin paid leave adjustments",
        )
    except Exception:
        pass


def parse_day_list_text(text: str, days_in_month: int) -> list[int]:
    """「26」や「5, 26」から月内の日付を取り出す。"""
    days = []
    for raw in str(text or "").replace("、", ",").replace("・", ",").split(","):
        raw = raw.strip().replace("日", "")
        if not raw.isdigit():
            continue
        day = int(raw)
        if 1 <= day <= days_in_month:
            days.append(day)
    return sorted(set(days))


def admin_paid_leave_adjustments_for_month(year: int, month: int) -> list[dict]:
    """指定年月の管理者有給調整だけを返す。"""
    data = load_admin_paid_leave_data()
    return [
        adj for adj in data.get("adjustments", [])
        if int(adj.get("year", 0) or 0) == int(year)
        and int(adj.get("month", 0) or 0) == int(month)
    ]


def admin_paid_leave_days_for_month(year: int, month: int) -> dict[str, int]:
    """管理者調整分の有給日数を従業員別に合算する。"""
    totals: dict[str, int] = {}
    for adj in admin_paid_leave_adjustments_for_month(year, month):
        employee = str(adj.get("employee", "")).strip()
        if not employee:
            continue
        totals[employee] = totals.get(employee, 0) + int(adj.get("days", 0) or 0)
    return totals


def admin_paid_leave_dates_for_month(year: int, month: int) -> dict[str, set[int]]:
    """管理者調整分の有給日付を従業員別に返す。"""
    dates_by_employee: dict[str, set[int]] = {}
    for adj in admin_paid_leave_adjustments_for_month(year, month):
        employee = str(adj.get("employee", "")).strip()
        if not employee:
            continue
        dates_by_employee.setdefault(employee, set()).update(
            int(d) for d in adj.get("dates", []) if str(d).isdigit()
        )
    return dates_by_employee


def combined_paid_leave_days(
    submitted_paid_leave_days: dict[str, int],
    year: int,
    month: int,
) -> dict[str, int]:
    """本人申請分と管理者調整分を合算する。"""
    combined = {
        emp: int(days or 0)
        for emp, days in (submitted_paid_leave_days or {}).items()
    }
    for emp, days in admin_paid_leave_days_for_month(year, month).items():
        combined[emp] = int(combined.get(emp, 0) or 0) + int(days or 0)
    return {emp: days for emp, days in combined.items() if days > 0}


def add_admin_paid_leave_adjustment(
    year: int,
    month: int,
    employee: str,
    days: int,
    dates: Optional[list[int]] = None,
    reason: str = "",
    actor: str = "管理者",
) -> None:
    """管理者側の有給調整を1件追加する。"""
    data = load_admin_paid_leave_data()
    data.setdefault("adjustments", []).append({
        "year": int(year),
        "month": int(month),
        "employee": str(employee),
        "days": int(days),
        "dates": sorted(set(int(d) for d in (dates or []) if int(d) > 0)),
        "reason": reason,
        "created_at": datetime.now().isoformat(),
        "created_by": actor,
    })
    save_admin_paid_leave_data(data, actor=actor)
    push_admin_paid_leave_to_github()
from prototype.models import Store, OperationMode, ShiftAssignment, MonthlyShift
from prototype.employees import ALL_EMPLOYEES, get_employee, shift_active_employees
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
from prototype.rules import (
    MAY_2026_HOLIDAY_OVERRIDES,
    STORE_KEYHOLDERS,
    SUZURAN_KEY_SUPPORT_FROM_OMIYA,
    get_monthly_work_target,
)


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

# 経営者向け: 提出データ件数の表示（バックアップ意識喚起）
if is_manager():
    try:
        from prototype.data_export import get_all_data_summary as _ds
        _data = _ds()
        if _data["submissions_total"] > 0:
            st.sidebar.markdown(
                f'<div style="background:#fef3c7; padding:8px 10px; '
                f'border-radius:6px; border-left:3px solid #f59e0b; '
                f'font-size:12px; margin:8px 0;">'
                f'💾 提出データ <strong>{_data["submissions_total"]}件</strong> 保存中<br>'
                f'<span style="color:#78350f;">「⚙️ 設定 → 💾 バックアップ」から定期DL推奨</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
    except Exception:
        pass

st.sidebar.markdown("---")
st.sidebar.caption("v0.1 プロトタイプ")
st.sidebar.caption(f"今日: {date.today()}")


# ============================================================
# ヘルパー関数
# ============================================================

SHORT_STAFF_STORE_LABELS = {
    Store.AKABANE: ("○", "赤羽", "#f59e0b", "#fffbeb"),
    Store.HIGASHIGUCHI: ("□", "東口", "#2563eb", "#eff6ff"),
    Store.OMIYA: ("△", "大宮", "#16a34a", "#f0fdf4"),
    Store.NISHIGUCHI: ("☆", "西口", "#db2777", "#fdf2f8"),
    Store.SUZURAN: ("◆", "すずらん", "#4f46e5", "#eef2ff"),
}


def detect_short_staff_by_store(shift: MonthlyShift) -> dict[int, set[Store]]:
    """
    人員不足を日付・店舗別に検出する（Validatorと同じ判定ロジックを使用）。
    判定基準:
    - 人員少マークは「人数が足りない日」を示す
    - スキル構成の注意は検証結果側で表示する
    """
    from prototype.rules import get_capacity
    from prototype.employees import get_employee
    from prototype.models import Skill

    short_by_store: dict[int, set[Store]] = {}
    days_in_month = monthrange(shift.year, shift.month)[1]

    for d in range(1, days_in_month + 1):
        mode = shift.operation_modes.get(d, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        cap = get_capacity(mode)

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

        # 各店舗の最低人数チェック
        for store, store_cap in cap.items():
            weekday = date(shift.year, shift.month, d).weekday()
            if weekday in store_cap.closed_dow:
                continue
            eco_count = store_eco.get(store, 0)
            ticket_count = store_ticket.get(store, 0)
            total_count = eco_count + ticket_count

            if store == Store.HIGASHIGUCHI:
                if not (eco_count == 1 and ticket_count == 0 and total_count == 1):
                    short_by_store.setdefault(d, set()).add(store)
                continue

            if store == Store.AKABANE and mode == OperationMode.NORMAL:
                yamamoto_present = any(
                    a.employee == "山本" and a.store == Store.AKABANE
                    for a in day_assigns
                )
                effective_ticket = ticket_count + max(0, eco_count - 1)
                if yamamoto_present:
                    effective_ticket += 1
                if eco_count < 1 or effective_ticket < 2:
                    short_by_store.setdefault(d, set()).add(store)
                continue

            if store == Store.OMIYA and mode == OperationMode.NORMAL:
                if eco_count >= 1 and total_count >= 3:
                    continue
                if total_count == 2 and eco_count >= 1:
                    short_by_store.setdefault(d, set()).add(store)
                    continue
                short_by_store.setdefault(d, set()).add(store)
                continue

            # 人員少マークでは人数不足のみを見る。スキル構成は検証結果側で確認する。
            if store == Store.SUZURAN and mode == OperationMode.NORMAL:
                if eco_count >= 1 and total_count >= 3:
                    continue
                short_by_store.setdefault(d, set()).add(store)
                continue

            if (
                eco_count < store_cap.eco_min
                or total_count < store_cap.eco_min + store_cap.ticket_min
            ):
                short_by_store.setdefault(d, set()).add(store)

    return short_by_store


def detect_short_staff_days(shift: MonthlyShift) -> set[int]:
    """人員不足がある日付だけを返す（既存処理との互換用）。"""
    return set(detect_short_staff_by_store(shift).keys())


def detect_key_warnings_by_store(shift: MonthlyShift) -> dict[int, dict[Store, str]]:
    """鍵担当がいない店舗を日付・店舗別に検出する。"""
    from prototype.rules import get_capacity

    warnings_by_store: dict[int, dict[Store, str]] = {}
    days_in_month = monthrange(shift.year, shift.month)[1]
    for d in range(1, days_in_month + 1):
        mode = shift.operation_modes.get(d, OperationMode.NORMAL)
        if mode == OperationMode.CLOSED:
            continue
        capacity_map = get_capacity(mode)
        weekday = date(shift.year, shift.month, d).weekday()
        day_assignments = shift.get_day_assignments(d)
        for store, keyholders in STORE_KEYHOLDERS.items():
            cap = capacity_map.get(store)
            if cap is None:
                continue
            if weekday in cap.closed_dow:
                continue
            workers = [
                a.employee for a in day_assignments
                if a.store == store
            ]
            if not workers:
                continue
            if any(name in keyholders for name in workers):
                continue
            status = "missing"
            if store == Store.SUZURAN:
                omiya_workers = [
                    a.employee for a in day_assignments
                    if a.store == Store.OMIYA
                ]
                if any(name in SUZURAN_KEY_SUPPORT_FROM_OMIYA for name in omiya_workers):
                    status = "support"
            warnings_by_store.setdefault(d, {})[store] = status
    return warnings_by_store


def format_key_warning_summary_for_day(statuses: dict[Store, str]) -> str:
    """編集グリッド内の鍵欄に入れる短いテキスト。"""
    if not statuses:
        return ""
    store_order = list(SHORT_STAFF_STORE_LABELS)
    labels = []
    for store in sorted(
        statuses,
        key=lambda s: store_order.index(s) if s in store_order else len(store_order),
    ):
        mark, name, _, _ = SHORT_STAFF_STORE_LABELS.get(
            store, (store.value, store.display_name, "#64748b", "#f8fafc")
        )
        prefix = "応援" if statuses[store] == "support" else "鍵"
        labels.append(f"{prefix}{mark}{name}")
    return "・".join(labels)


def format_key_warning_summary(
    shift: MonthlyShift,
    key_warnings_by_store: Optional[dict[int, dict[Store, str]]] = None,
) -> str:
    """鍵確認日を短く表示する。"""
    key_warnings_by_store = key_warnings_by_store or detect_key_warnings_by_store(shift)
    parts = []
    for day in sorted(key_warnings_by_store):
        day_text = format_key_warning_summary_for_day(key_warnings_by_store[day])
        if day_text:
            parts.append(f"{shift.month}/{day}（{day_text}）")
    return ", ".join(parts)


def format_short_staff_summary(
    shift: MonthlyShift,
    short_staff_by_store: Optional[dict[int, set[Store]]] = None,
) -> str:
    """人員不足日を店舗マーク付きで短く表示する。"""
    short_staff_by_store = short_staff_by_store or detect_short_staff_by_store(shift)
    store_order = list(SHORT_STAFF_STORE_LABELS)
    parts = []
    for day in sorted(short_staff_by_store):
        stores = sorted(
            short_staff_by_store[day],
            key=lambda s: store_order.index(s) if s in store_order else len(store_order),
        )
        labels = []
        for store in stores:
            mark, name, _, _ = SHORT_STAFF_STORE_LABELS.get(
                store, (store.value, store.display_name, "#64748b", "#f8fafc")
            )
            labels.append(f"{mark}{name}")
        parts.append(f"{shift.month}/{day}（{'・'.join(labels)}）")
    return ", ".join(parts)


def render_short_staff_marks(stores: set[Store]) -> str:
    """人員不足欄に表示する店舗別マークHTML。"""
    if not stores:
        return ""
    store_order = list(SHORT_STAFF_STORE_LABELS)
    chips = []
    for store in sorted(
        stores,
        key=lambda s: store_order.index(s) if s in store_order else len(store_order),
    ):
        mark, name, color, bg = SHORT_STAFF_STORE_LABELS.get(
            store, (store.value, store.display_name, "#64748b", "#f8fafc")
        )
        chips.append(
            f'<span style="display:inline-flex; align-items:center; gap:2px; '
            f'margin:1px 2px; padding:2px 5px; border-radius:4px; '
            f'background:{bg}; color:{color}; border:1px solid {color}; '
            f'font-size:12px; font-weight:700; white-space:nowrap;">{mark}{name}</span>'
        )
    return "".join(chips)


def render_key_warning_marks(statuses: dict[Store, str]) -> str:
    """鍵欄に表示する店舗別マークHTML。"""
    if not statuses:
        return ""
    store_order = list(SHORT_STAFF_STORE_LABELS)
    chips = []
    for store in sorted(
        statuses,
        key=lambda s: store_order.index(s) if s in store_order else len(store_order),
    ):
        mark, name, _, _ = SHORT_STAFF_STORE_LABELS.get(
            store, (store.value, store.display_name, "#64748b", "#f8fafc")
        )
        is_support = statuses[store] == "support"
        prefix = "応援" if is_support else "鍵"
        color = "#2563eb" if is_support else "#b45309"
        bg = "#eff6ff" if is_support else "#fff7ed"
        chips.append(
            f'<span style="display:inline-flex; align-items:center; gap:2px; '
            f'margin:1px 2px; padding:2px 5px; border-radius:4px; '
            f'background:{bg}; color:{color}; border:1px solid {color}; '
            f'font-size:12px; font-weight:700; white-space:nowrap;">'
            f'{prefix}{mark}{name}</span>'
        )
    return "".join(chips)


def render_shift_legend() -> None:
    """店舗記号の凡例を、色付き記号と文字記号の形を揃えて表示する。"""
    items = [
        ("○", "赤羽駅前店", "#f59e0b"),
        ("□", "赤羽東口店", "#2563eb"),
        ("△", "大宮駅前店", "#16a34a"),
        ("☆", "大宮西口店", "#db2777"),
        ("◆", "大宮すずらん通り店", "#4f46e5"),
        ("×", "休み", "#64748b"),
    ]
    html = (
        '<div style="display:flex; flex-wrap:wrap; gap:10px 14px; '
        'align-items:center; margin:4px 0 12px 0;">'
        '<strong style="margin-right:2px;">凡例</strong>'
    )
    for mark, label, color in items:
        html += (
            '<span style="display:inline-flex; align-items:center; gap:5px; '
            'white-space:nowrap; font-size:14px;">'
            f'<span style="color:{color}; font-size:20px; font-weight:900; '
            f'line-height:1;">{mark}</span>'
            f'<span>{mark} = {label}</span>'
            '</span>'
        )
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def employee_work_target_text(shift: MonthlyShift, name: str) -> str:
    """シフト表ヘッダーに出す「実績/基準」表示を返す。"""
    days_in_month = monthrange(shift.year, shift.month)[1]
    work_days = sum(
        1
        for d in range(1, days_in_month + 1)
        if (a := shift.get_assignment(name, d)) and a.store != Store.OFF
    )
    try:
        employee = get_employee(name)
        target = get_monthly_work_target(
            employee.name,
            shift.month,
            employee.annual_target_days,
        )
    except Exception:
        target = None
    if target is None:
        return str(work_days) if work_days else ""
    return f"{work_days}/{target}"


def employee_header_label(shift: MonthlyShift, name: str, html: bool = False) -> str:
    """従業員名と月間出勤日数をヘッダー表示用にまとめる。"""
    count_text = employee_work_target_text(shift, name)
    if html:
        if count_text:
            return (
                f'<span style="display:block; font-weight:800;">{escape(name)}</span>'
                f'<span style="display:block; font-size:11px; line-height:1.1; '
                f'color:#dbeafe;">{escape(count_text)}</span>'
            )
        return escape(name)
    return f"{name}\n{count_text}" if count_text else name


def render_shift_table(
    shift: MonthlyShift,
    short_staff_days: Optional[set[int]] = None,
    short_staff_by_store: Optional[dict[int, set[Store]]] = None,
    key_warnings_by_store: Optional[dict[int, dict[Store, str]]] = None,
    sticky: bool = False,
    changed_cells: Optional[set[tuple[str, int]]] = None,
    off_request_cells: Optional[set[tuple[str, int]]] = None,
    changed_cell_color: str = "#f97316",
    selectable_cells: bool = False,
    selected_cell: Optional[tuple[str, int]] = None,
) -> None:
    """シフト表をHTMLテーブルで表示"""
    days_in_month = monthrange(shift.year, shift.month)[1]
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    # 人員不足日を自動検出（指定がなければ）
    if short_staff_by_store is None:
        short_staff_by_store = detect_short_staff_by_store(shift)
    if short_staff_days is None:
        short_staff_days = set(short_staff_by_store.keys())
    else:
        short_staff_days = set(short_staff_days) | set(short_staff_by_store.keys())
    if key_warnings_by_store is None:
        key_warnings_by_store = detect_key_warnings_by_store(shift)
    changed_cells = changed_cells or set()
    off_request_cells = off_request_cells or set()
    selected_cell = selected_cell or ("", 0)

    # ヘッダー
    column_count = 2 + len(EXPORT_COLUMN_ORDER) + 2
    wrapper_style = (
        "max-height:400px; overflow:auto; border:1px solid #cbd5e1; "
        "border-radius:6px; background:white;"
        if sticky else
        "overflow-x:auto;"
    )
    table_style = (
        "border-collapse:separate; border-spacing:0; font-family:sans-serif; "
        "font-size:14px; min-width:max-content;"
    )
    title_style = (
        "position:sticky; top:0; z-index:9; background:#0f172a; "
        "color:#ffffff; box-shadow:0 1px 0 #334155;"
        if sticky else ""
    )
    header_style = (
        "position:sticky; top:41px; z-index:7;"
        if sticky else ""
    )
    left_date_style = (
        "position:sticky; left:0; z-index:6; min-width:70px; width:70px;"
        if sticky else "min-width:70px; width:70px;"
    )
    left_weekday_style = (
        "position:sticky; left:70px; z-index:6; min-width:46px; width:46px;"
        if sticky else "min-width:46px; width:46px;"
    )
    employee_header_style = "min-width:62px; line-height:1.15;"
    short_header_style = "min-width:190px; width:190px;"
    key_header_style = "min-width:150px; width:150px;"
    html = (
        f'<div style="{wrapper_style}">'
        f'<table style="{table_style}">'
    )
    html += '<thead>'
    html += (
        f'<tr style="background:#0f172a; color:white;">'
        f'<th colspan="{column_count}" style="padding:10px 12px; '
        f'border:1px solid #999; text-align:left; font-size:16px; '
        f'background:#0f172a; color:#ffffff; {title_style}">'
        f'{int(shift.year)}年{int(shift.month)}月 シフト表</th></tr>'
    )
    html += '<tr style="background:#1e3a8a; color:white;">'
    html += (
        f'<th style="padding:8px; border:1px solid #999; background:#1e3a8a; '
        f'{header_style} {left_date_style}">月日</th>'
    )
    html += (
        f'<th style="padding:8px; border:1px solid #999; background:#1e3a8a; '
        f'{header_style} {left_weekday_style}">曜</th>'
    )
    for name in EXPORT_COLUMN_ORDER:
        header_label = employee_header_label(shift, name, html=True)
        html += (
            f'<th style="padding:8px; border:1px solid #999; background:#1e3a8a; '
            f'{header_style} {employee_header_style}">{header_label}</th>'
        )
    html += (
        f'<th style="padding:8px; border:1px solid #999; background:#1e3a8a; '
        f'{header_style} {short_header_style}">人員少</th>'
    )
    html += (
        f'<th style="padding:8px; border:1px solid #999; background:#1e3a8a; '
        f'{header_style} {key_header_style}">鍵</th>'
    )
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
        current_date = date(shift.year, shift.month, d)
        wd = weekday_jp[current_date.weekday()]
        # 人員不足日は背景強調
        is_short = d in short_staff_days
        if is_short:
            bg = "#fff3cd"  # 黄色強調
        else:
            bg = "#fee2e2" if wd == "日" else ("#dbeafe" if wd == "土" else "white")
        html += f'<tr style="background:{bg};">'
        html += (
            f'<td style="padding:6px; border:1px solid #ccc; '
            f'text-align:center; font-weight:bold; background:{bg}; '
            f'{left_date_style}">{int(shift.month)}/{d}</td>'
        )
        html += (
            f'<td style="padding:6px; border:1px solid #ccc; text-align:center; '
            f'background:{bg}; {left_weekday_style}">{wd}</td>'
        )

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
            changed_style = (
                f"box-shadow:inset 0 0 0 3px {changed_cell_color}; font-weight:bold;"
                if (name, d) in changed_cells else ""
            )
            selected_style = (
                "box-shadow:inset 0 0 0 3px #2563eb; font-weight:bold;"
                if (name, d) == selected_cell else ""
            )
            off_request_style = (
                "outline:2px solid #dc2626; outline-offset:-3px; "
                "font-weight:900; color:#991b1b;"
                if (name, d) in off_request_cells and cell_text == "×" else ""
            )
            cell_body = cell_text
            if selectable_cells:
                ym = f"{int(shift.year):04d}-{int(shift.month):02d}"
                href = (
                    f"?edit_ym={ym}&edit_day={d}"
                    f"&edit_employee={quote(name)}"
                )
                cell_body = (
                    f'<a href="{href}" style="display:block; min-width:28px; '
                    f'color:inherit; text-decoration:none;">{cell_text or "&nbsp;"}</a>'
                )
            html += (
                f'<td style="padding:6px; border:1px solid #ccc; '
                f'text-align:center; background:{cell_bg}; font-size:16px; '
                f'{changed_style} {selected_style} {off_request_style}">{cell_body}</td>'
            )

        # 人員少マーク
        short_mark = render_short_staff_marks(short_staff_by_store.get(d, set()))
        short_bg = "#fff3cd" if is_short else "white"
        html += (
            f'<td style="padding:4px 6px; border:1px solid #ccc; min-width:190px; '
            f'text-align:center; background:{short_bg}; '
            f'font-weight:bold; color:#92400e;">{short_mark}</td>'
        )
        key_mark = render_key_warning_marks(key_warnings_by_store.get(d, {}))
        key_bg = "#fff7ed" if key_mark else "white"
        html += (
            f'<td style="padding:4px 6px; border:1px solid #ccc; min-width:150px; '
            f'text-align:center; background:{key_bg}; '
            f'font-weight:bold; color:#b45309;">{key_mark}</td>'
        )
        html += '</tr>'

    html += '</tbody></table></div>'
    st.markdown(html, unsafe_allow_html=True)


def get_session_shift() -> Optional[MonthlyShift]:
    """セッションに保存されたシフトを取得"""
    return st.session_state.get("current_shift")


def shift_session_key(year: int, month: int) -> str:
    """年月ごとのシフト保存キー。"""
    return f"{int(year):04d}-{int(month):02d}"


def get_session_shift_for_month(year: int, month: int) -> Optional[MonthlyShift]:
    """指定年月のシフトをセッションから取得する。"""
    shifts_by_month = st.session_state.get("shifts_by_month", {})
    shift = shifts_by_month.get(shift_session_key(year, month))
    if shift is not None:
        return shift
    current_shift = get_session_shift()
    if (
        current_shift is not None
        and int(current_shift.year) == int(year)
        and int(current_shift.month) == int(month)
    ):
        return current_shift
    return None


def save_session_shift(shift: MonthlyShift) -> None:
    """シフトをセッションに保存"""
    st.session_state["current_shift"] = shift
    shifts_by_month = dict(st.session_state.get("shifts_by_month", {}))
    shifts_by_month[shift_session_key(shift.year, shift.month)] = shift
    st.session_state["shifts_by_month"] = shifts_by_month


STORE_SYMBOL_OPTIONS = ["", "×", "○", "□", "△", "☆", "◆"]
STORE_SYMBOL_TO_STORE = {
    "×": Store.OFF,
    "○": Store.AKABANE,
    "□": Store.HIGASHIGUCHI,
    "△": Store.OMIYA,
    "☆": Store.NISHIGUCHI,
    "◆": Store.SUZURAN,
}
NO_HOME_STORE_LABEL = "（なし）"


def store_select_label(option: str) -> str:
    """店舗選択の内部値を、日本語の店舗名で表示する。"""
    if option == NO_HOME_STORE_LABEL:
        return option
    try:
        return Store[option].display_name
    except Exception:
        return str(option)


def clone_monthly_shift(shift: MonthlyShift) -> MonthlyShift:
    """シフトを編集履歴用にコピーする。"""
    return MonthlyShift(
        year=shift.year,
        month=shift.month,
        assignments=[
            ShiftAssignment(
                employee=a.employee,
                day=a.day,
                store=a.store,
                is_paid_leave=a.is_paid_leave,
            )
            for a in shift.assignments
        ],
        operation_modes=dict(shift.operation_modes),
    )


def assignment_to_symbol(assignment: Optional[ShiftAssignment]) -> str:
    """配属を表の記号に変換する。"""
    if assignment is None:
        return ""
    return assignment.store.value


def normalize_store_symbol(value) -> str:
    """編集表の値を安全な店舗記号に丸める。"""
    if value is None:
        return ""
    symbol = str(value).strip()
    return symbol if symbol in STORE_SYMBOL_OPTIONS else ""


def shift_to_editor_rows(shift: MonthlyShift) -> list[dict]:
    """シフトを編集表用の行データに変換する。"""
    days_in_month = monthrange(shift.year, shift.month)[1]
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    short_staff_by_store = detect_short_staff_by_store(shift)
    key_warnings_by_store = detect_key_warnings_by_store(shift)
    rows = []
    for day in range(1, days_in_month + 1):
        row = {
            "日": day,
            "曜": weekday_jp[date(shift.year, shift.month, day).weekday()],
        }
        for name in EXPORT_COLUMN_ORDER:
            row[name] = assignment_to_symbol(shift.get_assignment(name, day))
        row["人数少"] = format_short_staff_summary_for_day(short_staff_by_store.get(day, set()))
        row["鍵"] = format_key_warning_summary_for_day(key_warnings_by_store.get(day, {}))
        rows.append(row)
    return rows


def normalize_editor_records(rows: list[dict]) -> list[dict]:
    """編集部品から返った行データを、シフト表の標準形式に揃える。"""
    normalized = []
    for row in rows:
        try:
            day = int(row.get("日"))
        except (TypeError, ValueError):
            continue
        normalized_row = {
            "日": day,
            "曜": str(row.get("曜") or ""),
        }
        for name in EXPORT_COLUMN_ORDER:
            normalized_row[name] = normalize_store_symbol(row.get(name, ""))
        normalized_row["人数少"] = str(row.get("人数少") or "")
        normalized_row["鍵"] = str(row.get("鍵") or "")
        normalized.append(normalized_row)
    return normalized


def editor_symbol_signature(rows: list[dict]) -> str:
    """編集行の勤務記号だけを比較するための署名。"""
    payload = []
    for row in normalize_editor_records(rows):
        payload.append([
            int(row["日"]),
            [row.get(name, "") for name in EXPORT_COLUMN_ORDER],
        ])
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def refresh_editor_short_staff_column(
    base_shift: MonthlyShift,
    rows: list[dict],
) -> list[dict]:
    """未確定編集を反映した人員少欄に更新する。"""
    normalized = normalize_editor_records(rows)
    preview_shift = editor_rows_to_shift(base_shift, normalized)
    short_staff_by_store = detect_short_staff_by_store(preview_shift)
    key_warnings_by_store = detect_key_warnings_by_store(preview_shift)
    for row in normalized:
        row["人数少"] = format_short_staff_summary_for_day(
            short_staff_by_store.get(int(row["日"]), set())
        )
        row["鍵"] = format_key_warning_summary_for_day(
            key_warnings_by_store.get(int(row["日"]), {})
        )
    return normalized


def format_short_staff_summary_for_day(stores: set[Store]) -> str:
    """編集グリッド内の人員少欄に入れる短いテキスト。"""
    if not stores:
        return ""
    store_order = list(SHORT_STAFF_STORE_LABELS)
    labels = []
    for store in sorted(
        stores,
        key=lambda s: store_order.index(s) if s in store_order else len(store_order),
    ):
        mark, name, _, _ = SHORT_STAFF_STORE_LABELS.get(
            store, (store.value, store.display_name, "#64748b", "#f8fafc")
        )
        labels.append(f"{mark}{name}")
    return "・".join(labels)


def render_colored_shift_editor(
    shift: MonthlyShift,
    editor_df: pd.DataFrame,
    grid_key: str,
    locked: bool = False,
    off_request_cells: Optional[set[tuple[str, int]]] = None,
    changed_cells: Optional[set[tuple[str, int]]] = None,
):
    """色付きセルのまま編集できるシフト表を表示する。"""
    off_request_keys = json.dumps(
        sorted(f"{employee}|{int(day)}" for employee, day in (off_request_cells or set())),
        ensure_ascii=False,
    )
    changed_cell_keys = json.dumps(
        sorted(f"{employee}|{int(day)}" for employee, day in (changed_cells or set())),
        ensure_ascii=False,
    )
    cell_style = JsCode(
        """
        function(params) {
            const value = params.value;
            const fixedOffCells = new Set(__OFF_REQUEST_KEYS__);
            const changedCells = new Set(__CHANGED_CELL_KEYS__);
            const cellKey = String(params.colDef.field) + '|' + String(params.data['日']);
            const isFixedOff = fixedOffCells.has(cellKey);
            const isChanged = changedCells.has(cellKey);
            const base = {
                textAlign: 'center',
                fontWeight: '800',
                fontSize: '16px',
                borderRight: '1px solid #cbd5e1',
                borderBottom: '1px solid #e5e7eb'
            };
            function withMarkers(style) {
                if (isFixedOff && isChanged) {
                    style.boxShadow = 'inset 0 0 0 2px #dc2626, inset 0 0 0 5px #16a34a';
                    style.color = '#991b1b';
                    style.fontWeight = '900';
                } else if (isFixedOff) {
                    style.boxShadow = 'inset 0 0 0 3px #dc2626';
                    style.color = '#991b1b';
                    style.fontWeight = '900';
                } else if (isChanged) {
                    style.boxShadow = 'inset 0 0 0 3px #16a34a';
                    style.fontWeight = '900';
                }
                return style;
            }
            if (value === '○') { return withMarkers({...base, backgroundColor: '#fef3c7', color: '#92400e'}); }
            if (value === '□') { return withMarkers({...base, backgroundColor: '#dbeafe', color: '#1d4ed8'}); }
            if (value === '△') { return withMarkers({...base, backgroundColor: '#d1fae5', color: '#047857'}); }
            if (value === '☆') { return withMarkers({...base, backgroundColor: '#fce7f3', color: '#be185d'}); }
            if (value === '◆') { return withMarkers({...base, backgroundColor: '#e0e7ff', color: '#4338ca'}); }
            if (value === '×') { return withMarkers({...base, backgroundColor: '#f3f4f6', color: '#4b5563'}); }
            return withMarkers({...base, backgroundColor: '#ffffff', color: '#111827'});
        }
        """.replace("__OFF_REQUEST_KEYS__", off_request_keys)
        .replace("__CHANGED_CELL_KEYS__", changed_cell_keys)
    )
    header_style = JsCode(
        """
        function(params) {
            return {
                backgroundColor: '#1e3a8a',
                color: '#ffffff',
                fontWeight: '800',
                textAlign: 'center'
            };
        }
        """
    )
    column_defs = [
        {
            "field": "日",
            "editable": False,
            "pinned": "left",
            "width": 72,
            "cellStyle": {
                "textAlign": "center",
                "fontWeight": "800",
                "backgroundColor": "#f8fafc",
            },
        },
        {
            "field": "曜",
            "editable": False,
            "pinned": "left",
            "width": 56,
            "cellStyle": {
                "textAlign": "center",
                "fontWeight": "700",
                "backgroundColor": "#f8fafc",
            },
        },
    ]
    for name in EXPORT_COLUMN_ORDER:
        column_defs.append({
            "field": name,
            "headerName": employee_header_label(shift, name),
            "editable": not locked,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": STORE_SYMBOL_OPTIONS},
            "singleClickEdit": True,
            "width": 66,
            "cellStyle": cell_style,
            "headerClass": "shift-grid-header",
            "wrapHeaderText": True,
            "autoHeaderHeight": True,
        })
    column_defs.append({
        "field": "人数少",
        "editable": False,
        "pinned": "right",
        "width": 170,
        "cellStyle": {
            "textAlign": "center",
            "fontWeight": "800",
            "backgroundColor": "#fff3cd",
            "color": "#92400e",
            "borderLeft": "1px solid #cbd5e1",
        },
    })
    column_defs.append({
        "field": "鍵",
        "editable": False,
        "pinned": "right",
        "width": 150,
        "cellStyle": {
            "textAlign": "center",
            "fontWeight": "800",
            "backgroundColor": "#fff7ed",
            "color": "#b45309",
            "borderLeft": "1px solid #cbd5e1",
        },
    })
    grid_options = {
        "columnDefs": column_defs,
        "defaultColDef": {
            "sortable": False,
            "filter": False,
            "resizable": True,
            "suppressMenu": True,
            "headerClass": "shift-grid-header",
        },
        "stopEditingWhenCellsLoseFocus": True,
        "suppressRowClickSelection": True,
        "ensureDomOrder": True,
        "rowHeight": 34,
        "headerHeight": 54,
        "domLayout": "normal",
        "getRowStyle": JsCode(
            """
            function(params) {
                const dayLabel = params.data['曜'];
                if (dayLabel === '日') { return {backgroundColor: '#fee2e2'}; }
                if (dayLabel === '土') { return {backgroundColor: '#dbeafe'}; }
                return {backgroundColor: '#ffffff'};
            }
            """
        ),
    }
    aggrid_kwargs = {
        "gridOptions": grid_options,
        "key": grid_key,
        "height": 650,
        "width": "100%",
        "fit_columns_on_grid_load": False,
        "allow_unsafe_jscode": True,
        "theme": "streamlit",
        "reload_data": True,
    }
    if GridUpdateMode is not None:
        aggrid_kwargs["update_mode"] = GridUpdateMode.VALUE_CHANGED
    return AgGrid(editor_df, **aggrid_kwargs)


def editor_rows_to_records(editor_value) -> list[dict]:
    """st.data_editor の戻り値を list[dict] に揃える。"""
    if hasattr(editor_value, "to_dict"):
        return editor_value.to_dict("records")
    if isinstance(editor_value, list):
        return editor_value
    return []


def editor_rows_to_shift(base_shift: MonthlyShift, rows: list[dict]) -> MonthlyShift:
    """編集表の内容からプレビュー用シフトを作る。"""
    visible_names = set(EXPORT_COLUMN_ORDER)
    updated = MonthlyShift(
        year=base_shift.year,
        month=base_shift.month,
        operation_modes=dict(base_shift.operation_modes),
    )
    updated.assignments = [
        ShiftAssignment(
            employee=a.employee,
            day=a.day,
            store=a.store,
            is_paid_leave=a.is_paid_leave,
        )
        for a in base_shift.assignments
        if a.employee not in visible_names
    ]
    for row in rows:
        try:
            day = int(row.get("日"))
        except (TypeError, ValueError):
            continue
        for name in EXPORT_COLUMN_ORDER:
            symbol = normalize_store_symbol(row.get(name, ""))
            if not symbol:
                continue
            store = STORE_SYMBOL_TO_STORE[symbol]
            updated.assignments.append(ShiftAssignment(employee=name, day=day, store=store))
    return updated


def set_shift_cell_symbol(
    shift: MonthlyShift,
    employee: str,
    day: int,
    symbol: str,
) -> None:
    """シフト内の1セルを指定記号へ変更する。"""
    shift.assignments = [
        a for a in shift.assignments
        if not (a.employee == employee and a.day == day)
    ]
    symbol = normalize_store_symbol(symbol)
    if symbol:
        shift.assignments.append(
            ShiftAssignment(
                employee=employee,
                day=day,
                store=STORE_SYMBOL_TO_STORE[symbol],
            )
        )


def apply_pending_symbol_changes(
    base_shift: MonthlyShift,
    pending_changes: dict[tuple[str, int], str],
) -> MonthlyShift:
    """手動修正の未確定変更を反映したプレビュー用シフトを作る。"""
    preview = clone_monthly_shift(base_shift)
    for (employee, day), symbol in pending_changes.items():
        set_shift_cell_symbol(preview, employee, int(day), symbol)
    return preview


def get_editor_changed_cells(base_shift: MonthlyShift, rows: list[dict]) -> set[tuple[str, int]]:
    """編集表で変更されたセルを返す。"""
    changed = set()
    for row in rows:
        try:
            day = int(row.get("日"))
        except (TypeError, ValueError):
            continue
        for name in EXPORT_COLUMN_ORDER:
            before = assignment_to_symbol(base_shift.get_assignment(name, day))
            after = normalize_store_symbol(row.get(name, ""))
            if before != after:
                changed.add((name, day))
    return changed


def get_validation_context_for_shift(shift: MonthlyShift) -> dict:
    """生成時に使った希望データを、対象シフトに合う場合だけ返す。"""
    ym = f"{int(shift.year):04d}-{int(shift.month):02d}"
    inputs_by_month = st.session_state.get("validation_inputs_by_month", {})
    inputs = inputs_by_month.get(ym) or st.session_state.get("last_validation_inputs", {})
    if inputs.get("ym") != ym:
        return {
            "work_requests": [],
            "off_requests": {},
            "prev_month": [],
            "holiday_overrides": {},
            "exact_holiday_days": {},
            "employee_max_consecutive_work": {},
            "employee_max_consecutive_off": {},
            "monthly_store_count_rules": [],
        }
    return {
        "work_requests": inputs.get("work_requests", []),
        "preferred_work_requests": inputs.get("preferred_work_requests", []),
        "preferred_work_groups": inputs.get("preferred_work_groups", []),
        "off_requests": inputs.get("off_requests", {}),
        "prev_month": inputs.get("prev_month", []),
        "holiday_overrides": inputs.get("holiday_overrides", {}),
        "exact_holiday_days": inputs.get("exact_holiday_days", {}),
        "employee_max_consecutive_work": inputs.get("employee_max_consecutive_work", {}),
        "employee_max_consecutive_off": inputs.get("employee_max_consecutive_off", {}),
        "monthly_store_count_rules": inputs.get("monthly_store_count_rules", []),
    }


def save_validation_context(inputs: dict) -> None:
    """生成時に使った希望データを年月ごとに保存する。"""
    st.session_state["last_validation_inputs"] = dict(inputs)
    ym = inputs.get("ym")
    if not ym:
        return
    inputs_by_month = dict(st.session_state.get("validation_inputs_by_month", {}))
    inputs_by_month[str(ym)] = dict(inputs)
    st.session_state["validation_inputs_by_month"] = inputs_by_month


def restore_validation_context_for_month(
    year: int,
    month: int,
    rule_cfg: RuleConfig,
) -> dict:
    """下書き・確定版の復元時に、その月の提出データを検証条件として読み直す。"""
    from prototype.submission_loader import load_submissions_for_month

    days_in_month = monthrange(int(year), int(month))[1]
    sub_data = load_submissions_for_month(
        int(year), int(month), shift_submission_employee_names(),
    )

    def _valid_days(days_list) -> list[int]:
        valid = []
        for day in days_list or []:
            try:
                day_int = int(day)
            except (TypeError, ValueError):
                continue
            if 1 <= day_int <= days_in_month:
                valid.append(day_int)
        return valid

    off_requests = {
        emp: _valid_days(days)
        for emp, days in sub_data.off_requests.items()
    }
    off_requests = {emp: days for emp, days in off_requests.items() if days}
    off_sets = {emp: set(days) for emp, days in off_requests.items()}

    work_requests = [
        (emp, int(day), store)
        for emp, day, store in sub_data.work_requests
        if 1 <= int(day) <= days_in_month and int(day) not in off_sets.get(emp, set())
    ]
    preferred_work_requests = [
        (emp, int(day), store)
        for emp, day, store in getattr(sub_data, "preferred_work_requests", [])
        if 1 <= int(day) <= days_in_month and int(day) not in off_sets.get(emp, set())
    ]
    for emp, day, store in system_monthly_preferred_work_requests(int(year), int(month)):
        if int(day) in off_sets.get(emp, set()):
            continue
        item = (emp, int(day), store)
        if item not in preferred_work_requests:
            preferred_work_requests.append(item)

    preferred_work_groups = []
    for emp, candidate_days, required_count, store in getattr(
        sub_data, "preferred_work_groups", []
    ):
        filtered = [
            int(day) for day in candidate_days
            if 1 <= int(day) <= days_in_month
            and int(day) not in off_sets.get(emp, set())
        ]
        if filtered:
            preferred_work_groups.append((
                emp,
                sorted(set(filtered)),
                min(int(required_count), len(set(filtered))),
                store,
            ))

    holiday_overrides = {}
    exact_holiday_days = {}
    effective_paid_leave_days = combined_paid_leave_days(
        sub_data.paid_leave_days, int(year), int(month),
    )
    for emp_name, paid_days in effective_paid_leave_days.items():
        try:
            emp = get_employee(emp_name)
            base_target = get_monthly_work_target(
                emp.name,
                int(month),
                emp.annual_target_days,
            )
            if base_target:
                holiday_overrides[emp_name] = days_in_month - base_target + int(paid_days)
        except Exception:
            pass
    for emp_name, requested_days in getattr(sub_data, "requested_holiday_days", {}).items():
        try:
            requested_days_int = int(requested_days)
        except (TypeError, ValueError):
            continue
        if 0 <= requested_days_int <= days_in_month:
            holiday_overrides[emp_name] = max(
                int(holiday_overrides.get(emp_name, 0) or 0),
                requested_days_int,
            )
            exact_holiday_days[emp_name] = requested_days_int

    context = {
        "ym": f"{int(year):04d}-{int(month):02d}",
        "off_requests": off_requests,
        "work_requests": work_requests,
        "preferred_work_requests": preferred_work_requests,
        "preferred_work_groups": preferred_work_groups,
        "prev_month": [],
        "holiday_overrides": holiday_overrides,
        "exact_holiday_days": exact_holiday_days,
        "employee_max_consecutive_work": dict(
            getattr(sub_data, "max_consecutive_work_days", {})
        ),
        "employee_max_consecutive_off": dict(
            getattr(sub_data, "max_consecutive_off_days", {})
        ),
        "monthly_store_count_rules": active_monthly_store_count_rules(
            rule_cfg, int(year), int(month),
        ),
    }
    save_validation_context(context)
    return context


def build_part_time_paid_leave_suggestions(
    shift: MonthlyShift,
    validation_context: dict,
) -> list[dict]:
    """パートの出勤希望が休みになった日を、有給調整候補として返す。"""
    off_requests = validation_context.get("off_requests", {}) or {}
    work_preferences = []
    work_preferences.extend(validation_context.get("work_requests", []) or [])
    work_preferences.extend(validation_context.get("preferred_work_requests", []) or [])
    existing_admin_dates = admin_paid_leave_dates_for_month(shift.year, shift.month)
    suggestions: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for employee, day, requested_store in work_preferences:
        try:
            day = int(day)
            emp = get_employee(str(employee))
        except Exception:
            continue
        if emp.employment_status != EmploymentStatus.PART_TIME:
            continue
        if day in set(int(d) for d in off_requests.get(employee, [])):
            continue
        if day in existing_admin_dates.get(employee, set()):
            continue
        key = (str(employee), day)
        if key in seen:
            continue
        seen.add(key)
        assignment = shift.get_assignment(str(employee), day)
        if assignment is not None and assignment.store != Store.OFF:
            continue
        suggestions.append({
            "employee": str(employee),
            "day": day,
            "requested_store": requested_store,
            "reason": "出勤希望日が調整上休みになっています",
        })
    return sorted(suggestions, key=lambda x: (x["day"], x["employee"]))


def render_part_time_paid_leave_suggestions(
    shift: MonthlyShift,
    validation_context: dict,
    key_prefix: str,
) -> None:
    """パート・アルバイトの有給調整候補を画面に出す。"""
    suggestions = build_part_time_paid_leave_suggestions(shift, validation_context)
    if not suggestions:
        return
    st.info(
        "パート・アルバイトの出勤希望が休みになっている日があります。"
        "必要に応じて、管理者調整として有給を付けられます。"
    )
    rows = []
    for item in suggestions:
        store = item.get("requested_store")
        rows.append({
            "氏名": item["employee"],
            "日付": f"{int(shift.month)}/{int(item['day'])}",
            "希望店舗": store.display_name if isinstance(store, Store) else "指定なし",
            "状態": "休み",
            "候補": "有給調整",
        })
    st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("有給調整をこの画面で追加", expanded=False):
        for item in suggestions:
            label = f"{item['employee']} {int(shift.month)}/{int(item['day'])} を有給1日として記録"
            if st.button(label, key=f"{key_prefix}_paid_leave_{item['employee']}_{item['day']}"):
                add_admin_paid_leave_adjustment(
                    shift.year,
                    shift.month,
                    item["employee"],
                    1,
                    dates=[int(item["day"])],
                    reason="出勤希望日を調整上休みにしたため",
                    actor="管理者",
                )
                st.success("管理者有給調整を追加しました。")
                st.rerun()


def run_shift_validation(**kwargs):
    """Cloud側で古い検証関数が混ざっても落ちないよう、受け取れる引数だけ渡す。"""
    allowed = inspect.signature(validate).parameters
    safe_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    return validate(**safe_kwargs)


def call_with_supported_kwargs(func, *args, **kwargs):
    """Cloud側で古い関数が混ざっても、受け取れる引数だけ渡す。"""
    allowed = inspect.signature(func).parameters
    safe_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    return func(*args, **safe_kwargs)


def get_fixed_off_edit_violations(
    rows: list[dict],
    off_request_cells: set[tuple[str, int]],
) -> list[str]:
    """本人の×希望を別記号へ変えていないか確認する。"""
    violations = []
    for row in rows:
        try:
            day = int(row.get("日"))
        except (TypeError, ValueError):
            continue
        for name in EXPORT_COLUMN_ORDER:
            if (name, day) not in off_request_cells:
                continue
            if normalize_store_symbol(row.get(name, "")) != "×":
                violations.append(f"{name}さん {day}日")
    return violations


def format_day_list(days) -> str:
    """日付リストを画面表示用に整える。"""
    safe_days = sorted({int(d) for d in days if str(d).isdigit()})
    if not safe_days:
        return "なし"
    return "、".join(f"{d}日" for d in safe_days)


def render_scrollable_request_table(rows: list[dict]) -> None:
    """本人提出希望を横スクロール可能な表で表示する。"""
    if not rows:
        st.caption("表示する提出データがありません")
        return
    columns = [
        "氏名", "状態", "× 休み希望（絶対）", "△ できれば休み",
        "出勤希望", "有給", "自由記載から反映", "備考",
    ]
    widths = {
        "氏名": 110,
        "状態": 110,
        "× 休み希望（絶対）": 260,
        "△ できれば休み": 220,
        "出勤希望": 180,
        "有給": 90,
        "自由記載から反映": 220,
        "備考": 560,
    }
    html_parts = [
        '<div style="overflow:auto; max-height:430px; border:1px solid #e5e7eb; '
        'border-radius:6px; background:white;">',
        '<table style="border-collapse:collapse; min-width:1530px; width:max-content; '
        'font-size:14px;">',
        '<thead><tr>',
    ]
    for col in columns:
        html_parts.append(
            f'<th style="position:sticky; top:0; z-index:1; background:#f8fafc; '
            f'border:1px solid #e5e7eb; padding:8px; text-align:left; '
            f'min-width:{widths[col]}px;">{escape(col)}</th>'
        )
    html_parts.append('</tr></thead><tbody>')
    for row in rows:
        html_parts.append('<tr>')
        for col in columns:
            value = escape(str(row.get(col, ""))).replace("\n", "<br>")
            white_space = "pre-wrap" if col == "備考" else "nowrap"
            html_parts.append(
                f'<td style="border:1px solid #e5e7eb; padding:8px; '
                f'vertical-align:top; min-width:{widths[col]}px; '
                f'white-space:{white_space};">{value}</td>'
            )
        html_parts.append('</tr>')
    html_parts.append('</tbody></table></div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def render_scrollable_review_table(rows: list[dict]) -> None:
    """提出前確認の内容を横スクロール可能な表で表示する。"""
    columns = ["項目", "内容", "日数"]
    widths = {"項目": 220, "内容": 720, "日数": 90}
    html_parts = [
        '<div style="overflow:auto; max-height:360px; border:1px solid #e5e7eb; '
        'border-radius:6px; background:white;">',
        '<table style="border-collapse:collapse; min-width:1030px; width:max-content; '
        'font-size:14px;">',
        '<thead><tr>',
    ]
    for col in columns:
        html_parts.append(
            f'<th style="position:sticky; top:0; z-index:1; background:#f8fafc; '
            f'border:1px solid #e5e7eb; padding:8px; text-align:left; '
            f'min-width:{widths[col]}px;">{escape(col)}</th>'
        )
    html_parts.append('</tr></thead><tbody>')
    for row in rows:
        html_parts.append('<tr>')
        for col in columns:
            value = escape(str(row.get(col, ""))).replace("\n", "<br>")
            white_space = "pre-wrap" if col == "内容" else "nowrap"
            html_parts.append(
                f'<td style="border:1px solid #e5e7eb; padding:8px; '
                f'vertical-align:top; min-width:{widths[col]}px; '
                f'white-space:{white_space};">{value}</td>'
            )
        html_parts.append('</tr>')
    html_parts.append('</tbody></table></div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def render_scrollable_dict_table(
    rows: list[dict],
    columns: list[str],
    widths: dict[str, int],
    empty_message: str,
    max_height: int = 360,
) -> None:
    """任意の dict 行を横スクロール可能な表で表示する。"""
    if not rows:
        st.caption(empty_message)
        return
    min_width = sum(widths.get(col, 140) for col in columns)
    html_parts = [
        f'<div style="overflow:auto; max-height:{max_height}px; border:1px solid #e5e7eb; '
        'border-radius:6px; background:white;">',
        f'<table style="border-collapse:collapse; min-width:{min_width}px; width:max-content; '
        'font-size:14px;">',
        '<thead><tr>',
    ]
    for col in columns:
        html_parts.append(
            f'<th style="position:sticky; top:0; z-index:1; background:#f8fafc; '
            f'border:1px solid #e5e7eb; padding:8px; text-align:left; '
            f'min-width:{widths.get(col, 140)}px;">{escape(col)}</th>'
        )
    html_parts.append('</tr></thead><tbody>')
    for row in rows:
        html_parts.append('<tr>')
        for col in columns:
            value = escape(str(row.get(col, ""))).replace("\n", "<br>")
            html_parts.append(
                f'<td style="border:1px solid #e5e7eb; padding:8px; '
                f'vertical-align:top; min-width:{widths.get(col, 140)}px; '
                'white-space:pre-wrap;">'
                f'{value}</td>'
            )
        html_parts.append('</tr>')
    html_parts.append('</tbody></table></div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def load_public_shift_for_employee_view(
    year: int,
    month: int,
) -> tuple[Optional[MonthlyShift], str]:
    """従業員向けに公開できる確定シフトを読み込む。"""
    lock_mgr = ShiftLockManager()
    backup = ShiftBackup()
    lock_info = lock_mgr.get_lock_info(int(year), int(month))
    if lock_info:
        snapshot_path = (
            backup.backup_dir
            / f"{int(year):04d}-{int(month):02d}"
            / lock_info.snapshot_file
        )
        if snapshot_path.exists():
            return backup.load_shift(snapshot_path), "ロック済み確定版"

    latest_finalized = backup.get_latest_shift(int(year), int(month), kind="finalized")
    if latest_finalized is not None:
        return latest_finalized, "最新の確定保存版"
    return None, ""


def render_employee_confirmed_shift(
    shift: MonthlyShift,
    employee_name: str,
) -> None:
    """従業員本人向けに、自分の確定シフトだけを表示する。"""
    rows = []
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    for day in range(1, monthrange(int(shift.year), int(shift.month))[1] + 1):
        assignment = shift.get_assignment(employee_name, day)
        if assignment is None or assignment.store == Store.OFF:
            work_text = "有給" if assignment is not None and assignment.is_paid_leave else "休み"
        else:
            work_text = f"{assignment.store.value} {assignment.store.display_name}"
            if assignment.is_paid_leave:
                work_text += "（有給）"
        rows.append({
            "日付": f"{int(shift.month)}/{day}",
            "曜": weekday_jp[date(int(shift.year), int(shift.month), day).weekday()],
            "勤務": work_text,
        })

    render_scrollable_dict_table(
        rows,
        columns=["日付", "曜", "勤務"],
        widths={"日付": 90, "曜": 70, "勤務": 260},
        empty_message="表示できる確定シフトがありません",
        max_height=560,
    )


def _safe_preference_days(values) -> list[int]:
    """希望提出JSONの表記ゆれから日付だけを取り出す。"""
    days: list[int] = []
    if values is None:
        return days
    if isinstance(values, (str, int)):
        values = [values]
    if isinstance(values, dict):
        values = values.values()
    for value in values:
        if isinstance(value, dict):
            for key in ("day", "日", "date"):
                if str(value.get(key, "")).isdigit():
                    days.append(int(value[key]))
                    break
            continue
        if str(value).isdigit():
            days.append(int(value))
    return sorted(set(days))


def _extract_preference_days(raw, author: str) -> list[int]:
    """希望提出JSONの name/value/list/dict 形式を吸収して日付を返す。"""
    if isinstance(raw, dict):
        if author in raw:
            return _safe_preference_days(raw.get(author))
        if "employee" in raw and raw.get("employee") != author:
            return []
        for key in ("days", "candidate_days", "off_requests", "work_requests"):
            if key in raw:
                return _safe_preference_days(raw.get(key))
        combined: list[int] = []
        for value in raw.values():
            combined.extend(_safe_preference_days(value))
        return sorted(set(combined))
    if isinstance(raw, list):
        days: list[int] = []
        for item in raw:
            if isinstance(item, dict):
                if item.get("employee") and item.get("employee") != author:
                    continue
                days.extend(_extract_preference_days(item, author))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                if item[0] == author:
                    days.extend(_safe_preference_days(item[1:]))
            else:
                days.extend(_safe_preference_days([item]))
        return sorted(set(days))
    return _safe_preference_days(raw)


def _extract_marked_day_preferences(data: dict, author: str) -> tuple[list[int], list[int], list[int]]:
    """day_preferences 形式の古い/別形式データから ×・△・出勤希望を拾う。"""
    off_days: list[int] = []
    flexible_days: list[int] = []
    work_days: list[int] = []
    raw_items = (
        data.get("day_preferences")
        or data.get("preferences")
        or data.get("requests")
        or data.get("entries")
        or []
    )
    if isinstance(raw_items, dict):
        raw_items = raw_items.values()
    if not isinstance(raw_items, list):
        return off_days, flexible_days, work_days
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("employee") and item.get("employee") != author:
            continue
        day = item.get("day") or item.get("日") or item.get("date")
        if not str(day).isdigit():
            continue
        mark = str(item.get("mark") or item.get("希望") or item.get("value") or "")
        if mark in ("×", "OFF_REQUEST", "休み", "休み希望"):
            off_days.append(int(day))
        elif mark in ("△", "PREFER_OFF", "できれば休み"):
            flexible_days.append(int(day))
        elif mark.startswith("○") or mark in ("AVAILABLE", "出勤", "出勤希望"):
            work_days.append(int(day))
    return sorted(set(off_days)), sorted(set(flexible_days)), sorted(set(work_days))


def enrich_submission_days_from_files(
    backup_mgr: ShiftBackup,
    year: int,
    month: int,
    submission_status: dict,
) -> dict:
    """提出済み一覧に日付が入っていない場合、元JSONから補完する。"""
    month_dir = backup_mgr.backup_dir / f"{year}-{month:02d}"
    for submitted in submission_status.get("submitted", []):
        file_name = submitted.get("file")
        author = submitted.get("employee") or ""
        if not file_name or not author:
            continue
        file_path = month_dir / file_name
        if not file_path.exists():
            legacy_path = (
                backup_mgr.backup_dir.parent
                / "preferences"
                / f"{year:04d}-{month:02d}"
                / file_name
            )
            if legacy_path.exists():
                file_path = legacy_path
            else:
                continue
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        off_days = _extract_preference_days(data.get("off_requests", {}), author)
        flexible_days = _extract_preference_days(data.get("flexible_off", []), author)
        work_days = _extract_preference_days(data.get("work_requests", []), author)
        marked_off, marked_flexible, marked_work = _extract_marked_day_preferences(data, author)
        off_days = sorted(set(off_days) | set(marked_off))
        flexible_days = sorted(set(flexible_days) | set(marked_flexible))
        work_days = sorted(set(work_days) | set(marked_work))

        submitted["off_request_days"] = off_days
        submitted["off_request_count"] = len(off_days)
        submitted["flexible_off_days"] = flexible_days
        submitted["flexible_off_count"] = len(flexible_days)
        submitted["work_request_days"] = work_days
        submitted["paid_leave_days"] = int(data.get("paid_leave_days", submitted.get("paid_leave_days", 0)) or 0)

        notes = data.get("natural_language_notes", {})
        note_text = ""
        if isinstance(notes, dict) and notes.get(author):
            note_text = notes[author]
            submitted["note"] = note_text
            submitted["note_excerpt"] = note_text[:50] + ("..." if len(note_text) > 50 else "")
            submitted["has_note"] = True
        try:
            from prototype.submission_loader import parse_natural_language_note
            parsed_note = parse_natural_language_note(note_text, year, month)
            off_days = sorted(set(off_days) | set(parsed_note.off_requests))
            flexible_extra_days = [
                day
                for candidate_days, _ in parsed_note.flexible_off
                for day in candidate_days
            ]
            flexible_days = sorted(set(flexible_days) | set(flexible_extra_days))
            work_days = sorted(set(work_days) | {day for day, _ in parsed_note.work_requests})
            if parsed_note.work_groups:
                group_labels = []
                for candidate_days, required_count, store in parsed_note.work_groups:
                    day_label = "・".join(f"{int(day)}日" for day in candidate_days)
                    store_label = f"（{store.display_name}希望）" if store else ""
                    group_labels.append(
                        f"{day_label}のうち{int(required_count)}日出勤希望{store_label}"
                    )
                submitted["work_request_group_labels"] = group_labels
            if parsed_note.paid_leave_days is not None:
                submitted["paid_leave_days"] = max(
                    int(submitted.get("paid_leave_days", 0) or 0),
                    int(parsed_note.paid_leave_days),
                )
            if parsed_note.requested_holiday_days is not None:
                submitted["requested_holiday_days"] = parsed_note.requested_holiday_days
            if parsed_note.max_consecutive_work_days is not None:
                submitted["max_consecutive_work_days"] = parsed_note.max_consecutive_work_days
            if parsed_note.max_consecutive_off_days is not None:
                submitted["max_consecutive_off_days"] = parsed_note.max_consecutive_off_days
            if parsed_note.preferred_consecutive_off_days is not None:
                submitted["preferred_consecutive_off_days"] = parsed_note.preferred_consecutive_off_days
        except Exception:
            pass
        submitted["off_request_days"] = off_days
        submitted["off_request_count"] = len(off_days)
        submitted["flexible_off_days"] = flexible_days
        submitted["flexible_off_count"] = len(flexible_days)
        submitted["work_request_days"] = work_days
    return submission_status


def build_off_request_cells(off_requests: dict[str, list[int]]) -> set[tuple[str, int]]:
    """本人が提出した絶対休み（×）のセル集合を作る。"""
    cells: set[tuple[str, int]] = set()
    for emp_name, days in (off_requests or {}).items():
        for day in days:
            if str(day).isdigit():
                cells.add((emp_name, int(day)))
    return cells


def shift_submission_employee_names() -> list[str]:
    """希望提出の対象者リスト。山本さんは補助・特別枠として含める。"""
    names = [e.name for e in shift_active_employees() if not e.is_auxiliary]
    try:
        yamamoto = get_employee("山本")
        if yamamoto.name not in names:
            names.append(yamamoto.name)
    except Exception:
        if "山本" not in names:
            names.append("山本")
    return names


def save_shift_snapshot_with_github(
    backup_mgr: ShiftBackup,
    shift: MonthlyShift,
    kind: str,
    author: str,
    note: str = "",
) -> Path:
    """シフトをローカル保存し、設定済みならGitHubにも自動保存する。"""
    path = backup_mgr.save_shift(shift, kind=kind, author=author, note=note)
    try:
        from prototype.github_backup import push_shift_to_github
        push_shift_to_github(path, int(shift.year), int(shift.month), kind=kind)
    except Exception:
        pass
    return path


def push_lock_file_to_github(lock_path: Path, year: int, month: int, action: str) -> None:
    """ロック情報をGitHubへ自動保存する。"""
    try:
        from prototype.github_backup import push_lock_to_github
        push_lock_to_github(lock_path, int(year), int(month), action=action)
    except Exception:
        pass


def record_edit_history_with_github(
    backup_mgr: ShiftBackup,
    year: int,
    month: int,
    before_shift: MonthlyShift,
    after_shift: MonthlyShift,
    changed_cells: set[tuple[str, int]],
    actor: str,
    reason: str,
) -> None:
    """手動編集履歴をローカルとGitHubへ残す。"""
    if not changed_cells:
        return
    for employee, day in sorted(changed_cells, key=lambda x: (x[1], x[0])):
        before_symbol = assignment_to_symbol(before_shift.get_assignment(employee, int(day))) or "空白"
        after_symbol = assignment_to_symbol(after_shift.get_assignment(employee, int(day))) or "空白"
        if before_symbol == after_symbol:
            continue
        try:
            backup_mgr.log_edit(
                int(year), int(month),
                employee=employee,
                day=int(day),
                before_store=before_symbol,
                after_store=after_symbol,
                actor=actor,
                reason=reason,
            )
        except Exception:
            pass
    try:
        from prototype.github_backup import push_edit_log_to_github
        log_path = (
            backup_mgr.backup_dir
            / f"{int(year):04d}-{int(month):02d}"
            / f"edits_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        )
        if log_path.exists():
            push_edit_log_to_github(log_path, int(year), int(month))
    except Exception:
        pass


def system_monthly_store_count_rules(year: int, month: int) -> list[dict]:
    """チャットで固まった標準の月次運用ルールを返す。"""
    rules = []
    if int(year) == 2026 and int(month) == 5:
        rules.append({
            "name": "2026年5月 顧問は最大2日まで",
            "employee": "顧問",
            "stores": ["AKABANE", "HIGASHIGUCHI"],
            "count": 2,
            "severity": "ERROR",
            "comparison": "max",
            "source": "system",
        })
    return rules


def system_monthly_preferred_work_requests(
    year: int,
    month: int,
) -> list[tuple[str, int, Store]]:
    """特定月だけ、日付・店舗まで明示されている実運用例外を返す。"""
    return []


def active_monthly_store_count_rules(
    rule_cfg: RuleConfig,
    year: int,
    month: int,
) -> list[dict]:
    """対象年月に有効な月別配置ルールだけを生成・検証用に取り出す。"""
    active_rules = system_monthly_store_count_rules(year, month)
    for rule in getattr(rule_cfg, "custom_rules", []):
        if not getattr(rule, "enabled", True):
            continue
        if getattr(rule, "rule_type", "note") != "employee_store_count":
            continue
        try:
            target_year = getattr(rule, "target_year", None)
            target_month = getattr(rule, "target_month", None)
            if target_year is not None and int(target_year) != int(year):
                continue
            if target_month is not None and int(target_month) != int(month):
                continue
        except (TypeError, ValueError):
            continue
        active_rules.append({
            "name": rule.name,
            "employee": getattr(rule, "employee", ""),
            "stores": list(getattr(rule, "stores", []) or []),
            "count": int(getattr(rule, "count", 0) or 0),
            "severity": getattr(rule, "severity", "WARNING"),
            "comparison": getattr(rule, "comparison", "min"),
        })
    return active_rules


def active_monthly_custom_rules(
    rule_cfg: RuleConfig,
    year: int,
    month: int,
) -> list[CustomRule]:
    """対象年月に有効な、画面で追加された月別ルールを返す。"""
    active_rules = []
    for rule in getattr(rule_cfg, "custom_rules", []):
        if not getattr(rule, "enabled", True):
            continue
        try:
            target_year = getattr(rule, "target_year", None)
            target_month = getattr(rule, "target_month", None)
            if target_year is not None and int(target_year) != int(year):
                continue
            if target_month is not None and int(target_month) != int(month):
                continue
        except (TypeError, ValueError):
            continue
        active_rules.append(rule)
    return active_rules


def monthly_rule_store_label(stores: list) -> str:
    """月別ルールの店舗名を画面表示用に整える。"""
    labels = []
    for raw_store in stores or []:
        try:
            store = raw_store if isinstance(raw_store, Store) else Store[str(raw_store)]
        except Exception:
            store = next(
                (
                    s for s in Store
                    if s.display_name == str(raw_store) or s.value == str(raw_store)
                ),
                None,
            )
        labels.append(store.display_name if isinstance(store, Store) else str(raw_store))
    return "・".join(labels) if labels else "店舗指定なし"


def monthly_rule_display_text(rule: dict) -> str:
    """月別ルール1件を自然な日本語にする。"""
    store_label = monthly_rule_store_label(rule.get("stores") or [])
    return (
        f"{rule.get('name', '月別ルール')}: "
        f"{rule.get('employee') or 'スタッフ指定なし'} / "
        f"{store_label} / {format_monthly_rule_condition(rule)}"
    )


def advisor_candidate_limit(year: int, month: int) -> int:
    """顧問を候補として試算する最大日数。自動確定はしない。"""
    # 2026年5月はGWの希望休集中に加え、牧野さんの東口・西口単独NGを反映すると
    # 2日候補では下書き自体が出ない。候補表示だけ3日まで広げ、最終確定は手動で行う。
    if int(year) == 2026 and int(month) == 5:
        return 3
    return 2


def emergency_advisor_candidate_limit(year: int, month: int) -> int:
    """通常候補でも解なしの時だけ使う、下書き作成用の緊急候補枠。"""
    return max(advisor_candidate_limit(year, month), 10)


def advisor_candidate_rows_from_shift(shift: Optional[MonthlyShift]) -> list[dict]:
    """顧問を許可した試算シフトから、顧問投入候補だけ取り出す。"""
    if shift is None:
        return []
    rows = []
    for assignment in sorted(
        shift.assignments, key=lambda a: (int(a.day), a.employee),
    ):
        if assignment.employee != "顧問" or assignment.store == Store.OFF:
            continue
        rows.append({
            "日付": f"{int(shift.month)}/{int(assignment.day)}",
            "候補店舗": assignment.store.display_name,
            "扱い": "候補のみ",
        })
    return rows


def strip_advisor_assignments(source: MonthlyShift) -> MonthlyShift:
    """顧問候補入りの試算から、顧問だけ外した確認用下書きを作る。"""
    shift = MonthlyShift(year=source.year, month=source.month)
    shift.operation_modes = dict(getattr(source, "operation_modes", {}) or {})
    shift.comments = list(getattr(source, "comments", []) or [])
    advisor_days = set()
    for assignment in source.assignments:
        if assignment.employee == "顧問":
            advisor_days.add(int(assignment.day))
            continue
        shift.assignments.append(assignment)
    days_in_month = monthrange(int(source.year), int(source.month))[1]
    existing_advisor_days = {
        int(a.day) for a in shift.assignments if a.employee == "顧問"
    }
    for day in range(1, days_in_month + 1):
        if day not in existing_advisor_days:
            shift.assignments.append(ShiftAssignment(
                employee="顧問", day=day, store=Store.OFF,
            ))
    return shift


def advisor_candidate_trigger_issues(validation_result) -> list[dict]:
    """顧問候補を出すきっかけになる人数系の検証結果を抜き出す。"""
    rows = []
    for issue in getattr(validation_result, "issues", []):
        category = str(getattr(issue, "category", ""))
        if not (
            "店舗人数" in category
            or "人数少" in category
            or "大宮アンカー" in category
        ):
            continue
        rows.append({
            "日付": (
                f"{int(getattr(issue, 'month', 0) or 0)}/{int(issue.day)}"
                if getattr(issue, "day", None) else ""
            ),
            "区分": category,
            "内容": str(getattr(issue, "message", "")),
        })
    return rows


def render_advisor_candidate_notice(input_summary: dict) -> None:
    """直近生成結果に、顧問投入候補を表示する。"""
    candidates = input_summary.get("advisor_candidates") or []
    trigger_issues = input_summary.get("advisor_candidate_triggers") or []
    if not candidates and not trigger_issues:
        return
    if input_summary.get("emergency_draft_used"):
        st.warning(
            "通常条件では下書きも作れなかったため、緊急用に顧問候補枠を広げて"
            "確認用下書きを作成しています。顧問は自動確定していません。"
            "エラー・警告を見ながら手動で調整してください。"
        )
    elif input_summary.get("advisor_candidate_base_used"):
        st.warning(
            "専務/顧問なしではシフトを確定できないため、"
            "候補を外した確認用下書きを表示しています。"
            "候補から手動で入れる店舗を選んでください。"
        )
    else:
        st.warning(
            "専務/顧問は自動では確定していません。"
            "人員不足や解なしに近い条件の確認用として、投入候補だけを表示しています。"
        )
    if candidates:
        st.dataframe(candidates, width="stretch", hide_index=True)
    if trigger_issues:
        with st.expander("候補表示の理由", expanded=False):
            st.dataframe(trigger_issues, width="stretch", hide_index=True)


def format_monthly_rule_condition(rule: dict) -> str:
    """月別配置ルールの条件を画面表示用に整える。"""
    comparison = str(rule.get("comparison") or "min").lower()
    count = int(rule.get("count") or 0)
    if comparison == "max":
        return f"{count}回以下"
    if comparison == "exact":
        return f"{count}回ちょうど"
    if comparison == "forbid":
        return "配置禁止"
    return f"{count}回以上"


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

    # ============================================================
    # 対象年月の同期
    # 月切替はページ遷移させず、Streamlit の session_state だけを更新する。
    # これにより、月ボタンを押してもログイン状態を保ったまま操作できる。
    # ============================================================
    def _parse_ym(value) -> Optional[tuple[int, int]]:
        """YYYY-MM 形式を安全にパースする。"""
        if not value:
            return None
        try:
            y_str, m_str = str(value).split("-", 1)
            parsed = (int(y_str), int(m_str))
            if 2024 <= parsed[0] <= 2030 and 1 <= parsed[1] <= 12:
                return parsed
        except Exception:
            pass
        return None

    def _is_selected_ym(y: int, m: int) -> bool:
        return (
            int(st.session_state.get("target_year", 0)) == int(y)
            and int(st.session_state.get("target_month", 0)) == int(m)
        )

    def _select_target_ym(y: int, m: int) -> None:
        """対象年月をセッション内で切り替える。"""
        st.session_state["target_year"] = int(y)
        st.session_state["target_month"] = int(m)
        # 任意年月の入力欄も、最後に選択した年月へ揃えておく
        st.session_state["custom_year_input"] = int(y)
        st.session_state["custom_month_input"] = int(m)

    def _button_type_for_ym(y: int, m: int) -> str:
        return "primary" if _is_selected_ym(y, m) else "secondary"

    def _apply_custom_target_ym() -> None:
        _select_target_ym(
            int(st.session_state.get("custom_year_input", next_month_year)),
            int(st.session_state.get("custom_month_input", next_month_month)),
        )

    # URL の ym は初回表示時だけ採用する。以後はボタン操作を優先する。
    if "target_year" not in st.session_state or "target_month" not in st.session_state:
        _url_ym = None
        try:
            _url_ym = _parse_ym(st.query_params.get("ym"))
        except Exception:
            _url_ym = None

        if _url_ym is not None:
            _select_target_ym(_url_ym[0], _url_ym[1])
        else:
            _select_target_ym(next_month_year, next_month_month)

    # クイック切替ボタン
    st.markdown("##### 📅 表示する対象月")
    qcol1, qcol2, qcol3, qcol4, qcol5 = st.columns([1, 1, 1, 1, 3])
    with qcol1:
        st.button(
            f"前月\n({prev_month_year}/{prev_month_month})",
            key="target_prev_month",
            type=_button_type_for_ym(prev_month_year, prev_month_month),
            width="stretch",
            on_click=_select_target_ym,
            args=(prev_month_year, prev_month_month),
        )
    with qcol2:
        st.button(
            f"今月\n({today.year}/{today.month})",
            key="target_this_month",
            type=_button_type_for_ym(today.year, today.month),
            width="stretch",
            on_click=_select_target_ym,
            args=(today.year, today.month),
        )
    with qcol3:
        # 翌月ボタン（デフォルト＝強調表示）
        st.button(
            f"📌 翌月\n({next_month_year}/{next_month_month})",
            key="target_next_month",
            type=_button_type_for_ym(next_month_year, next_month_month),
            width="stretch",
            help="通常はこちらを選択（提出締切は今月25日）",
            on_click=_select_target_ym,
            args=(next_month_year, next_month_month),
        )
    with qcol4:
        st.button(
            f"翌々月\n({nn_year}/{nn_month})",
            key="target_month_after_next",
            type=_button_type_for_ym(nn_year, nn_month),
            width="stretch",
            on_click=_select_target_ym,
            args=(nn_year, nn_month),
        )

    # 任意の年月を選択するための数値入力（折りたたみ式）
    with qcol5:
        with st.expander("🔧 任意の年月を選択", expanded=False):
            ec1, ec2 = st.columns(2)
            with ec1:
                custom_year = st.number_input(
                    "年", min_value=2024, max_value=2030,
                    key="custom_year_input",
                )
            with ec2:
                custom_month = st.number_input(
                    "月", min_value=1, max_value=12,
                    key="custom_month_input",
                )
            st.button(
                "この年月を表示",
                key="target_custom_month",
                type=_button_type_for_ym(int(custom_year), int(custom_month)),
                width="stretch",
                on_click=_apply_custom_target_ym,
            )

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
    # 直近のシフト生成結果（成功・失敗・例外を永続表示）
    # session_state に保存されているので、画面遷移しても消えない。
    # ============================================================
    _last_gen = st.session_state.get("last_gen_result")
    if _last_gen:
        _gen_status = _last_gen.get("status", "")
        _gen_ym = _last_gen.get("ym", "")
        _gen_finished = _last_gen.get("finished_at", "")[:19]
        with st.container():
            cols = st.columns([5, 1])
            with cols[0]:
                if _gen_status == "success":
                    st.success(
                        f"🟢 **直近の生成結果（{_gen_ym} / {_gen_finished}）: 成功**\n\n"
                        f"{_last_gen.get('message', '')}"
                    )
                elif _gen_status == "infeasible":
                    st.error(
                        f"🔴 **直近の生成結果（{_gen_ym} / {_gen_finished}）: 解なし**\n\n"
                        f"{_last_gen.get('message', '')}"
                    )
                elif _gen_status == "exception":
                    st.error(
                        f"🔴 **直近の生成結果（{_gen_ym} / {_gen_finished}）: エラー**\n\n"
                        f"`{_last_gen.get('error_type', '')}`: "
                        f"{_last_gen.get('error_msg', '')}"
                    )
                elif _gen_status == "running":
                    st.warning(
                        f"⏳ **{_gen_ym} の生成を実行中だった可能性があります** "
                        f"(開始 {_last_gen.get('started_at', '')[:19]}). "
                        "完了まで時間がかかった場合、サーバーが処理を打ち切った可能性があります。"
                    )
            with cols[1]:
                if st.button("結果を消す", key="clear_gen_result"):
                    st.session_state.pop("last_gen_result", None)
                    st.rerun()

            if "input_summary" in _last_gen:
                render_advisor_candidate_notice(_last_gen["input_summary"])

            # 入力データの詳細（成功・失敗どちらでも展開可能）
            if "input_summary" in _last_gen:
                _isum = _last_gen["input_summary"]
                with st.expander("📋 生成時の入力データ（クリックで展開）", expanded=(_gen_status != "success")):
                    st.write(
                        f"- 対象月: **{_gen_ym}** ({_isum.get('days_in_month', '?')}日)"
                    )
                    st.write(f"- 提出: {_isum.get('submission_count', 0)}名 "
                             f"／ 未提出: {_isum.get('pending_count', 0)}名")
                    st.write(
                        f"- 休み希望合計: **{_isum.get('total_off_days_requested', 0)}日**"
                    )
                    if _isum.get("off_requests_summary"):
                        st.write("- 各人の休み希望日数:")
                        for emp, cnt in _isum["off_requests_summary"].items():
                            days = _isum.get("off_requests_detail", {}).get(emp, [])
                            st.write(f"    - {emp}: {cnt}日 → {days}")
                    if _isum.get("holiday_overrides"):
                        st.write("- 月内目標休日数（有給込み）:")
                        for emp, n in _isum["holiday_overrides"].items():
                            req_off = _isum.get("off_requests_summary", {}).get(emp, 0)
                            warn = " ⚠ 休み希望が目標を超過" if req_off > n else ""
                            st.write(f"    - {emp}: {n}日{warn}")
                    if _isum.get("paid_leave_days"):
                        st.write(f"- 有給申請: {_isum['paid_leave_days']}")
                    if _isum.get("parsed_note_summaries"):
                        st.write(
                            f"- 自由記載から追加反映: "
                            f"{len(_isum['parsed_note_summaries'])}名分"
                        )
                    if _isum.get("requested_holiday_days"):
                        st.write(f"- 自由記載の休日日数指定: {_isum['requested_holiday_days']}")
                    if _isum.get("employee_max_consecutive_work"):
                        st.write(f"- 自由記載の連勤上限: {_isum['employee_max_consecutive_work']}")
                    if _isum.get("employee_max_consecutive_off"):
                        st.write(f"- 自由記載の連休上限: {_isum['employee_max_consecutive_off']}")
                    if _isum.get("preferred_consecutive_off"):
                        st.write(f"- 自由記載の連休希望: {_isum['preferred_consecutive_off']}")
                    if _isum.get("monthly_store_count_rules"):
                        st.write("- 月別ルール:")
                        for rule in _isum["monthly_store_count_rules"]:
                            st.write(f"    - {monthly_rule_display_text(rule)}")
                    render_advisor_candidate_notice(_isum)
                    st.write(f"- 出勤希望（希望扱い）: {_isum.get('work_requests_count', 0)}件")
                    st.write(
                        f"- 自由記載の出勤希望（優先反映）: "
                        f"{_isum.get('preferred_work_requests_count', 0)}件"
                    )
                    st.write(
                        f"- 自由記載の選択式出勤希望: "
                        f"{_isum.get('preferred_work_groups_count', 0)}件"
                    )
                    st.write(f"- 柔軟休み: {_isum.get('flexible_off_count', 0)}件")
            if _last_gen.get("error_detail"):
                with st.expander("🔧 技術者向け: 例外スタックトレース", expanded=False):
                    st.code(_last_gen["error_detail"])

    # ============================================================
    # 希望シフト提出状況（リアルタイム）
    # ============================================================
    expected_employees = shift_submission_employee_names()
    submission_status = backup_mgr.get_submission_status(
        int(target_year), int(target_month), expected_employees,
    )
    submission_status = enrich_submission_days_from_files(
        backup_mgr, int(target_year), int(target_month), submission_status,
    )
    summary = submission_status["summary"]
    current_ym_label = f"{int(target_year):04d}-{int(target_month):02d}"
    available_preference_months: list[str] = []
    try:
        available_preference_months = [
            p.name
            for p in sorted(backup_mgr.backup_dir.iterdir())
            if p.is_dir() and any(p.glob("preferences_*.json"))
        ]
    except Exception:
        available_preference_months = []

    # 診断: 各月に保存されている提出ファイル数（デバッグ表示）
    with st.expander("🔧 診断: 各月の保存データ件数（クリックで展開）", expanded=False):
        try:
            from prototype.data_export import get_all_data_summary
            _ds = get_all_data_summary()
            st.write(f"全提出件数: {_ds['submissions_total']} 件")
            if _ds.get("submissions_by_month"):
                st.write("月別:")
                for ym, cnt in sorted(_ds["submissions_by_month"].items(), reverse=True):
                    marker = " ← 表示中" if ym == f"{int(target_year):04d}-{int(target_month):02d}" else ""
                    st.write(f"  - {ym}: {cnt} 件{marker}")
            else:
                st.caption(
                    "⚠ 保存されている提出データがありません。"
                    "Streamlit Cloud のサーバー再起動でデータが消えた可能性があります。"
                    "GitHub 自動バックアップを設定済みであれば、別途復旧可能です。"
                )
            st.caption(
                f"現在の表示対象: **{int(target_year)}年{int(target_month)}月** "
                f"(target_year={st.session_state.get('target_year')}, "
                f"target_month={st.session_state.get('target_month')})"
            )
        except Exception as _e:
            st.caption(f"診断情報取得失敗: {_e}")

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
            f'<br>💾 <strong>「⚙️ 設定」→「💾 バックアップ」</strong>から、提出データをダウンロードしてPCに保存することをお勧めします。'
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
            f'<br>💡 提出データがない状態で「シフトを自動生成」を押すと、サンプルデータで生成します（テスト用）。'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        other_months = [
            ym for ym in available_preference_months if ym != current_ym_label
        ]
        if other_months:
            st.info(
                "保存済みの希望提出データは "
                + "、".join(other_months)
                + " にあります。現在の表示対象は "
                + current_ym_label
                + " です。過去月を確認する場合は、上部の対象年月を切り替えてください。"
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
            f'⏳ 未提出 {summary["total_pending"]}名: <strong>{", ".join(submission_status["not_submitted"])}</strong>'
            f'<br>📝 原則として全員提出後に生成してください。急ぎの場合のみ、操作欄で確認して生成できます。'
            f'<br>📝 長期欠勤の場合は「⚙️ 設定 → 👥 従業員マスタ」で雇用形態を「休職中」に変更してください（シフト対象外になります）。'
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
                        "× 休み希望（絶対）": format_day_list(s.get("off_request_days", [])),
                        "△ できれば休み": format_day_list(s.get("flexible_off_days", [])),
                        "有給": f"{s.get('paid_leave_days', 0)}日",
                        "備考": "📝 あり" if s["has_note"] else "",
                    })
                st.dataframe(submitted_data, width="stretch", hide_index=True)

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

    with st.expander("🧾 本人提出希望の一覧（調整時の確認用）", expanded=summary["total_submitted"] > 0):
        st.caption(
            "×は本人が提出した絶対休みです。シフト作成・AI対話・手動調整でも勤務にしない前提で扱います。"
        )
        submitted_by_name = {
            s["employee"]: s for s in submission_status["submitted"]
        }
        admin_leave_by_employee = admin_paid_leave_days_for_month(
            int(target_year), int(target_month),
        )
        monthly_custom_by_employee: dict[str, list[str]] = {}
        for rule in active_monthly_custom_rules(rule_cfg, int(target_year), int(target_month)):
            employee = getattr(rule, "employee", "") or ""
            if not employee:
                continue
            if getattr(rule, "rule_type", "note") == "employee_store_count":
                label = monthly_rule_display_text({
                    "name": rule.name,
                    "employee": rule.employee,
                    "stores": list(rule.stores),
                    "count": rule.count,
                    "comparison": getattr(rule, "comparison", "min"),
                })
            else:
                label = f"月別メモ: {rule.name}"
            monthly_custom_by_employee.setdefault(employee, []).append(label)

        request_rows = []
        for emp_name in expected_employees:
            s = submitted_by_name.get(emp_name)
            if s:
                submitted_leave = int(s.get("paid_leave_days", 0) or 0)
                admin_leave = int(admin_leave_by_employee.get(emp_name, 0) or 0)
                leave_label = f"{submitted_leave + admin_leave}日"
                if admin_leave:
                    leave_label += f"（管理者+{admin_leave}日）"
                note_applied = []
                if s.get("requested_holiday_days"):
                    note_applied.append(f"休み計{s['requested_holiday_days']}日")
                if s.get("max_consecutive_work_days"):
                    note_applied.append(f"{s['max_consecutive_work_days']}連勤まで")
                if s.get("max_consecutive_off_days"):
                    note_applied.append(f"{s['max_consecutive_off_days']}連休まで")
                if s.get("preferred_consecutive_off_days"):
                    note_applied.append(f"{s['preferred_consecutive_off_days']}連休を優先")
                note_applied.extend(s.get("work_request_group_labels", []))
                note_applied.extend(monthly_custom_by_employee.get(emp_name, []))
                request_rows.append({
                    "氏名": emp_name,
                    "状態": "提出済み",
                    "× 休み希望（絶対）": format_day_list(s.get("off_request_days", [])),
                    "△ できれば休み": format_day_list(s.get("flexible_off_days", [])),
                    "出勤希望": format_day_list(s.get("work_request_days", [])),
                    "有給": leave_label,
                    "自由記載から反映": " / ".join(note_applied),
                    "備考": s.get("note", "") or s.get("note_excerpt", ""),
                })
            else:
                admin_leave = int(admin_leave_by_employee.get(emp_name, 0) or 0)
                request_rows.append({
                    "氏名": emp_name,
                    "状態": "未提出",
                    "× 休み希望（絶対）": "",
                    "△ できれば休み": "",
                    "出勤希望": "",
                    "有給": f"管理者+{admin_leave}日" if admin_leave else "",
                    "自由記載から反映": " / ".join(monthly_custom_by_employee.get(emp_name, [])),
                    "備考": "",
                })
        render_scrollable_request_table(request_rows)

    st.markdown("---")

    with st.expander("📌 今月だけの特別ルール", expanded=False):
        current_month_rules = active_monthly_store_count_rules(
            rule_cfg, int(target_year), int(target_month),
        )
        current_custom_rules = active_monthly_custom_rules(
            rule_cfg, int(target_year), int(target_month),
        )
        st.caption(
            "基本ルールは「設定 → ルール設定」と従業員マスタで管理しています。"
            "ここには、その月だけの例外・研修・一時的な配置条件だけを表示します。"
        )

        display_rows = []
        for rule in current_month_rules:
            display_rows.append({
                "種類": "システム月別例外" if rule.get("source") == "system" else "月別配置",
                "内容": monthly_rule_display_text(rule),
                "重要度": rule.get("severity", "WARNING"),
            })
        for rule in current_custom_rules:
            if getattr(rule, "rule_type", "note") == "employee_store_count":
                continue
            display_rows.append({
                "種類": "月別メモ",
                "内容": f"{rule.name}: {rule.description}",
                "重要度": rule.severity,
            })
        if display_rows:
            st.dataframe(display_rows, width="stretch", hide_index=True)
        else:
            st.caption("この月だけの追加ルールは未設定です。")

        with st.form(
            f"quick_monthly_rule_form_{int(target_year)}_{int(target_month)}",
            clear_on_submit=True,
        ):
            st.markdown("##### 特別ルールを追加")
            rule_mode = st.radio(
                "反映方法",
                ["メモとして残す", "生成にも反映する"],
                horizontal=True,
                help=(
                    "生成にも反映する場合、スタッフ・店舗・回数の条件をシフト計算に渡します。"
                    "文章の細かい条件は備考として残ります。"
                ),
            )
            quick_rule_name = st.text_input(
                "見出し",
                placeholder="例: 牧野さんの西口研修",
            )
            quick_rule_desc = st.text_area(
                "内容",
                placeholder="例: 牧野さんを研修のため、楯さんが西口勤務の時に一緒に3回だけ入れる",
                height=80,
            )
            employee_options = ["指定なし"] + shift_submission_employee_names()
            quick_employee = st.selectbox("対象スタッフ", employee_options)
            quick_stores = []
            quick_count = 0
            quick_comparison = "min"
            if rule_mode == "生成にも反映する":
                store_options = [s.name for s in Store if s != Store.OFF]
                store_labels = {s.name: s.display_name for s in Store if s != Store.OFF}
                quick_stores = st.multiselect(
                    "対象店舗",
                    options=store_options,
                    format_func=lambda name: store_labels.get(name, name),
                )
                comparison_label = st.selectbox(
                    "条件",
                    ["最低回数", "最大回数", "ちょうど回数", "配置禁止"],
                )
                quick_comparison = {
                    "最低回数": "min",
                    "最大回数": "max",
                    "ちょうど回数": "exact",
                    "配置禁止": "forbid",
                }[comparison_label]
                quick_count = int(st.number_input(
                    "月内回数",
                    min_value=0 if quick_comparison == "forbid" else 1,
                    max_value=31,
                    value=0 if quick_comparison == "forbid" else 3,
                    disabled=quick_comparison == "forbid",
                ))
            quick_severity = st.selectbox(
                "重要度",
                ["WARNING", "ERROR"],
                help="ERRORは必ず守る条件、WARNINGはできるだけ守る条件です。",
            )
            submitted_quick_rule = st.form_submit_button("この月の特別ルールに追加")
            if submitted_quick_rule:
                is_structured = rule_mode == "生成にも反映する"
                missing_structured = (
                    is_structured
                    and (
                        quick_employee == "指定なし"
                        or not quick_stores
                        or (quick_comparison != "forbid" and quick_count <= 0)
                    )
                )
                if not quick_rule_name or not quick_rule_desc:
                    st.error("見出しと内容を入力してください。")
                elif missing_structured:
                    st.error("生成にも反映する場合は、スタッフ・店舗・回数を入力してください。")
                else:
                    new_rule = CustomRule(
                        id=f"monthly_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                        name=quick_rule_name,
                        description=quick_rule_desc,
                        enabled=True,
                        severity=quick_severity,
                        created_at=datetime.now().isoformat(),
                        created_by="管理者",
                        target_year=int(target_year),
                        target_month=int(target_month),
                        rule_type="employee_store_count" if is_structured else "note",
                        employee="" if quick_employee == "指定なし" else quick_employee,
                        stores=quick_stores,
                        count=0 if quick_comparison == "forbid" else quick_count,
                        comparison=quick_comparison,
                    )
                    next_cfg = RuleConfig(
                        enabled_checks=dict(rule_cfg.enabled_checks),
                        parameters=dict(rule_cfg.parameters),
                        custom_rules=list(rule_cfg.custom_rules) + [new_rule],
                    )
                    rule_mgr.save(
                        next_cfg,
                        actor="管理者",
                        note=f"{int(target_year)}年{int(target_month)}月の特別ルール追加",
                    )
                    st.success("この月の特別ルールに追加しました。")
                    st.rerun()

    # 操作ボタン群
    st.markdown("##### 操作")
    allow_partial_generation = summary["total_pending"] == 0
    if summary["total_pending"] > 0:
        st.warning(
            f"未提出者が {summary['total_pending']}名います。"
            "本人の×希望を守るため、原則は全員提出後に生成してください。"
        )
        allow_partial_generation = st.checkbox(
            "未提出者を希望未指定として生成する（緊急時のみ）",
            key=f"allow_partial_generation_{int(target_year)}_{int(target_month)}",
        )
    bcol1, bcol2, bcol3, bcol4 = st.columns(4)

    with bcol1:
        # 生成ボタン: ロック中は無効、未提出者がいれば警告
        gen_disabled = lock_info is not None or not allow_partial_generation
        if gen_disabled:
            if lock_info is not None:
                gen_help = "ロック解除してから再生成してください"
            else:
                gen_help = "未提出者がいるため、緊急時の確認チェックを入れるまで生成できません"
        elif summary["total_pending"] > 0:
            gen_help = f"⚠ {summary['total_pending']}名 未提出です。それでも生成しますか？"
        else:
            gen_help = "シフト計算エンジンが希望データから新規シフトを作成します"

        # 一部提出でも生成可能（未提出者は自由配置）
        gen_button_label = "🔄 シフトを自動生成"
        if summary["total_pending"] > 0 and not gen_disabled:
            gen_button_label = f"🔄 シフトを自動生成（提出 {summary['total_submitted']}/{summary['total_expected']}名）"

        if st.button(
            gen_button_label,
            type="primary",
            width="stretch",
            disabled=gen_disabled,
            help=gen_help,
        ):
            # ========================================================
            # シフト生成（例外を必ずキャッチしてユーザーに表示する）
            # 生成前に target_year/month を保存（生成中のリセットに対する保険）
            # URL にも書き込んで、セッションが切れても月情報が残るようにする
            # ========================================================
            _saved_target_year = int(target_year)
            _saved_target_month = int(target_month)
            st.session_state["target_year"] = _saved_target_year
            st.session_state["target_month"] = _saved_target_month

            # 結果を session_state に永続化するためのキー
            _gen_result_key = "last_gen_result"
            st.session_state[_gen_result_key] = {
                "status": "running",
                "ym": f"{_saved_target_year:04d}-{_saved_target_month:02d}",
                "started_at": datetime.now().isoformat(),
            }

            # ステップ進捗の状態表示エリア
            progress_area = st.empty()
            try:
                from calendar import monthrange as _mr
                days_in_m = _mr(_saved_target_year, _saved_target_month)[1]
                solver_limit_seconds = max(
                    180,
                    int(rule_cfg.parameters.get("solver_time_limit_seconds", 180)),
                )

                progress_area.info(
                    f"⏳ ステップ 1/4: {_saved_target_year}年{_saved_target_month}月の提出データを読み込み中..."
                )
                with st.spinner(f"シフト案を生成中... (最大{solver_limit_seconds}秒)"):
                    # 実際の提出データを読み込む
                    from prototype.submission_loader import load_submissions_for_month
                    sub_data = load_submissions_for_month(
                        _saved_target_year, _saved_target_month, expected_employees,
                    )
                    progress_area.info(
                        f"⏳ ステップ 2/4: 提出データを処理中... "
                        f"（提出 {sub_data.submission_count}名 / 未提出 {len(sub_data.pending_employees)}名）"
                    )

                    # ヘルパー関数: 月内の有効日のみに絞り込む
                    def _filter_valid_days(days_list, max_day):
                        return [d for d in days_list if 1 <= d <= max_day]

                    # 5月2026年（テストデータ用）かどうかで分岐
                    is_test_may_2026 = (
                        _saved_target_year == 2026 and _saved_target_month == 5
                        and sub_data.submission_count == 0
                    )
                    # 提出があればそれを使い、なければテストデータにフォールバック
                    if sub_data.submission_count > 0:
                        # 実データで生成（任意の月で動く）
                        # 不正な日（その月に存在しない日）をフィルタリング
                        use_off_requests = {
                            emp: _filter_valid_days(days, days_in_m)
                            for emp, days in sub_data.off_requests.items()
                        }
                        use_off_requests = {
                            emp: days for emp, days in use_off_requests.items() if days
                        }
                        use_work_requests = [
                            (emp, d, store) for (emp, d, store) in sub_data.work_requests
                            if 1 <= d <= days_in_m
                            and d not in set(use_off_requests.get(emp, []))
                        ]
                        use_preferred_work_requests = [
                            (emp, d, store)
                            for (emp, d, store) in getattr(sub_data, "preferred_work_requests", [])
                            if 1 <= d <= days_in_m
                            and d not in set(use_off_requests.get(emp, []))
                        ]
                        use_preferred_work_groups = []
                        for emp, candidate_days, required_count, store in getattr(
                            sub_data, "preferred_work_groups", []
                        ):
                            filtered_candidates = [
                                int(d) for d in candidate_days
                                if 1 <= int(d) <= days_in_m
                                and int(d) not in set(use_off_requests.get(emp, []))
                            ]
                            if filtered_candidates:
                                use_preferred_work_groups.append((
                                    emp,
                                    sorted(set(filtered_candidates)),
                                    min(int(required_count), len(set(filtered_candidates))),
                                    store,
                                ))
                        use_flexible_off = []
                        for fo in sub_data.flexible_off:
                            if isinstance(fo, tuple) and len(fo) >= 3:
                                emp, cands, n = fo[0], fo[1], fo[2]
                                cands = _filter_valid_days(cands, days_in_m)
                                if cands:
                                    use_flexible_off.append((emp, cands, n))
                        use_holiday_overrides = {}
                        use_exact_holiday_days = {}
                        use_employee_max_consecutive_work = dict(
                            getattr(sub_data, "max_consecutive_work_days", {})
                        )
                        use_employee_max_consecutive_off = dict(
                            getattr(sub_data, "max_consecutive_off_days", {})
                        )
                        use_preferred_consecutive_off = list(
                            getattr(sub_data, "preferred_consecutive_off", [])
                        )
                        effective_paid_leave_days = combined_paid_leave_days(
                            sub_data.paid_leave_days,
                            _saved_target_year,
                            _saved_target_month,
                        )
                        # 有給日数を holiday_overrides に反映（基準＋有給日数）
                        for emp_name, paid_days in effective_paid_leave_days.items():
                            try:
                                from prototype.employees import get_employee
                                emp = get_employee(emp_name)
                                base_target = get_monthly_work_target(
                                    emp.name,
                                    _saved_target_month,
                                    emp.annual_target_days,
                                )
                                if base_target:
                                    base_holidays = days_in_m - base_target
                                    use_holiday_overrides[emp_name] = base_holidays + paid_days
                            except Exception:
                                pass
                        for emp_name, requested_days in getattr(
                            sub_data, "requested_holiday_days", {}
                        ).items():
                            if 0 <= int(requested_days) <= days_in_m:
                                use_holiday_overrides[emp_name] = max(
                                    int(use_holiday_overrides.get(emp_name, 0) or 0),
                                    int(requested_days),
                                )
                                use_exact_holiday_days[emp_name] = int(requested_days)
                        # 実データ使用時は前月持ち越し・特例なし（過去状態が不明）
                        use_prev_month = []
                        use_consec_exceptions = []
                        data_source_msg = (
                            f"📥 **実際の提出データ {sub_data.submission_count}名分**を使用して生成しました"
                        )
                        parsed_note_summaries = getattr(sub_data, "parsed_note_summaries", {})
                        if parsed_note_summaries:
                            data_source_msg += (
                                f"\n自由記載から "
                                f"{len(parsed_note_summaries)}名分の希望も反映しました。"
                            )
                        if sub_data.pending_employees:
                            data_source_msg += (
                                f"\n（未提出 {len(sub_data.pending_employees)}名: "
                                f"{', '.join(sub_data.pending_employees[:5])}"
                                f"{'...' if len(sub_data.pending_employees) > 5 else ''}"
                                    "は希望未指定として自由配置）"
                                )
                        admin_leave_days = admin_paid_leave_days_for_month(
                            _saved_target_year, _saved_target_month,
                        )
                        if admin_leave_days:
                            data_source_msg += (
                                "\n管理者が追加した有給調整も集計に含めました。"
                            )
                    elif is_test_may_2026:
                        # 2026年5月のテストデータ（PREVIOUS_MONTH_CARRYOVER は5月用）
                        use_off_requests = OFF_REQUESTS
                        use_work_requests = WORK_REQUESTS
                        use_preferred_work_requests = []
                        use_preferred_work_groups = []
                        use_flexible_off = FLEXIBLE_OFF_REQUESTS
                        use_holiday_overrides = MAY_2026_HOLIDAY_OVERRIDES
                        use_exact_holiday_days = {}
                        use_employee_max_consecutive_work = {}
                        use_employee_max_consecutive_off = {}
                        use_preferred_consecutive_off = []
                        use_prev_month = PREVIOUS_MONTH_CARRYOVER
                        use_consec_exceptions = ["野澤"]
                        data_source_msg = (
                            "💡 提出データがないため、2026年5月のサンプルテストデータで生成しました。"
                        )
                    else:
                        # 提出ゼロ + 5月以外: 制約なしで生成（誰でも自由配置）
                        use_off_requests = {}
                        use_work_requests = []
                        use_preferred_work_requests = []
                        use_preferred_work_groups = []
                        use_flexible_off = []
                        use_holiday_overrides = {}
                        use_exact_holiday_days = {}
                        use_employee_max_consecutive_work = {}
                        use_employee_max_consecutive_off = {}
                        use_preferred_consecutive_off = []
                        use_prev_month = []
                        use_consec_exceptions = []
                        data_source_msg = (
                            f"💡 提出データがないため、希望なしで "
                            f"{_saved_target_year}年{_saved_target_month}月のシフトを生成しました。"
                            "本番では従業員から希望が届いた後に生成してください。"
                        )

                    progress_area.info(
                        f"⏳ ステップ 3/4: 営業モードを判定中..."
                    )
                    modes = determine_operation_modes(_saved_target_year, _saved_target_month)
                    use_monthly_store_count_rules = active_monthly_store_count_rules(
                        rule_cfg, _saved_target_year, _saved_target_month,
                    )
                    for _emp, _day, _store in system_monthly_preferred_work_requests(
                        _saved_target_year, _saved_target_month,
                    ):
                        if _day in set(use_off_requests.get(_emp, [])):
                            continue
                        _item = (_emp, int(_day), _store)
                        if _item not in use_preferred_work_requests:
                            use_preferred_work_requests.append(_item)

                    progress_area.info(
                        f"⏳ ステップ 4/4: シフト計算エンジン実行中... "
                        f"(最大{solver_limit_seconds}秒)"
                    )
                    generation_kwargs = {
                        "year": _saved_target_year,
                        "month": _saved_target_month,
                        "off_requests": use_off_requests,
                        "work_requests": use_work_requests,
                        "prev_month": use_prev_month,
                        "flexible_off": use_flexible_off,
                        "holiday_overrides": use_holiday_overrides,
                        "operation_modes": modes,
                        "consec_exceptions": use_consec_exceptions,
                        "default_holidays": rule_cfg.parameters.get("default_holiday_days", 8),
                        "max_consec_override": rule_cfg.parameters.get("max_consec_work", 5),
                        "time_limit_seconds": solver_limit_seconds,
                        "random_seed": rule_cfg.parameters.get("solver_seed", 42),
                        "verbose": False,
                    }
                    generator_params = inspect.signature(generate_shift).parameters
                    if "preferred_work_requests" in generator_params:
                        generation_kwargs["preferred_work_requests"] = use_preferred_work_requests
                    if "preferred_work_groups" in generator_params:
                        generation_kwargs["preferred_work_groups"] = use_preferred_work_groups
                    if "preferred_consecutive_off" in generator_params:
                        generation_kwargs["preferred_consecutive_off"] = use_preferred_consecutive_off
                    if "exact_holiday_days" in generator_params:
                        generation_kwargs["exact_holiday_days"] = use_exact_holiday_days
                    if "employee_max_consecutive_work" in generator_params:
                        generation_kwargs["employee_max_consecutive_work"] = use_employee_max_consecutive_work
                    if "employee_max_consecutive_off" in generator_params:
                        generation_kwargs["employee_max_consecutive_off"] = use_employee_max_consecutive_off
                    if "monthly_store_count_rules" in generator_params:
                        generation_kwargs["monthly_store_count_rules"] = use_monthly_store_count_rules
                    if "strict_warning_constraints" in generator_params:
                        generation_kwargs["strict_warning_constraints"] = True
                    if "advisor_max_days" in generator_params:
                        # 顧問は自動では確定しない。必要時だけ候補試算として別途確認する。
                        generation_kwargs["advisor_max_days"] = 0
                        data_source_msg += (
                            "\n顧問は自動配置せず、必要な場合だけ候補として表示します。"
                        )
                    shift = generate_shift(**generation_kwargs)
                    relaxed_warning_constraints = False
                    relaxed_advisor_limit = False
                    if shift is None and "strict_warning_constraints" in generator_params:
                        progress_area.info(
                            "⏳ 厳しめ条件では解が見つからなかったため、"
                            "警告候補だけを緩めて再探索しています..."
                        )
                        relaxed_kwargs = dict(generation_kwargs)
                        relaxed_kwargs["strict_warning_constraints"] = False
                        shift = generate_shift(**relaxed_kwargs)
                        relaxed_warning_constraints = shift is not None
                    if relaxed_warning_constraints:
                        data_source_msg += (
                            "\n※警告が出ない条件では解が見つからなかったため、"
                            "一部の警告条件だけ緩めて生成しました。"
                        )

                    advisor_candidates = []
                    advisor_candidate_triggers = []
                    advisor_candidate_base_used = False
                    emergency_draft_used = False
                    if shift is not None:
                        candidate_validation = run_shift_validation(
                            shift=shift,
                            work_requests=list(use_work_requests) + list(use_preferred_work_requests),
                            off_requests=use_off_requests,
                            prev_month=use_prev_month,
                            holiday_overrides=use_holiday_overrides,
                            exact_holiday_days=use_exact_holiday_days,
                            employee_max_consecutive_work=use_employee_max_consecutive_work,
                            employee_max_consecutive_off=use_employee_max_consecutive_off,
                            default_holidays=rule_cfg.parameters.get("default_holiday_days", 8),
                            max_consec=rule_cfg.parameters.get("max_consec_work", 5),
                            monthly_store_count_rules=use_monthly_store_count_rules,
                        )
                        advisor_candidate_triggers = advisor_candidate_trigger_issues(
                            candidate_validation
                        )

                    if shift is None or advisor_candidate_triggers:
                        advisor_probe_kwargs = dict(generation_kwargs)
                        if "advisor_max_days" in generator_params:
                            advisor_probe_kwargs["advisor_max_days"] = advisor_candidate_limit(
                                _saved_target_year, _saved_target_month,
                            )
                        advisor_probe_shift = generate_shift(**advisor_probe_kwargs)
                        if (
                            advisor_probe_shift is None
                            and "strict_warning_constraints" in advisor_probe_kwargs
                        ):
                            relaxed_probe_kwargs = dict(advisor_probe_kwargs)
                            relaxed_probe_kwargs["strict_warning_constraints"] = False
                            advisor_probe_shift = generate_shift(**relaxed_probe_kwargs)
                        advisor_candidates = advisor_candidate_rows_from_shift(
                            advisor_probe_shift
                        )
                        if shift is None and not advisor_candidate_triggers:
                            advisor_candidate_triggers = [{
                                "日付": "",
                                "区分": "解なし",
                                "内容": "顧問なしでは条件を満たすシフトを見つけられませんでした。",
                            }]

                        if shift is None and advisor_probe_shift is not None:
                            shift = strip_advisor_assignments(advisor_probe_shift)
                            advisor_candidate_base_used = True
                            data_source_msg += (
                                "\n顧問なしでは解が見つからなかったため、"
                                "顧問候補を外した確認用下書きを表示します。"
                                "候補を手動で入れるか、条件を調整してください。"
                            )

                    if shift is None and "advisor_max_days" in generator_params:
                        emergency_limit = emergency_advisor_candidate_limit(
                            _saved_target_year, _saved_target_month,
                        )
                        if emergency_limit > advisor_candidate_limit(
                            _saved_target_year, _saved_target_month,
                        ):
                            progress_area.info(
                                "⏳ 通常の候補枠でも下書きが出ないため、"
                                "緊急用の仮下書きを作成しています..."
                            )
                            emergency_probe_kwargs = dict(generation_kwargs)
                            emergency_probe_kwargs["advisor_max_days"] = emergency_limit
                            if "strict_warning_constraints" in emergency_probe_kwargs:
                                emergency_probe_kwargs["strict_warning_constraints"] = False
                            emergency_probe_shift = generate_shift(**emergency_probe_kwargs)
                            if emergency_probe_shift is not None:
                                shift = strip_advisor_assignments(emergency_probe_shift)
                                advisor_candidates = advisor_candidate_rows_from_shift(
                                    emergency_probe_shift
                                )
                                advisor_candidate_base_used = True
                                emergency_draft_used = True
                                if not advisor_candidate_triggers:
                                    advisor_candidate_triggers = [{
                                        "日付": "",
                                        "区分": "緊急下書き",
                                        "内容": (
                                            "通常の候補枠では下書きが出ないため、"
                                            "顧問候補枠を一時的に広げて仮案を作成しました。"
                                        ),
                                    }]
                                data_source_msg += (
                                    "\n通常の候補枠でも解が見つからなかったため、"
                                    "緊急用に顧問候補枠を広げた確認用下書きを表示します。"
                                    "顧問は自動確定していません。エラーを見ながら手動で調整してください。"
                                )

                # session_state を再度確実にセット（生成中にリセットされた場合の保険）
                st.session_state["target_year"] = _saved_target_year
                st.session_state["target_month"] = _saved_target_month

                # 入力データのサマリ（成功・失敗どちらでも残す）
                _input_summary = {
                    "submission_count": int(sub_data.submission_count),
                    "pending_count": int(len(sub_data.pending_employees)),
                    "submitted_employees": list(sub_data.submitted_employees),
                    "off_requests_summary": {
                        emp: len(days) for emp, days in use_off_requests.items()
                    },
                    "off_requests_detail": {
                        emp: list(days) for emp, days in use_off_requests.items()
                    },
                    "work_requests_count": len(use_work_requests),
                    "preferred_work_requests_count": len(use_preferred_work_requests),
                    "preferred_work_groups_count": len(use_preferred_work_groups),
                    "flexible_off_count": len(use_flexible_off),
                    "holiday_overrides": dict(use_holiday_overrides),
                    "paid_leave_days": dict(
                        combined_paid_leave_days(
                            sub_data.paid_leave_days,
                            _saved_target_year,
                            _saved_target_month,
                        )
                    ),
                    "submitted_paid_leave_days": dict(sub_data.paid_leave_days),
                    "admin_paid_leave_days": dict(
                        admin_paid_leave_days_for_month(
                            _saved_target_year,
                            _saved_target_month,
                        )
                    ),
                    "requested_holiday_days": dict(
                        getattr(sub_data, "requested_holiday_days", {})
                    ),
                    "exact_holiday_days": dict(use_exact_holiday_days),
                    "employee_max_consecutive_work": dict(use_employee_max_consecutive_work),
                    "employee_max_consecutive_off": dict(use_employee_max_consecutive_off),
                    "preferred_consecutive_off": list(use_preferred_consecutive_off),
                    "monthly_store_count_rules": list(use_monthly_store_count_rules),
                    "relaxed_warning_constraints": bool(relaxed_warning_constraints),
                    "relaxed_advisor_limit": bool(relaxed_advisor_limit),
                    "advisor_auto_assignment": False,
                    "advisor_candidate_limit": advisor_candidate_limit(
                        _saved_target_year, _saved_target_month,
                    ),
                    "advisor_candidates": list(advisor_candidates),
                    "advisor_candidate_triggers": list(advisor_candidate_triggers),
                    "advisor_candidate_base_used": bool(advisor_candidate_base_used),
                    "emergency_draft_used": bool(emergency_draft_used),
                    "solver_limit_seconds": int(solver_limit_seconds),
                    "parsed_note_summaries": dict(
                        getattr(sub_data, "parsed_note_summaries", {})
                    ),
                    "days_in_month": days_in_m,
                    "total_off_days_requested": sum(
                        len(v) for v in use_off_requests.values()
                    ),
                }

                if shift is not None:
                    save_session_shift(shift)
                    try:
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            shift,
                            kind="draft",
                            author="自動保存",
                            note="シフト生成直後の下書き",
                        )
                    except Exception:
                        pass
                    # 検証で使うために、実際に使った制約も保存
                    save_validation_context({
                        "ym": f"{_saved_target_year:04d}-{_saved_target_month:02d}",
                        "off_requests": dict(use_off_requests),
                        "work_requests": list(use_work_requests),
                        "preferred_work_requests": list(use_preferred_work_requests),
                        "preferred_work_groups": list(use_preferred_work_groups),
                        "prev_month": list(use_prev_month),
                        "holiday_overrides": dict(use_holiday_overrides),
                        "exact_holiday_days": dict(use_exact_holiday_days),
                        "employee_max_consecutive_work": dict(use_employee_max_consecutive_work),
                        "employee_max_consecutive_off": dict(use_employee_max_consecutive_off),
                        "monthly_store_count_rules": list(use_monthly_store_count_rules),
                    })
                    progress_area.empty()
                    st.success(f"✅ シフト生成完了！\n\n{data_source_msg}")
                    st.session_state[_gen_result_key] = {
                        "status": "success",
                        "ym": f"{_saved_target_year:04d}-{_saved_target_month:02d}",
                        "message": data_source_msg,
                        "input_summary": _input_summary,
                        "finished_at": datetime.now().isoformat(),
                    }
                else:
                    # ソルバーが解を見つけられなかった場合
                    progress_area.empty()
                    diag_lines = [
                        "❌ **シフトを生成できませんでした**",
                        "",
                        f"ソルバーが {solver_limit_seconds}秒以内に "
                        "制約を全て満たすシフトを見つけられませんでした。",
                        "",
                        "**考えられる原因:**",
                    ]
                    if sub_data.submission_count > 0:
                        for emp_name, off_days in use_off_requests.items():
                            if len(off_days) > days_in_m - 5:
                                diag_lines.append(
                                    f"- ⚠ **{emp_name}** の休み希望が {len(off_days)}日と多すぎる可能性"
                                )
                        # 有給日数による holiday_overrides の矛盾チェック
                        for emp_name, req_holidays in use_holiday_overrides.items():
                            req_off_count = len(use_off_requests.get(emp_name, []))
                            if req_off_count > req_holidays:
                                diag_lines.append(
                                    f"- ⚠ **{emp_name}** は休み希望 {req_off_count}日に対し、"
                                    f"有給込み目標休日 {req_holidays}日 → 矛盾の可能性"
                                )
                        # 総休日数チェック
                        total_off = sum(len(v) for v in use_off_requests.values())
                        diag_lines.append(
                            f"- 提出された休み希望の合計: {total_off}日"
                        )
                    else:
                        diag_lines.append("- 提出データなしで生成しようとした可能性")
                    diag_lines.extend([
                        "",
                        "**対処方法:**",
                        "1. 下の「📋 生成時の入力データ」で実際に渡された希望を確認",
                        "2. 矛盾する希望があれば該当従業員に再提出を依頼",
                        "3. 「⚙️ 設定 → 🔧 ルール設定」で",
                        "   - 「最大連勤日数」を 5→6 や 7 に増やす",
                        "   - 「ソルバー最大実行時間」を 120→180 秒に増やす",
                        "4. 長期欠勤者は「⚙️ 設定 → 👥 従業員マスタ」で「休職中」に変更",
                    ])
                    st.error("\n".join(diag_lines))
                    st.session_state[_gen_result_key] = {
                        "status": "infeasible",
                        "ym": f"{_saved_target_year:04d}-{_saved_target_month:02d}",
                        "message": "\n".join(diag_lines),
                        "input_summary": _input_summary,
                        "finished_at": datetime.now().isoformat(),
                    }
            except Exception as _gen_err:
                # 想定外の例外（KeyError など）も画面に表示
                progress_area.empty()
                import traceback
                error_detail = traceback.format_exc()
                st.error(
                    f"❌ **シフト生成中にエラーが発生しました**\n\n"
                    f"エラー種別: `{type(_gen_err).__name__}`\n\n"
                    f"エラー内容: `{str(_gen_err)}`\n\n"
                    f"**よくある原因:**\n"
                    f"- 提出データに不正な日付が含まれている（例: 4月に31日が入っている）\n"
                    f"- 提出データの形式が古い・破損している\n"
                    f"- システム内部の不具合\n\n"
                    f"**対処方法:** 該当従業員に再提出を依頼するか、技術者にお問い合わせください。"
                )
                with st.expander("🔧 技術者向け: 詳細エラーログ", expanded=False):
                    st.code(error_detail)
                st.session_state[_gen_result_key] = {
                    "status": "exception",
                    "ym": f"{_saved_target_year:04d}-{_saved_target_month:02d}",
                    "error_type": type(_gen_err).__name__,
                    "error_msg": str(_gen_err),
                    "error_detail": error_detail,
                    "finished_at": datetime.now().isoformat(),
                }
            finally:
                # 例外が発生してもターゲット月をリセットしない
                st.session_state["target_year"] = _saved_target_year
                st.session_state["target_month"] = _saved_target_month
                # ここで st.query_params を書き換えると、Streamlit Cloud で
                # 追加の rerun が入り月が戻る場合があるため、session_state のみを更新する。

    with bcol2:
        # 確定版を読み込む
        if lock_info is not None:
            if st.button(
                "📥 確定版を読み込む",
                width="stretch",
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
                    try:
                        restore_validation_context_for_month(
                            int(target_year), int(target_month), rule_cfg,
                        )
                    except Exception:
                        pass
                    st.success("✅ 確定版を読み込みました")
                    st.rerun()
                else:
                    st.error(f"スナップショットが見つかりません: {snapshot_path}")
        else:
            st.button(
                "📥 確定版を読み込む",
                width="stretch",
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
                width="stretch",
                disabled=current_shift is None,
                type="secondary",
                help="現在のシフトを確定版として保存し、編集をロックします",
            ):
                st.session_state["show_lock_dialog"] = True
        else:
            # ロック済み: 解除ボタン表示
            if st.button(
                "🔓 ロックを解除",
                width="stretch",
                type="secondary",
                help="編集できる状態に戻します（バックアップは残ります）",
            ):
                st.session_state["show_unlock_dialog"] = True

    with bcol4:
        # ロック一覧表示
        with st.popover("📅 ロック済み一覧", width="stretch"):
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
                submit_lock = st.form_submit_button("✅ ロックする", type="primary", width="stretch")
            with col_b:
                cancel_lock = st.form_submit_button("キャンセル", width="stretch")
            if submit_lock and current_shift is not None:
                # バックアップ保存
                snapshot_path = save_shift_snapshot_with_github(
                    backup_mgr,
                    current_shift, kind="finalized",
                    author=lock_author, note=lock_note,
                )
                # ロック登録
                lock_path = lock_mgr.lock(
                    year=int(target_year), month=int(target_month),
                    locked_by=lock_author,
                    snapshot_file=snapshot_path.name,
                    note=lock_note,
                )
                push_lock_file_to_github(
                    lock_path, int(target_year), int(target_month), "lock",
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
                submit_unlock = st.form_submit_button("✅ 解除する", type="primary", width="stretch")
            with col_b:
                cancel_unlock = st.form_submit_button("キャンセル", width="stretch")
            if submit_unlock:
                if lock_mgr.unlock(int(target_year), int(target_month)):
                    try:
                        archive_dir = lock_mgr.lock_dir / "archive"
                        pattern = f"{int(target_year):04d}-{int(target_month):02d}_unlocked_*.json"
                        archive_files = sorted(
                            archive_dir.glob(pattern),
                            key=lambda p: p.stat().st_mtime,
                        )
                        if archive_files:
                            push_lock_file_to_github(
                                archive_files[-1],
                                int(target_year),
                                int(target_month),
                                "unlock",
                            )
                    except Exception:
                        pass
                st.success(f"✅ ロックを解除しました")
                st.session_state["show_unlock_dialog"] = False
                st.rerun()
            if cancel_unlock:
                st.session_state["show_unlock_dialog"] = False
                st.rerun()

    st.markdown("---")

    # シフト表示
    shift = get_session_shift_for_month(int(target_year), int(target_month))
    if shift is None:
        st.info("👆 上のボタンを押してシフトを生成してください")
        try:
            latest_draft = backup_mgr.get_latest_shift(
                int(target_year), int(target_month), kind="draft",
            )
        except Exception:
            latest_draft = None
        if latest_draft is not None:
            if st.button(
                f"💾 自動保存の下書き（{int(target_year)}年{int(target_month)}月）を復元",
                key="restore_latest_draft_shift",
            ):
                save_session_shift(latest_draft)
                try:
                    restore_validation_context_for_month(
                        int(target_year), int(target_month), rule_cfg,
                    )
                except Exception:
                    pass
                st.success("自動保存の下書きを復元しました")
                st.rerun()
    if shift is not None and int(shift.year) == int(target_year) and int(shift.month) == int(target_month):
        # Streamlit の tabs は送信後に先頭へ戻りやすいので、選択状態を保持するメニューで切り替える。
        shift_view_options = ["📋 シフト表", "📊 統計", "📥 出力"]
        if st.session_state.get("manager_shift_view") not in shift_view_options:
            st.session_state["manager_shift_view"] = shift_view_options[0]
        selected_shift_view = st.radio(
            "表示切替",
            options=shift_view_options,
            horizontal=True,
            key="manager_shift_view",
            label_visibility="collapsed",
        )

        if selected_shift_view == "📋 シフト表":
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

            render_shift_legend()
            _table_validation_context = get_validation_context_for_shift(shift)
            _table_ym = f"{int(shift.year):04d}-{int(shift.month):02d}"
            if _table_ym not in st.session_state.get("validation_inputs_by_month", {}):
                try:
                    _table_validation_context = restore_validation_context_for_month(
                        int(shift.year), int(shift.month), rule_cfg,
                    )
                except Exception:
                    pass
            _table_off_cells = build_off_request_cells(
                _table_validation_context.get("off_requests", {})
            )
            if _table_off_cells:
                st.caption("赤枠の × は、本人が提出した「絶対休み」の希望です。")

            edit_ym = f"{int(shift.year):04d}_{int(shift.month):02d}"
            inline_version_key = f"inline_shift_editor_version_{edit_ym}"
            inline_undo_key = f"inline_edit_undo_stack_{edit_ym}"
            inline_redo_key = f"inline_edit_redo_stack_{edit_ym}"
            inline_status_key = f"inline_edit_status_{edit_ym}"
            inline_autosave_key = f"inline_edit_autosave_signature_{edit_ym}"
            inline_draft_key = f"inline_edit_draft_rows_{edit_ym}"
            inline_base_signature_key = f"inline_edit_base_signature_{edit_ym}"
            if inline_version_key not in st.session_state:
                st.session_state[inline_version_key] = 0
            if inline_undo_key not in st.session_state:
                st.session_state[inline_undo_key] = []
            if inline_redo_key not in st.session_state:
                st.session_state[inline_redo_key] = []
            if st.session_state.get(inline_status_key):
                st.success(st.session_state.pop(inline_status_key))

            st.markdown("##### ✏️ 色付きシフト表を直接クリックして修正")
            st.caption(
                "セルをクリックすると、空白・×・各店舗記号を選べます。"
                "変更は下の「変更を確定」を押すまで本シフトには保存されません。"
            )
            with st.container(border=True):
                if lock_info is not None:
                    st.warning("この月は確定版としてロック中です。編集する場合は先にロックを解除してください。")

                editor_columns = ["日", "曜"] + EXPORT_COLUMN_ORDER + ["人数少", "鍵"]
                base_editor_rows = shift_to_editor_rows(shift)
                base_signature = editor_symbol_signature(base_editor_rows)
                if st.session_state.get(inline_base_signature_key) != base_signature:
                    st.session_state[inline_draft_key] = base_editor_rows
                    st.session_state[inline_base_signature_key] = base_signature
                draft_rows = st.session_state.get(inline_draft_key) or base_editor_rows
                draft_rows = refresh_editor_short_staff_column(shift, draft_rows)
                st.session_state[inline_draft_key] = draft_rows
                draft_changed_cells = get_editor_changed_cells(shift, draft_rows)
                editor_df = pd.DataFrame(draft_rows, columns=editor_columns)
                column_config = {
                    "日": st.column_config.NumberColumn("日", width="small"),
                    "曜": st.column_config.TextColumn("曜", width="small"),
                }
                for name in EXPORT_COLUMN_ORDER:
                    column_config[name] = st.column_config.SelectboxColumn(
                        name,
                        options=STORE_SYMBOL_OPTIONS,
                        width="small",
                        help="空白 / ×休み / ○赤羽 / □東口 / △大宮 / ☆西口 / ◆すずらん",
                    )
                disabled_columns = ["日", "曜", "人数少", "鍵"]
                if lock_info is not None:
                    disabled_columns = editor_columns

                editor_key = f"inline_shift_editor_{edit_ym}_{st.session_state[inline_version_key]}"
                if HAS_AGGRID:
                    st.markdown(
                        f'<div style="background:#0f172a; color:#ffffff; '
                        f'padding:10px 12px; border:1px solid #999; '
                        f'border-radius:6px 6px 0 0; font-weight:800; '
                        f'font-size:16px;">'
                        f'{int(shift.year)}年{int(shift.month)}月 シフト表</div>',
                        unsafe_allow_html=True,
                    )
                    grid_response = render_colored_shift_editor(
                        shift,
                        editor_df,
                        grid_key=editor_key,
                        locked=lock_info is not None,
                        off_request_cells=_table_off_cells,
                        changed_cells=draft_changed_cells,
                    )
                    edited_value = grid_response.get("data", editor_df)
                else:
                    st.warning(
                        "色付きの直接編集部品がまだ入っていないため、一時的に標準の編集表で表示しています。"
                        "GitHubに requirements.txt も反映すると、色付き編集に切り替わります。"
                    )
                    render_shift_table(
                        shift,
                        short_staff_by_store=detect_short_staff_by_store(shift),
                        off_request_cells=_table_off_cells,
                        sticky=True,
                        selectable_cells=False,
                    )
                    edited_value = st.data_editor(
                        editor_df,
                        key=editor_key,
                        hide_index=True,
                        width="stretch",
                        height=620,
                        num_rows="fixed",
                        column_config=column_config,
                        disabled=disabled_columns,
                    )
                edited_records = normalize_editor_records(editor_rows_to_records(edited_value))
                if not edited_records:
                    edited_records = draft_rows
                edited_records = refresh_editor_short_staff_column(shift, edited_records)
                if (
                    HAS_AGGRID
                    and lock_info is None
                    and editor_symbol_signature(edited_records)
                    != editor_symbol_signature(st.session_state.get(inline_draft_key, []))
                ):
                    st.session_state[inline_draft_key] = edited_records
                    st.rerun()

                inline_changed_cells = get_editor_changed_cells(shift, edited_records)
                inline_display_shift = editor_rows_to_shift(shift, edited_records)
                fixed_off_violations = get_fixed_off_edit_violations(
                    edited_records, _table_off_cells,
                )
                short_staff_by_store = detect_short_staff_by_store(inline_display_shift)
                short_days = set(short_staff_by_store.keys())
                if short_days:
                    short_day_text = format_short_staff_summary(inline_display_shift, short_staff_by_store)
                    st.warning(
                        f"⚠ 人員不足の日: {short_day_text}"
                        "（黄色でハイライト・人員少欄に店舗別マーク表示）"
                    )
                key_warnings_by_store = detect_key_warnings_by_store(inline_display_shift)
                if key_warnings_by_store:
                    st.warning(
                        "鍵確認: "
                        + format_key_warning_summary(inline_display_shift, key_warnings_by_store)
                        + "（鍵欄にも表示）"
                    )

                inline_result = run_shift_validation(
                    shift=inline_display_shift,
                    work_requests=_table_validation_context.get("work_requests", []),
                    off_requests=_table_validation_context.get("off_requests", {}),
                    prev_month=_table_validation_context.get("prev_month", []),
                    holiday_overrides=_table_validation_context.get("holiday_overrides", {}),
                    exact_holiday_days=_table_validation_context.get("exact_holiday_days", {}),
                    employee_max_consecutive_work=_table_validation_context.get("employee_max_consecutive_work", {}),
                    employee_max_consecutive_off=_table_validation_context.get("employee_max_consecutive_off", {}),
                    max_consec=rule_cfg.parameters.get("max_consec_work", 5),
                    monthly_store_count_rules=_table_validation_context.get("monthly_store_count_rules", []),
                )

                if inline_changed_cells and lock_info is None:
                    autosave_signature = json.dumps(
                        [
                            [
                                employee,
                                day,
                                assignment_to_symbol(
                                    inline_display_shift.get_assignment(employee, day)
                                ),
                            ]
                            for employee, day in sorted(inline_changed_cells)
                        ],
                        ensure_ascii=False,
                    )
                    if st.session_state.get(inline_autosave_key) != autosave_signature:
                        try:
                            save_shift_snapshot_with_github(
                                backup_mgr,
                                inline_display_shift,
                                kind="draft",
                                author="手動修正",
                                note=f"編集中の自動保存（未確定・{len(inline_changed_cells)}件）",
                            )
                            st.session_state[inline_autosave_key] = autosave_signature
                        except Exception:
                            pass

                state_col1, state_col2, state_col3, state_col4, state_col5 = st.columns(5)
                state_col1.metric("編集中の変更", len(inline_changed_cells))
                state_col2.metric("エラー", inline_result.error_count, delta_color="inverse")
                state_col3.metric("警告", inline_result.warning_count, delta_color="inverse")
                state_col4.metric("人員不足日", len(short_staff_by_store), delta_color="inverse")
                state_col5.metric("鍵確認", len(key_warnings_by_store), delta_color="inverse")
                render_part_time_paid_leave_suggestions(
                    inline_display_shift,
                    _table_validation_context,
                    key_prefix=f"inline_{edit_ym}",
                )

                needs_exception_save = bool(fixed_off_violations) or inline_result.error_count > 0
                allow_exception_save = False

                if fixed_off_violations:
                    st.error(
                        "本人の×休み希望を勤務へ変更しようとしているセルがあります: "
                        + "、".join(fixed_off_violations)
                        + "。通常運用では×へ戻してください。"
                    )
                elif inline_result.error_count == 0:
                    st.success("確定できる状態です。警告がある場合は内容だけ確認してください。")
                else:
                    st.error("エラーが残っています。下の詳細を確認して修正してください。")

                if needs_exception_save and inline_changed_cells and lock_info is None:
                    allow_exception_save = st.checkbox(
                        "管理者例外として、エラーが残っていても下書きに反映する",
                        key=f"inline_allow_exception_save_{edit_ym}",
                        help=(
                            "過去月のすり合わせや本人と後日合意済みの変更だけで使ってください。"
                            "エラー表示は残るため、確定前に必ず内容を確認してください。"
                        ),
                    )
                    if allow_exception_save:
                        st.warning(
                            "管理者例外として下書きに反映できます。"
                            "本人×休み希望やその他エラーは、検証詳細に残ります。"
                        )

                btn_col1, btn_col2, btn_col3, btn_col4, btn_col5 = st.columns([1, 1, 1, 1, 2])
                with btn_col1:
                    apply_disabled = (
                        lock_info is not None
                        or not inline_changed_cells
                        or (needs_exception_save and not allow_exception_save)
                    )
                    if st.button(
                        (
                            "下書きに反映"
                            if needs_exception_save and allow_exception_save
                            else "変更を確定"
                        ),
                        key=f"inline_edit_apply_{edit_ym}",
                        type="primary",
                        width="stretch",
                        disabled=apply_disabled,
                    ):
                        st.session_state[inline_undo_key].append(clone_monthly_shift(shift))
                        st.session_state[inline_undo_key] = st.session_state[inline_undo_key][-20:]
                        st.session_state[inline_redo_key] = []
                        save_session_shift(inline_display_shift)
                        record_edit_history_with_github(
                            backup_mgr,
                            shift.year,
                            shift.month,
                            before_shift=shift,
                            after_shift=inline_display_shift,
                            changed_cells=inline_changed_cells,
                            actor="手動修正",
                            reason=(
                                "シフト表直接編集（管理者例外）"
                                if needs_exception_save and allow_exception_save
                                else "シフト表直接編集"
                            ),
                        )
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            inline_display_shift,
                            kind="draft",
                            author="手動修正",
                            note=(
                                f"シフト表直接編集で{len(inline_changed_cells)}件変更"
                                + (
                                    "（エラーあり・管理者例外として下書き反映）"
                                    if needs_exception_save and allow_exception_save
                                    else ""
                                )
                            ),
                        )
                        st.session_state[inline_version_key] += 1
                        st.session_state.pop(inline_autosave_key, None)
                        st.session_state.pop(inline_draft_key, None)
                        st.session_state.pop(inline_base_signature_key, None)
                        st.session_state.pop("chat_engine", None)
                        st.session_state.pop("chat_shift_id", None)
                        st.session_state[inline_status_key] = (
                            f"{len(inline_changed_cells)}件の変更を下書きに反映しました。"
                            if needs_exception_save and allow_exception_save
                            else f"{len(inline_changed_cells)}件の変更を確定しました。"
                        )
                        st.rerun()
                with btn_col2:
                    if st.button(
                        "変更を破棄",
                        key=f"inline_edit_discard_{edit_ym}",
                        width="stretch",
                        disabled=not inline_changed_cells,
                    ):
                        st.session_state[inline_version_key] += 1
                        st.session_state.pop(inline_autosave_key, None)
                        st.session_state.pop(inline_draft_key, None)
                        st.session_state.pop(inline_base_signature_key, None)
                        st.session_state[inline_status_key] = "編集中の変更を破棄しました。"
                        st.rerun()
                with btn_col3:
                    if st.button(
                        "← 戻る",
                        key=f"inline_edit_undo_{edit_ym}",
                        width="stretch",
                        disabled=lock_info is not None or not st.session_state[inline_undo_key],
                    ):
                        previous_shift = st.session_state[inline_undo_key].pop()
                        st.session_state[inline_redo_key].append(clone_monthly_shift(shift))
                        undo_cells = get_editor_changed_cells(shift, shift_to_editor_rows(previous_shift))
                        save_session_shift(previous_shift)
                        record_edit_history_with_github(
                            backup_mgr,
                            shift.year,
                            shift.month,
                            before_shift=shift,
                            after_shift=previous_shift,
                            changed_cells=undo_cells,
                            actor="手動修正",
                            reason="戻る",
                        )
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            previous_shift,
                            kind="draft",
                            author="手動修正",
                            note="戻るでシフトを復元",
                        )
                        st.session_state[inline_version_key] += 1
                        st.session_state.pop(inline_autosave_key, None)
                        st.session_state.pop(inline_draft_key, None)
                        st.session_state.pop(inline_base_signature_key, None)
                        st.session_state.pop("chat_engine", None)
                        st.session_state.pop("chat_shift_id", None)
                        st.session_state[inline_status_key] = "直前の変更を元に戻しました。"
                        st.rerun()
                with btn_col4:
                    if st.button(
                        "進む →",
                        key=f"inline_edit_redo_{edit_ym}",
                        width="stretch",
                        disabled=lock_info is not None or not st.session_state[inline_redo_key],
                    ):
                        next_shift = st.session_state[inline_redo_key].pop()
                        st.session_state[inline_undo_key].append(clone_monthly_shift(shift))
                        redo_cells = get_editor_changed_cells(shift, shift_to_editor_rows(next_shift))
                        save_session_shift(next_shift)
                        record_edit_history_with_github(
                            backup_mgr,
                            shift.year,
                            shift.month,
                            before_shift=shift,
                            after_shift=next_shift,
                            changed_cells=redo_cells,
                            actor="手動修正",
                            reason="進む",
                        )
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            next_shift,
                            kind="draft",
                            author="手動修正",
                            note="進むでシフトを再反映",
                        )
                        st.session_state[inline_version_key] += 1
                        st.session_state.pop(inline_autosave_key, None)
                        st.session_state.pop(inline_draft_key, None)
                        st.session_state.pop(inline_base_signature_key, None)
                        st.session_state.pop("chat_engine", None)
                        st.session_state.pop("chat_shift_id", None)
                        st.session_state[inline_status_key] = "元に戻した変更をもう一度反映しました。"
                        st.rerun()
                with btn_col5:
                    if inline_changed_cells:
                        st.caption("緑枠のセルが、現在編集中の変更です。")
                    else:
                        st.caption("まだ変更はありません。")

                if inline_result.error_count > 0 or inline_result.warning_count > 0:
                    with st.expander(
                        f"エラー・警告の詳細（{inline_result.error_count + inline_result.warning_count}件）",
                        expanded=inline_result.error_count > 0,
                    ):
                        for issue in inline_result.issues:
                            prefix = "❌" if issue.severity == "ERROR" else "⚠"
                            st.write(f"{prefix} {issue}")

            if not HAS_AGGRID:
                st.markdown("##### 色付きプレビュー")
                render_shift_table(
                    inline_display_shift,
                    short_staff_by_store=short_staff_by_store,
                    sticky=True,
                    off_request_cells=_table_off_cells,
                    changed_cells=inline_changed_cells,
                    changed_cell_color="#16a34a",
                    selectable_cells=False,
                )

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

        elif selected_shift_view == "✏️ シフト修正":
            st.markdown("##### ✏️ シフト修正")
            st.caption(
                "表のセルをクリックして、空白・休み・各店舗を選びます。"
                "本人が提出した×は赤枠で固定扱いです。変更中のセルは下のプレビュー表で緑枠になります。"
            )

            validation_context = get_validation_context_for_shift(shift)
            off_request_cells = build_off_request_cells(
                validation_context.get("off_requests", {})
            )
            if off_request_cells:
                st.info(
                    "赤枠の×は、本人が提出した「絶対休み」です。通常は勤務へ変更しません。"
                    "過去月のすり合わせなど、本人と後日合意済みの例外だけ管理者例外として下書き反映できます。"
                )
            if lock_info is not None:
                st.warning("この月は確定版としてロック中です。編集する場合は先にロックを解除してください。")

            edit_ym = f"{int(shift.year):04d}_{int(shift.month):02d}"
            version_key = f"manual_edit_version_{edit_ym}"
            undo_key = f"manual_edit_undo_{edit_ym}"
            redo_key = f"manual_edit_redo_{edit_ym}"
            status_key = f"manual_edit_status_{edit_ym}"
            if version_key not in st.session_state:
                st.session_state[version_key] = 0
            if undo_key not in st.session_state:
                st.session_state[undo_key] = []
            if redo_key not in st.session_state:
                st.session_state[redo_key] = []
            if st.session_state.get(status_key):
                st.success(st.session_state.pop(status_key))

            editor_columns = ["日", "曜"] + EXPORT_COLUMN_ORDER + ["人数少", "鍵"]
            editor_df = pd.DataFrame(shift_to_editor_rows(shift), columns=editor_columns)
            column_config = {
                "日": st.column_config.NumberColumn("日", width="small"),
                "曜": st.column_config.TextColumn("曜", width="small"),
            }
            for name in EXPORT_COLUMN_ORDER:
                column_config[name] = st.column_config.SelectboxColumn(
                    name,
                    options=STORE_SYMBOL_OPTIONS,
                    width="small",
                    help="空白 / ×休み / ○赤羽 / □東口 / △大宮 / ☆西口 / ◆すずらん",
                )
            disabled_columns = ["日", "曜", "人数少", "鍵"]
            if lock_info is not None:
                disabled_columns = editor_columns

            edited_value = st.data_editor(
                editor_df,
                key=f"manual_shift_editor_{edit_ym}_{st.session_state[version_key]}",
                hide_index=True,
                width="stretch",
                height=620,
                num_rows="fixed",
                column_config=column_config,
                disabled=disabled_columns,
            )
            edited_records = editor_rows_to_records(edited_value)
            if not edited_records:
                edited_records = shift_to_editor_rows(shift)
            edited_records = refresh_editor_short_staff_column(shift, edited_records)

            changed_cells = get_editor_changed_cells(shift, edited_records)
            edited_shift = editor_rows_to_shift(shift, edited_records)
            fixed_off_violations = get_fixed_off_edit_violations(
                edited_records, off_request_cells,
            )

            edit_result = run_shift_validation(
                shift=edited_shift,
                work_requests=validation_context.get("work_requests", []),
                off_requests=validation_context.get("off_requests", {}),
                prev_month=validation_context.get("prev_month", []),
                holiday_overrides=validation_context.get("holiday_overrides", {}),
                exact_holiday_days=validation_context.get("exact_holiday_days", {}),
                employee_max_consecutive_work=validation_context.get("employee_max_consecutive_work", {}),
                employee_max_consecutive_off=validation_context.get("employee_max_consecutive_off", {}),
                max_consec=rule_cfg.parameters.get("max_consec_work", 5),
                monthly_store_count_rules=validation_context.get("monthly_store_count_rules", []),
            )
            edit_short_staff_by_store = detect_short_staff_by_store(edited_shift)
            edit_key_warnings_by_store = detect_key_warnings_by_store(edited_shift)

            st.markdown("##### 編集中の状態")
            state_col1, state_col2, state_col3, state_col4, state_col5 = st.columns(5)
            state_col1.metric("変更セル", len(changed_cells))
            state_col2.metric("エラー", edit_result.error_count, delta_color="inverse")
            state_col3.metric("警告", edit_result.warning_count, delta_color="inverse")
            state_col4.metric("人員不足日", len(edit_short_staff_by_store), delta_color="inverse")
            state_col5.metric("鍵確認", len(edit_key_warnings_by_store), delta_color="inverse")

            needs_exception_save = bool(fixed_off_violations) or edit_result.error_count > 0
            allow_exception_save = False

            if fixed_off_violations:
                st.error(
                    "本人の×休み希望を勤務へ変更しようとしているセルがあります: "
                    + "、".join(fixed_off_violations)
                    + "。通常運用では×へ戻してください。"
                )
            elif edit_result.error_count == 0:
                st.success("確定できる状態です。警告がある場合は内容だけ確認してください。")
            else:
                st.error("エラーが残っています。下の詳細を確認して修正してください。")

            if needs_exception_save and changed_cells and lock_info is None:
                allow_exception_save = st.checkbox(
                    "管理者例外として、エラーが残っていても下書きに反映する",
                    key=f"manual_allow_exception_save_{edit_ym}",
                    help=(
                        "過去月のすり合わせや本人と後日合意済みの変更だけで使ってください。"
                        "エラー表示は残るため、確定前に必ず内容を確認してください。"
                    ),
                )
                if allow_exception_save:
                    st.warning(
                        "管理者例外として下書きに反映できます。"
                        "本人×休み希望やその他エラーは、検証詳細に残ります。"
                    )

            action_col1, action_col2, action_col3, action_col4, action_col5 = st.columns([1, 1, 1, 1, 2])
            with action_col1:
                confirm_disabled = (
                    lock_info is not None
                    or not changed_cells
                    or (needs_exception_save and not allow_exception_save)
                )
                if st.button(
                    (
                        "下書きに反映"
                        if needs_exception_save and allow_exception_save
                        else "変更を確定"
                    ),
                    key="manual_edit_apply",
                    type="primary",
                    width="stretch",
                    disabled=confirm_disabled,
                    help="通常はエラー0件で反映します。管理者例外チェック時だけエラーありの下書き反映ができます",
                ):
                    before_shift = clone_monthly_shift(shift)
                    st.session_state[undo_key].append(before_shift)
                    st.session_state[undo_key] = st.session_state[undo_key][-20:]
                    st.session_state[redo_key] = []
                    save_session_shift(edited_shift)
                    record_edit_history_with_github(
                        backup_mgr,
                        shift.year,
                        shift.month,
                        before_shift=before_shift,
                        after_shift=edited_shift,
                        changed_cells=changed_cells,
                        actor="手動修正",
                        reason=(
                            "クリック編集（管理者例外）"
                            if needs_exception_save and allow_exception_save
                            else "クリック編集"
                        ),
                    )
                    try:
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            edited_shift,
                            kind="draft",
                            author="手動修正",
                            note=(
                                f"クリック編集で{len(changed_cells)}件変更"
                                + (
                                    "（エラーあり・管理者例外として下書き反映）"
                                    if needs_exception_save and allow_exception_save
                                    else ""
                                )
                            ),
                        )
                    except Exception:
                        pass
                    st.session_state[version_key] += 1
                    st.session_state.pop("chat_engine", None)
                    st.session_state.pop("chat_shift_id", None)
                    st.session_state[status_key] = (
                        f"{len(changed_cells)}件の変更を下書きに反映しました。"
                        if needs_exception_save and allow_exception_save
                        else f"{len(changed_cells)}件の変更を確定しました。"
                    )
                    st.rerun()
            with action_col2:
                if st.button(
                    "変更を破棄",
                    key="manual_edit_discard",
                    width="stretch",
                    disabled=not changed_cells,
                    help="表で編集中の内容を捨てて、本シフトの内容に戻します",
                ):
                    st.session_state[version_key] += 1
                    st.session_state[status_key] = "編集中の変更を破棄しました。"
                    st.rerun()
            with action_col3:
                if st.button(
                    "← 戻る",
                    key="manual_edit_undo",
                    width="stretch",
                    disabled=lock_info is not None or not st.session_state[undo_key],
                    help="直前に確定した手動修正を元に戻します",
                ):
                    previous_shift = st.session_state[undo_key].pop()
                    st.session_state[redo_key].append(clone_monthly_shift(shift))
                    save_session_shift(previous_shift)
                    st.session_state[version_key] += 1
                    st.session_state.pop("chat_engine", None)
                    st.session_state.pop("chat_shift_id", None)
                    st.session_state[status_key] = "直前の手動修正を元に戻しました。"
                    st.rerun()
            with action_col4:
                if st.button(
                    "進む →",
                    key="manual_edit_redo",
                    width="stretch",
                    disabled=lock_info is not None or not st.session_state[redo_key],
                    help="元に戻した手動修正をもう一度反映します",
                ):
                    next_shift = st.session_state[redo_key].pop()
                    st.session_state[undo_key].append(clone_monthly_shift(shift))
                    save_session_shift(next_shift)
                    st.session_state[version_key] += 1
                    st.session_state.pop("chat_engine", None)
                    st.session_state.pop("chat_shift_id", None)
                    st.session_state[status_key] = "元に戻した手動修正をもう一度反映しました。"
                    st.rerun()
            with action_col5:
                if changed_cells:
                    st.caption("緑枠のセルが、現在編集中の変更です。")
                else:
                    st.caption("まだ変更はありません。")

            with st.expander(
                f"エラー・警告の詳細（{edit_result.error_count + edit_result.warning_count}件）",
                expanded=edit_result.error_count > 0,
            ):
                if not edit_result.issues:
                    st.write("問題はありません。")
                else:
                    for issue in edit_result.issues:
                        prefix = "❌" if issue.severity == "ERROR" else "⚠"
                        st.write(f"{prefix} {issue}")

            st.markdown("##### プレビュー")
            render_shift_legend()
            render_shift_table(
                edited_shift,
                short_staff_by_store=edit_short_staff_by_store,
                key_warnings_by_store=edit_key_warnings_by_store,
                sticky=True,
                changed_cells=changed_cells,
                changed_cell_color="#16a34a",
                off_request_cells=off_request_cells,
            )

        elif selected_shift_view == "✅ 検証結果":
            # シフト生成時に使った制約を取得（無ければ空＝制約なしで検証）
            _validation_context = get_validation_context_for_shift(shift)
            _v_work = _validation_context.get("work_requests", [])
            _v_off = _validation_context.get("off_requests", {})
            _v_prev = _validation_context.get("prev_month", [])
            _v_holiday = _validation_context.get("holiday_overrides", {})
            _v_exact_holiday = _validation_context.get("exact_holiday_days", {})
            _v_max_work = _validation_context.get("employee_max_consecutive_work", {})
            _v_max_off = _validation_context.get("employee_max_consecutive_off", {})
            result = run_shift_validation(
                shift=shift, work_requests=_v_work,
                off_requests=_v_off, prev_month=_v_prev,
                holiday_overrides=_v_holiday,
                exact_holiday_days=_v_exact_holiday,
                employee_max_consecutive_work=_v_max_work,
                employee_max_consecutive_off=_v_max_off,
                max_consec=rule_cfg.parameters.get("max_consec_work", 5),
                monthly_store_count_rules=_validation_context.get("monthly_store_count_rules", []),
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

        elif selected_shift_view == "📊 統計":
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
                target = get_monthly_work_target(
                    e.name,
                    shift.month,
                    e.annual_target_days,
                )
                diff = (work - target) if target else None
                data.append({
                    "氏名": e.name,
                    "出勤": work,
                    "休": off,
                    "目標": str(target) if target else "-",
                    "差分": f"{diff:+d}" if diff is not None else "-",
                })
            st.dataframe(data, width="stretch", hide_index=True)

        elif selected_shift_view == "📥 出力":
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
            short_staff_for_export = detect_short_staff_by_store(shift)
            key_warnings_for_export = detect_key_warnings_by_store(shift)

            st.info(
                "📝 「📋 シフト表」タブで入力したコメントと注意書きが反映されます。"
                "未入力の場合は空欄になります。"
            )

            col_x, col_p = st.columns(2)
            with col_x:
                st.write("**📁 Excel 形式（編集可）**")
                if st.button("Excel を生成", key="gen_xlsx"):
                    file_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.xlsx"
                    call_with_supported_kwargs(
                        export_shift_to_excel,
                        shift, file_path,
                        header_comments=header_comments,
                        footer_notes=footer_notes if footer_notes else None,
                        short_staff_days=short_staff_for_export,
                        key_warnings_by_store=key_warnings_for_export,
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
                st.write("**📄 PDF 形式（印刷用・A4縦1枚）**")
                if st.button("PDF を生成", key="gen_pdf"):
                    file_path = output_dir / f"{shift.year}年{shift.month}月_AI生成シフト.pdf"
                    call_with_supported_kwargs(
                        export_shift_to_pdf,
                        shift, file_path,
                        header_notes=header_comments,
                        footer_notes=footer_notes if footer_notes else None,
                        short_staff_days=short_staff_for_export,
                        key_warnings_by_store=key_warnings_for_export,
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
                path = save_shift_snapshot_with_github(
                    backup, shift, kind=kind, author="代表取締役", note=note,
                )
                st.success(f"✅ バックアップ保存: {path.name}")

        elif selected_shift_view == "💬 AI相談":
            st.markdown("##### 💬 AI相談（補助機能）")
            st.caption(
                "基本の修正は「シフト修正」画面で行います。AIは、エラーの直し方や候補探しを相談する補助機能として使えます。"
            )

            api_key = get_anthropic_api_key()
            if not api_key:
                st.warning(
                    "⚠ Claude API キーが設定されていません。"
                    "Streamlit Cloud の Settings → Secrets に "
                    "`ANTHROPIC_API_KEY` を登録してください。"
                )
            else:
                _chat_validation_inputs = st.session_state.get("last_validation_inputs", {})
                _chat_validation_match = (
                    _chat_validation_inputs.get("ym")
                    == f"{int(shift.year):04d}-{int(shift.month):02d}"
                )
                if not _chat_validation_match:
                    _chat_validation_inputs = {}
                _chat_max_consec = rule_cfg.parameters.get("max_consec_work", 5)
                if "chat_engine" not in st.session_state or st.session_state.get("chat_shift_id") != id(shift):
                    st.session_state.chat_engine = ShiftChatEngine(
                        shift,
                        api_key=api_key,
                        validation_inputs=_chat_validation_inputs,
                        max_consec=_chat_max_consec,
                    )
                    st.session_state.chat_shift_id = id(shift)
                    st.session_state.chat_messages = []

                chat_engine = st.session_state.chat_engine
                chat_engine.set_validation_context(
                    _chat_validation_inputs,
                    max_consec=_chat_max_consec,
                )

                st.markdown("##### 📋 現在のシフト表")
                st.caption("AIが作ったプレビュー変更は、表ではオレンジ枠で表示します。")

                def _save_chat_shift_snapshot(note: str) -> None:
                    try:
                        save_shift_snapshot_with_github(
                            backup_mgr,
                            chat_engine.shift,
                            kind="draft",
                            author="AI対話",
                            note=note,
                        )
                    except Exception:
                        pass
                    save_session_shift(chat_engine.shift)

                pending_count = chat_engine.get_pending_change_count()
                if pending_count:
                    st.warning(
                        f"プレビュー中の変更が **{pending_count}件** あります。"
                        "まだ本シフトには入っていません。"
                    )
                    for line in chat_engine.get_pending_change_summary():
                        st.caption(line)
                elif chat_engine.last_status_message:
                    st.success(chat_engine.last_status_message)
                else:
                    st.info("プレビュー中の変更はありません。AIに依頼すると、まず表にプレビュー表示されます。")

                def _render_chat_action_buttons() -> None:
                    action_pending_count = chat_engine.get_pending_change_count()

                    st.markdown("##### 操作")
                    if action_pending_count and st.session_state.get("chat_apply_confirm"):
                        st.warning(
                            f"プレビュー中の変更 **{action_pending_count}件** を本シフトに反映します。"
                            "反映後も「戻る」で直前の反映を取り消せます。"
                        )
                        confirm_apply_col, cancel_apply_col, _ = st.columns([1.3, 1, 2.7])
                        with confirm_apply_col:
                            if st.button(
                                "本シフトに反映",
                                key="chat_apply_confirmed",
                                type="primary",
                                width="stretch",
                            ):
                                msg = chat_engine.apply_pending_changes()
                                _save_chat_shift_snapshot(msg)
                                st.session_state.chat_messages.append({"role": "assistant", "content": msg})
                                st.session_state["chat_apply_confirm"] = False
                                st.session_state["chat_discard_confirm"] = False
                                st.rerun()
                        with cancel_apply_col:
                            if st.button("操作に戻る", key="chat_apply_cancel", width="stretch"):
                                st.session_state["chat_apply_confirm"] = False
                                st.rerun()
                    else:
                        btn_apply, btn_discard, _ = st.columns([1, 1, 3])
                        with btn_apply:
                            if st.button(
                                "本シフトに反映",
                                key="chat_apply_pending",
                                type="primary",
                                width="stretch",
                                disabled=action_pending_count == 0,
                                help="プレビュー中の変更を本シフトに反映する前に確認します",
                            ):
                                st.session_state["chat_apply_confirm"] = True
                                st.session_state["chat_discard_confirm"] = False
                                st.rerun()
                        with btn_discard:
                            if st.button(
                                "プレビューを破棄",
                                key="chat_discard_pending",
                                width="stretch",
                                disabled=action_pending_count == 0,
                                help="本シフトは変えず、プレビュー中の変更だけを消します",
                            ):
                                msg = chat_engine.discard_pending_changes()
                                st.session_state.chat_messages.append({"role": "assistant", "content": msg})
                                st.session_state["chat_apply_confirm"] = False
                                st.session_state["chat_discard_confirm"] = False
                                st.rerun()

                    can_undo = bool(chat_engine.undo_stack)
                    can_redo = bool(chat_engine.redo_stack)
                    btn_undo, btn_redo, btn_clear = st.columns([1, 1, 3])
                    with btn_undo:
                        if st.button(
                            "← 戻る",
                            key="chat_undo",
                            width="stretch",
                            disabled=not can_undo,
                            help="直前に反映した変更を元に戻します",
                        ):
                            msg = chat_engine.undo_last_apply()
                            _save_chat_shift_snapshot(msg)
                            st.session_state.chat_messages.append({"role": "assistant", "content": msg})
                            st.session_state["chat_apply_confirm"] = False
                            st.session_state["chat_discard_confirm"] = False
                            st.rerun()
                    with btn_redo:
                        if st.button(
                            "進む →",
                            key="chat_redo",
                            width="stretch",
                            disabled=not can_redo,
                            help="元に戻した変更をもう一度反映します",
                        ):
                            msg = chat_engine.redo_last_apply()
                            _save_chat_shift_snapshot(msg)
                            st.session_state.chat_messages.append({"role": "assistant", "content": msg})
                            st.session_state["chat_apply_confirm"] = False
                            st.session_state["chat_discard_confirm"] = False
                            st.rerun()
                    with btn_clear:
                        if st.button("会話をクリア", key="chat_clear", width="stretch"):
                            st.session_state.chat_messages = []
                            st.rerun()

                pending_count = chat_engine.get_pending_change_count()
                display_shift = chat_engine.get_preview_shift() if pending_count else chat_engine.shift
                changed_cells = chat_engine.get_pending_change_keys() if pending_count else set()

                _cv_inputs = _chat_validation_inputs
                _cv_match = (
                    _cv_inputs.get("ym")
                    == f"{int(display_shift.year):04d}-{int(display_shift.month):02d}"
                )
                if _cv_match:
                    _cv_work = _cv_inputs.get("work_requests", [])
                    _cv_off = _cv_inputs.get("off_requests", {})
                    _cv_prev = _cv_inputs.get("prev_month", [])
                    _cv_holiday = _cv_inputs.get("holiday_overrides", {})
                    _cv_exact_holiday = _cv_inputs.get("exact_holiday_days", {})
                    _cv_max_work = _cv_inputs.get("employee_max_consecutive_work", {})
                    _cv_max_off = _cv_inputs.get("employee_max_consecutive_off", {})
                    _cv_monthly_rules = _cv_inputs.get("monthly_store_count_rules", [])
                else:
                    _cv_work = []
                    _cv_off = {}
                    _cv_prev = []
                    _cv_holiday = {}
                    _cv_exact_holiday = {}
                    _cv_max_work = {}
                    _cv_max_off = {}
                    _cv_monthly_rules = []
                chat_result = run_shift_validation(
                    shift=display_shift, work_requests=_cv_work,
                    off_requests=_cv_off, prev_month=_cv_prev,
                    holiday_overrides=_cv_holiday,
                    exact_holiday_days=_cv_exact_holiday,
                    employee_max_consecutive_work=_cv_max_work,
                    employee_max_consecutive_off=_cv_max_off,
                    max_consec=rule_cfg.parameters.get("max_consec_work", 5),
                    monthly_store_count_rules=_cv_monthly_rules,
                )
                short_staff_by_store_chat = detect_short_staff_by_store(display_shift)
                short_days_chat = set(short_staff_by_store_chat.keys())

                render_shift_legend()
                if _cv_off:
                    st.caption("赤枠の × は、本人が提出した「絶対休み」の希望です。")
                render_shift_table(
                    display_shift,
                    short_staff_by_store=short_staff_by_store_chat,
                    sticky=True,
                    changed_cells=changed_cells,
                    off_request_cells=build_off_request_cells(_cv_off),
                )

                _render_chat_action_buttons()

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
                    if short_days_chat:
                        short_day_text_chat = format_short_staff_summary(
                            display_shift, short_staff_by_store_chat,
                        )
                        st.markdown(
                            f'<div style="background:#fef3c7; padding:8px; border-radius:6px; '
                            f'text-align:center; font-weight:bold; color:#92400e;">'
                            f'👥 人員不足: {short_day_text_chat}</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            '<div style="background:#dcfce7; padding:8px; border-radius:6px; '
                            'text-align:center; font-weight:bold; color:#166534;">'
                            '👥 人員充足</div>',
                            unsafe_allow_html=True,
                        )

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
                                        f'{" · " + str(display_shift.month) + "/" + str(issue.day) if issue.day else ""}'
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
                                        f'{" · " + str(display_shift.month) + "/" + str(issue.day) if issue.day else ""}'
                                        f'{" · " + issue.employee if issue.employee else ""}<br>'
                                        f'<span style="color:#713f12;">{issue.message}</span></div>',
                                        unsafe_allow_html=True,
                                    )

                st.markdown("---")
                st.markdown("##### 会話")
                chat_container = st.container(height=360, border=True)
                with chat_container:
                    if not st.session_state.chat_messages:
                        with st.chat_message("assistant"):
                            st.write(
                                "シフト表を見ながら相談できます。"
                                f"たとえば「{shift.month}/15 の大宮に田中さんを入れたい」"
                                "「鈴木さんと黒澤さんの20日を入れ替えるとどうなる？」のように送ってください。"
                            )
                    for msg in st.session_state.chat_messages:
                        with st.chat_message(msg["role"]):
                            st.write(msg["content"])

                st.markdown("##### メッセージ入力")
                with st.form(
                    f"chat_prompt_form_{shift.year}_{shift.month}",
                    clear_on_submit=True,
                ):
                    prompt = st.text_area(
                        "メッセージ",
                        placeholder=(
                            f"{shift.month}/15 の大宮に田中さんを入れたい\n"
                            "鈴木さんと黒澤さんの20日を入れ替えるとどうなる？"
                        ),
                        height=100,
                        key=f"chat_prompt_text_{shift.year}_{shift.month}",
                    )
                    send_prompt = st.form_submit_button(
                        "送信",
                        type="primary",
                        width="stretch",
                    )

                if send_prompt:
                    prompt = prompt.strip()
                    if not prompt:
                        st.warning("AIに送る内容を入力してください。")
                    else:
                        st.session_state.chat_messages.append({"role": "user", "content": prompt})
                        st.session_state["chat_apply_confirm"] = False
                        st.session_state["chat_discard_confirm"] = False
                        with st.spinner("AIが考え中..."):
                            try:
                                response = chat_engine.chat(prompt)
                            except Exception as chat_err:
                                response = (
                                    "AI対話中にエラーが発生しました。"
                                    "APIキーの設定、利用上限、または通信状態を確認してください。\n\n"
                                    f"詳細: {type(chat_err).__name__}: {chat_err}"
                                )
                        st.session_state.chat_messages.append({"role": "assistant", "content": response})
                        st.rerun()

                save_session_shift(chat_engine.shift)


# ============================================================
# 従業員ビュー（スマホ向け）
# ============================================================

elif mode == "👤 従業員ビュー":
    st.title("👤 希望シフト")

    # マジックリンクでログインしている場合は、その従業員に固定
    from auth import get_logged_in_employee, is_employee, is_manager
    logged_in_emp = get_logged_in_employee()

    # employee_names は後でボタンキー生成に使うので、ここで必ず定義しておく
    employee_names = shift_submission_employee_names()

    if is_employee() and logged_in_emp:
        # 従業員モード（マジックリンク経由）: 自分に固定
        selected = logged_in_emp
        # ログイン中の従業員が在籍リストに含まれていない場合（退職など）の救済
        if selected not in employee_names:
            employee_names = employee_names + [selected]
        st.markdown(
            f'<div style="background:#dcfce7; padding:12px 16px; border-radius:8px; '
            f'border-left:4px solid #16a34a; margin-bottom:12px;">'
            f'👋 こんにちは、<strong>{selected}さん</strong>。<br>'
            f'<span style="font-size:13px; color:#166534;">'
            f'このページで希望提出と確定シフトの確認ができます。'
            f'</span></div>',
            unsafe_allow_html=True,
        )
    elif is_manager():
        # 経営者がプレビューする場合: 従業員を選択可能
        st.info(
            "💡 経営者として閲覧中です。実運用では従業員はマジックリンク経由で"
            "自動的に自分の画面が開きます。動作確認のため任意の従業員を選択できます。"
        )
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

    # ============================================================
    # 従業員側の表示範囲
    # - 従業員リンクでは、原則「翌月の希望提出」と「今月の確定シフト確認」だけ。
    # - 過去月・翌々月などのテスト操作は、管理者プレビュー時だけ折りたたみ内に残す。
    # ============================================================
    today = date.today()
    # 翌月計算
    if today.month == 12:
        next_year, next_month = today.year + 1, 1
    else:
        next_year, next_month = today.year, today.month + 1
    # 翌々月
    if next_month == 12:
        nn_year, nn_month = next_year + 1, 1
    else:
        nn_year, nn_month = next_year, next_month + 1
    # 前月
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    # 前々月
    if prev_month == 1:
        pp_year, pp_month = prev_year - 1, 12
    else:
        pp_year, pp_month = prev_year, prev_month - 1

    employee_view_mode = "翌月の希望を提出"
    if is_employee():
        employee_view_mode = st.radio(
            "表示",
            ["翌月の希望を提出", "今月の確定シフトを見る"],
            horizontal=True,
            key="employee_magic_link_view_mode",
            label_visibility="collapsed",
        )

        if employee_view_mode == "今月の確定シフトを見る":
            st.markdown(f"### {today.year}年{today.month}月の確定シフト")
            public_shift, public_shift_source = load_public_shift_for_employee_view(
                today.year, today.month,
            )
            if public_shift is None:
                st.warning(
                    "今月の確定シフトはまだ公開されていません。"
                    "確定後にこの画面で確認できます。"
                )
            else:
                st.success(f"公開中の確定シフトを表示しています（{public_shift_source}）。")
                render_employee_confirmed_shift(public_shift, selected)
            st.info(
                "急な忌引き・体調不良などで変更が必要な場合は、"
                "この画面で上書きせず、店長または管理者へ直接連絡してください。"
                "管理者側でシフトを修正して再度確定します。"
            )
            st.stop()

        target_year = next_year
        target_month = next_month
        st.markdown(
            f'<div style="background:#eff6ff; padding:12px 16px; border-radius:8px; '
            f'border-left:4px solid #2563eb; margin:8px 0 14px 0;">'
            f'<strong>提出対象:</strong> {target_year}年{target_month}月分<br>'
            f'<span style="font-size:13px; color:#1e40af;">'
            f'月が替わると、この提出対象は自動で次の月に切り替わります。'
            f'</span></div>',
            unsafe_allow_html=True,
        )
    else:
        # 管理者プレビューでは、過去月テストや翌々月確認のために月変更を残す。
        if "emp_target_year" not in st.session_state:
            st.session_state["emp_target_year"] = next_year
        if "emp_target_month" not in st.session_state:
            st.session_state["emp_target_month"] = next_month

        target_year = st.session_state["emp_target_year"]
        target_month = st.session_state["emp_target_month"]
        st.markdown(
            f"##### 📅 管理者プレビュー対象: {int(target_year)}年{int(target_month)}月"
        )
        with st.expander("管理者プレビュー用に対象月を変更", expanded=False):
            qb_col1, qb_col2, qb_col3, qb_col4 = st.columns(4)
            with qb_col1:
                if st.button(f"前月\n({prev_year}/{prev_month})", key="emp_qb_prev", width="stretch"):
                    st.session_state["emp_target_year"] = prev_year
                    st.session_state["emp_target_month"] = prev_month
                    st.rerun()
            with qb_col2:
                if st.button(f"今月\n({today.year}/{today.month})", key="emp_qb_curr", width="stretch"):
                    st.session_state["emp_target_year"] = today.year
                    st.session_state["emp_target_month"] = today.month
                    st.rerun()
            with qb_col3:
                if st.button(
                    f"📌 翌月\n({next_year}/{next_month})",
                    key="emp_qb_next",
                    type="primary",
                    width="stretch",
                ):
                    st.session_state["emp_target_year"] = next_year
                    st.session_state["emp_target_month"] = next_month
                    st.rerun()
            with qb_col4:
                if st.button(f"翌々月\n({nn_year}/{nn_month})", key="emp_qb_nn", width="stretch"):
                    st.session_state["emp_target_year"] = nn_year
                    st.session_state["emp_target_month"] = nn_month
                    st.rerun()

    days_in_month = monthrange(target_year, target_month)[1]
    free_text_key = f"free_text_{selected}_{target_year}_{target_month}"
    paid_leave_key = f"paid_leave_days_{selected}_{target_year}_{target_month}"
    review_key = f"pref_review_{selected}_{target_year}_{target_month}"
    done_key = f"pref_done_{selected}_{target_year}_{target_month}"

    if "user_prefs" not in st.session_state:
        st.session_state.user_prefs = {}
    user_key = f"{selected}_{target_year}_{target_month}"
    existing_submission = None
    edit_existing_key = f"pref_edit_existing_{selected}_{target_year}_{target_month}"
    try:
        _status = ShiftBackup().get_submission_status(
            int(target_year), int(target_month), [selected],
        )
        existing_submission = next(
            (s for s in _status.get("submitted", []) if s.get("employee") == selected),
            None,
        )
    except Exception:
        existing_submission = None

    if (
        existing_submission
        and not st.session_state.get(done_key)
        and not st.session_state.get(edit_existing_key)
    ):
        submitted_at = existing_submission.get("submitted_at", "")[:19].replace("T", " ")
        x_days_submitted = existing_submission.get("off_request_days", [])
        triangle_days_submitted = existing_submission.get("flexible_off_days", [])
        ok_days_submitted = [
            d for d in range(1, days_in_month + 1)
            if d not in set(x_days_submitted) and d not in set(triangle_days_submitted)
        ]
        paid_leave_submitted = int(existing_submission.get("paid_leave_days", 0) or 0)
        st.markdown("### 提出済み")
        st.success(
            f"✅ **{selected}さんの {target_year}年{target_month}月分** は提出済みです。\n\n"
            f"提出日時: {submitted_at or '保存済み'}"
        )
        st.info(
            "この画面が表示されていれば、希望は保存されています。"
            "内容を変更したい場合だけ、下のボタンから修正して再提出してください。"
        )
        submitted_rows = [
            {"項目": "× 休み希望（絶対）", "内容": format_day_list(x_days_submitted), "日数": len(x_days_submitted)},
            {"項目": "△ できれば休み", "内容": format_day_list(triangle_days_submitted), "日数": len(triangle_days_submitted)},
            {"項目": "○ 出勤可能", "内容": format_day_list(ok_days_submitted), "日数": len(ok_days_submitted)},
            {"項目": "希望有給日数", "内容": f"{paid_leave_submitted}日", "日数": paid_leave_submitted},
            {"項目": "自由記述", "内容": existing_submission.get("note", "").strip() or "なし", "日数": ""},
        ]
        render_scrollable_review_table(submitted_rows)
        if st.button(
            "内容を修正して再提出する",
            key=f"edit_existing_submission_{selected}_{target_year}_{target_month}",
            width="stretch",
        ):
            st.session_state[edit_existing_key] = True
            st.rerun()
        st.stop()

    if st.session_state.get(done_key):
        done_info = st.session_state.get(done_key) or {}
        x_days_done = done_info.get("x_days", [])
        triangle_days_done = done_info.get("triangle_days", [])
        ok_days_done = done_info.get("ok_days", [])
        paid_leave_done = int(done_info.get("paid_leave_days", 0) or 0)
        submitted_at_done = done_info.get("submitted_at", datetime.now().isoformat())[:19].replace("T", " ")

        st.markdown("### 提出完了")
        st.success(
            f"✅ **{selected}さんの {target_year}年{target_month}月分** の希望を受け付けました。\n\n"
            f"提出日時: {submitted_at_done}"
        )
        st.info(
            "この画面が表示されていれば提出は完了しています。"
            "内容を変えたい場合だけ、下のボタンから修正して再提出してください。"
        )
        completion_rows = [
            {"項目": "× 休み希望（絶対）", "内容": format_day_list(x_days_done), "日数": len(x_days_done)},
            {"項目": "△ できれば休み", "内容": format_day_list(triangle_days_done), "日数": len(triangle_days_done)},
            {"項目": "○ 出勤可能", "内容": format_day_list(ok_days_done), "日数": len(ok_days_done)},
            {"項目": "希望有給日数", "内容": f"{paid_leave_done}日", "日数": paid_leave_done},
            {"項目": "自由記述", "内容": done_info.get("free_text", "").strip() or "なし", "日数": ""},
        ]
        render_scrollable_review_table(completion_rows)
        if st.button(
            "内容を修正して再提出する",
            key=f"edit_after_done_{selected}_{target_year}_{target_month}",
            width="stretch",
        ):
            st.session_state.pop(done_key, None)
            st.session_state[review_key] = False
            st.session_state[edit_existing_key] = True
            st.rerun()
        st.stop()

    # テスト月（過去・今月）の場合は注意表示
    is_test_month = (
        (target_year < today.year)
        or (target_year == today.year and target_month <= today.month)
    )
    if is_test_month:
        st.info(
            f"💡 **テスト用の月（{target_year}年{target_month}月）を選択中**です。"
            "本番運用時は「📌 翌月」を選んでください。"
        )

    st.markdown(f"### {target_year}年{target_month}月の希望を入力")
    st.caption("各日ごとに、右側の ○・△・× から1つ選んでください。")
    st.markdown(
        """
        <div style="display:flex; gap:14px; margin:8px 0 16px 0; font-size:15px; flex-wrap:wrap;">
          <span style="background:#dcfce7; color:#166534; padding:6px 14px; border-radius:999px; font-weight:bold;">○ 出勤可能</span>
          <span style="background:#fef9c3; color:#854d0e; padding:6px 14px; border-radius:999px; font-weight:bold;">△ できれば休み</span>
          <span style="background:#fee2e2; color:#991b1b; padding:6px 14px; border-radius:999px; font-weight:bold;">× 休み希望</span>
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
        .employee-answer-title {
            padding: 14px 16px;
            background: #f8fafc;
            border-bottom: 1px solid #e5e7eb;
            font-size: 20px;
            font-weight: 800;
            color: #111827;
            margin: 0 0 12px 0;
            border-radius: 8px 8px 0 0;
        }
        [class*="st-key-choice_seg_"] {
            margin: 4px 0 10px 0;
        }
        [class*="st-key-choice_seg_"] label p {
            font-size: 16px !important;
            font-weight: 800 !important;
            color: #111827 !important;
            margin-bottom: 4px !important;
        }
        [class*="st-key-choice_seg_"] button {
            min-height: 42px !important;
            font-size: 20px !important;
            font-weight: 800 !important;
        }
        [class*="st-key-choice_seg_"] button p {
            font-size: 20px !important;
            line-height: 1 !important;
        }
        @media (max-width: 640px) {
            [class*="st-key-employee_answer_grid"] {
                max-width: 100% !important;
                overflow-x: hidden !important;
            }
            [class*="st-key-choice_seg_"] {
                margin-bottom: 8px;
            }
            [class*="st-key-choice_seg_"] label p {
                font-size: 15px !important;
            }
            [class*="st-key-choice_seg_"] button {
                min-height: 40px !important;
                font-size: 19px !important;
                padding-left: 0.65rem !important;
                padding-right: 0.65rem !important;
            }
            [class*="st-key-choice_seg_"] button p {
                font-size: 19px !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # LINEの日程回答に近い、スマホ向けの1日1行形式で入力
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    paid_leave_default_value = 0
    if user_key not in st.session_state.user_prefs:
        initial_prefs = {d: "○" for d in range(1, days_in_month + 1)}
        if existing_submission:
            paid_leave_default_value = int(existing_submission.get("paid_leave_days", 0) or 0)
            for d in existing_submission.get("off_request_days", []):
                if 1 <= int(d) <= days_in_month:
                    initial_prefs[int(d)] = "×"
            for d in existing_submission.get("flexible_off_days", []):
                if 1 <= int(d) <= days_in_month and initial_prefs.get(int(d)) != "×":
                    initial_prefs[int(d)] = "△"
            st.session_state.setdefault(
                free_text_key, existing_submission.get("note", "") or "",
            )
        st.session_state.user_prefs[user_key] = initial_prefs
    elif existing_submission:
        paid_leave_default_value = int(existing_submission.get("paid_leave_days", 0) or 0)

    prefs = st.session_state.user_prefs[user_key]
    if existing_submission and not st.session_state.get(done_key):
        st.info(
            f"この月はすでに提出済みです"
            f"（{existing_submission.get('submitted_at', '')[:19].replace('T', ' ')}）。"
            "内容を変更したい場合は、この画面で修正して再提出してください。"
        )

    # 当該従業員の月間基準日数を取得
    _emp_obj = None
    try:
        from prototype.employees import get_employee as _get_emp
        _emp_obj = _get_emp(selected)
        annual_target = _emp_obj.annual_target_days
    except Exception:
        annual_target = None

    if annual_target:
        monthly_target = get_monthly_work_target(
            selected,
            target_month,
            annual_target,
        )
        base_holidays = days_in_month - monthly_target
    else:
        monthly_target = None
        base_holidays = None

    def _save_employee_preferences(paid_leave_days_value: int, free_text_value: str) -> Path:
        backup = ShiftBackup()
        off_requests = {selected: [d for d, m in prefs.items() if m == "×"]}
        work_requests = []
        if _emp_obj and getattr(_emp_obj, "only_on_request_days", False):
            work_requests = [
                (selected, d, None)
                for d, m in prefs.items()
                if m == "○"
            ]
        flexible_days = [d for d, m in prefs.items() if m == "△"]
        natural_language_notes = {selected: free_text_value}
        save_path = backup.save_preferences(
            year=target_year, month=target_month,
            off_requests=off_requests,
            work_requests=work_requests,
            flexible_off=[(selected, flexible_days, len(flexible_days) // 2)] if flexible_days else [],
            natural_language_notes=natural_language_notes,
            author=selected,
        )
        try:
            import json
            with open(save_path, encoding="utf-8") as f:
                _data = json.load(f)
            _data["paid_leave_days"] = int(paid_leave_days_value)
            _data["monthly_target_workdays"] = monthly_target
            _data["base_holidays"] = base_holidays
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        try:
            from prototype.github_backup import push_preference_to_github
            push_preference_to_github(
                save_path, employee_name=selected,
                year=target_year, month=target_month,
            )
        except Exception:
            pass
        return save_path

    if st.session_state.get(review_key):
        paid_leave_days_review = int(st.session_state.get(paid_leave_key, 0) or 0)
        free_text_review = st.session_state.get(free_text_key, "")
        x_days = [d for d, m in prefs.items() if m == "×"]
        triangle_days = [d for d, m in prefs.items() if m == "△"]
        ok_days = [d for d, m in prefs.items() if m == "○"]

        st.markdown("### 提出前の確認")
        st.warning(
            "この画面で内容を確認してください。"
            "× を付けた日は、例外なく「休み希望（絶対）」として扱われます。"
        )
        review_rows = [
            {"項目": "× 休み希望（絶対）", "内容": format_day_list(x_days), "日数": len(x_days)},
            {"項目": "△ できれば休み", "内容": format_day_list(triangle_days), "日数": len(triangle_days)},
            {"項目": "○ 出勤可能", "内容": format_day_list(ok_days), "日数": len(ok_days)},
            {"項目": "希望有給日数", "内容": f"{paid_leave_days_review}日", "日数": paid_leave_days_review},
            {"項目": "自由記述", "内容": free_text_review.strip() or "なし", "日数": ""},
        ]
        render_scrollable_review_table(review_rows)

        confirm_col1, confirm_col2 = st.columns([1, 1])
        with confirm_col1:
            if st.button("入力に戻る", key="emp_review_back", width="stretch"):
                st.session_state[review_key] = False
                st.rerun()
        with confirm_col2:
            if st.button("この内容で提出する", key="emp_review_submit", type="primary", width="stretch"):
                _save_employee_preferences(paid_leave_days_review, free_text_review)
                st.session_state[review_key] = False
                st.session_state[done_key] = {
                    "submitted_at": datetime.now().isoformat(),
                    "x_days": x_days,
                    "triangle_days": triangle_days,
                    "ok_days": ok_days,
                    "paid_leave_days": paid_leave_days_review,
                    "free_text": free_text_review,
                }
                st.session_state.pop(edit_existing_key, None)
                st.rerun()
        st.stop()

    # ここはStreamlitボタンで描画する。リンクではないため別タブ遷移は発生しない。
    with st.container(border=True, key="employee_answer_grid"):
        st.markdown(
            '<div class="employee-answer-title">日程回答</div>',
            unsafe_allow_html=True,
        )
        for d in range(1, days_in_month + 1):
            wd = weekday_jp[date(target_year, target_month, d).weekday()]
            current = prefs.get(d, "○")
            selected_mark = st.segmented_control(
                f"{target_month}.{d}({wd})",
                ["○", "△", "×"],
                default=current,
                key=f"choice_seg_{target_year}_{target_month}_{d}",
                width="stretch",
            )
            if selected_mark and selected_mark != current:
                prefs[d] = selected_mark
                st.rerun()

    # ============================================================
    # 希望有給日数の入力（任意）
    # ============================================================
    st.markdown("---")
    st.subheader("🏖 希望有給日数（任意）")

    # 当該従業員の月間基準日数を取得
    try:
        from prototype.employees import get_employee as _get_emp
        _emp_obj = _get_emp(selected)
        annual_target = _emp_obj.annual_target_days
    except Exception:
        annual_target = None

    if annual_target:
        monthly_target = get_monthly_work_target(
            selected,
            target_month,
            annual_target,
        )
        base_holidays = days_in_month - monthly_target
        st.caption(
            f"💡 **{selected}さんの今月の基準**: 勤務 {monthly_target}日 / 休み {base_holidays}日"
            f"（{target_year}年{target_month}月は{days_in_month}日間）\n\n"
            f"基準より多く休みたい場合は、その差分（=有給で消化したい日数）を入力してください。"
            f"例：休みを{base_holidays+1}日にしたい場合は「1」と入力。"
        )
    else:
        monthly_target = None
        base_holidays = None
        st.caption(
            f"💡 基準日数が設定されていない方は、希望有給日数を入力する必要はありません。"
        )

    paid_leave_days = st.number_input(
        "希望有給日数（任意）",
        min_value=0, max_value=31, value=paid_leave_default_value, step=1,
        help="今月使いたい有給日数を入力してください。基準より多く休みたい時のみ。",
        key=paid_leave_key,
    )

    # 現在の入力状況を集計
    x_count = sum(1 for m in prefs.values() if m == "×")
    triangle_count = sum(1 for m in prefs.values() if m == "△")
    ok_count = sum(1 for m in prefs.values() if m == "○")

    if base_holidays is not None:
        # 「希望休日合計」のリアルタイム表示
        total_holidays = base_holidays + paid_leave_days

        # ケース1: 有給入力済み → 補足情報表示
        if paid_leave_days > 0:
            st.info(
                f"📝 希望休日合計: **{total_holidays}日**"
                f"（基準{base_holidays}日 + 有給{paid_leave_days}日）"
            )

        # ケース2: ×日数 > 基準休日数 + 有給日数 → 警告（有給忘れの可能性）
        if x_count > total_holidays:
            shortage = x_count - total_holidays
            if paid_leave_days == 0:
                # 有給日数を全く入力していない
                st.warning(
                    f"⚠ **有給日数の入力をお忘れではありませんか？**\n\n"
                    f"現在の状況:\n"
                    f"- ×（休み希望）: **{x_count}日**\n"
                    f"- 今月の基準休日数: **{base_holidays}日**\n"
                    f"- 希望有給日数: **0日**\n\n"
                    f"基準より **{shortage}日多く** 休みを希望されています。"
                    f"基準を超える分は有給で消化する必要があるため、"
                    f"上の **「希望有給日数」** に **{shortage}** を入力してください。\n\n"
                    f"💡 もし基準より多く休みたいわけでなければ、× の数を{base_holidays}日に減らしてください。"
                )
            else:
                # 有給は入れているが、まだ足りない
                st.warning(
                    f"⚠ **有給日数が不足しています**\n\n"
                    f"現在の状況:\n"
                    f"- ×（休み希望）: **{x_count}日**\n"
                    f"- 今月の基準休日数: **{base_holidays}日**\n"
                    f"- 希望有給日数: **{paid_leave_days}日**\n"
                    f"- 合計許容休日: **{total_holidays}日**\n\n"
                    f"× の数（{x_count}日）が、基準＋有給（{total_holidays}日）を**{shortage}日超えています**。\n"
                    f"以下のいずれかをご検討ください：\n"
                    f"- 上の「希望有給日数」を **{paid_leave_days + shortage}** に増やす\n"
                    f"- × の数を{total_holidays}日に減らす"
                )

        # ケース3: ×日数 < 基準休日数（基準より働きたい）
        elif x_count < base_holidays and paid_leave_days == 0 and x_count > 0:
            st.caption(
                f"📊 現在 ×（休み希望）{x_count}日 / 基準休日{base_holidays}日。"
                f"基準内に収まっています。"
            )

        # ケース4: ×日数 > 基準＋有給 ではないが、有給だけ入れている（過剰申請）
        elif paid_leave_days > 0 and x_count <= base_holidays:
            st.warning(
                f"⚠ **有給日数が多すぎる可能性があります**\n\n"
                f"× で休み希望にしている日（{x_count}日）が "
                f"基準休日数（{base_holidays}日）以下です。\n"
                f"有給を申請する必要は通常ありません（基準内なら有給を使わずに休めます）。\n\n"
                f"もし{base_holidays + paid_leave_days}日休みたいのであれば、"
                f"× の数を{base_holidays + paid_leave_days}日に増やしてください。"
            )

    st.markdown("---")
    st.subheader("自由記述（任意）")
    st.caption(
        "シフト作成時に考慮してほしい点があれば、下の書き方に合わせてご記入ください。"
        "日付と内容がはっきりしているほど反映されやすくなります。"
    )
    st.info(
        "**おすすめの書き方**\n\n"
        "日付を入れて、何を希望しているかを短く書いてください。\n\n"
        "```\n"
        "22日 出勤希望\n"
        "6日 赤羽出勤希望\n"
        "1日 すずらん出勤希望\n"
        "3日か29日のいずれか1日は出勤したい\n"
        "16日・17日のどちらか1日休み希望\n"
        "23日・24日のどちらか1日休み希望\n"
        "有給2日利用で、合計9日休み希望\n"
        "4連休希望\n"
        "```"
    )
    st.warning(
        "**ダメな例（反映しづらい書き方）**\n\n"
        "```\n"
        "月末あたり休みたいです\n"
        "どこかで連休ください\n"
        "なるべく赤羽がいいです\n"
        "出られる日はあります\n"
        "いい感じにお願いします\n"
        "```"
    )
    free_text = st.text_area(
        "自然言語で希望を書いてください",
        placeholder=(
            "例:\n"
            "22日 出勤希望\n"
            "3日か29日のいずれか1日は出勤したい\n"
            "16日・17日のどちらか1日休み希望\n"
            "有給2日利用で、合計9日休み希望"
        ),
        height=120,
        key=free_text_key,
    )

    if st.button(
        f"確認画面へ進む",
        type="primary",
        width="stretch",
    ):
        st.session_state[review_key] = True
        st.rerun()


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
            short_day_text = ", ".join(f"{shift.month}/{d}" for d in short_days)
            st.write(f"人員少マーク日: {short_day_text}")
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
        setting_tab3, setting_tab_leave, setting_tab4, setting_tab5,
    ) = st.tabs([
        "🔗 マジックリンク",
        "🔧 ルール設定", "📜 ルール変更履歴",
        "👥 従業員マスタ", "🏖 有給使用状況", "🔑 APIキー", "💾 バックアップ",
    ])

    # ============================================================
    # タブ: 有給使用状況（経営者専用）
    # ============================================================
    with setting_tab_leave:
        st.markdown("### 🏖 有給使用状況（経営者のみ閲覧可能）")
        st.caption(
            "本人が提出した希望有給と、管理者が後から付けた有給調整を合算して確認できます。"
        )

        with st.container(border=True):
            st.markdown("#### 管理者側で有給調整を追加")
            st.caption(
                "出勤希望だったパート・アルバイトを調整上休みにした場合などに使います。"
                "本人の提出内容は上書きせず、管理者調整として別に記録します。"
            )
            adj_col1, adj_col2, adj_col3, adj_col4 = st.columns([1, 1, 1.4, 1])
            with adj_col1:
                adj_year = st.number_input(
                    "年",
                    min_value=2020,
                    max_value=2100,
                    value=int(st.session_state.get("target_year", date.today().year)),
                    step=1,
                    key="admin_leave_year",
                )
            with adj_col2:
                adj_month = st.number_input(
                    "月",
                    min_value=1,
                    max_value=12,
                    value=int(st.session_state.get("target_month", date.today().month)),
                    step=1,
                    key="admin_leave_month",
                )
            part_time_names = [
                e.name for e in get_all_employees_including_retired()
                if e.employment_status == EmploymentStatus.PART_TIME
            ]
            employee_options = part_time_names + [
                e.name for e in get_all_employees_including_retired()
                if e.name not in part_time_names
                and e.employment_status in (EmploymentStatus.ACTIVE, EmploymentStatus.PART_TIME)
            ]
            with adj_col3:
                adj_employee = st.selectbox(
                    "従業員",
                    options=employee_options,
                    key="admin_leave_employee",
                )
            with adj_col4:
                adj_days = st.number_input(
                    "有給日数",
                    min_value=1,
                    max_value=31,
                    value=1,
                    step=1,
                    key="admin_leave_days",
                )
            adj_col5, adj_col6 = st.columns([1, 2])
            with adj_col5:
                adj_dates_text = st.text_input(
                    "対象日（任意）",
                    placeholder="例: 26",
                    key="admin_leave_dates",
                )
            with adj_col6:
                adj_reason = st.text_input(
                    "理由",
                    placeholder="例: 出勤希望だったが調整上休み。有給消化で合意",
                    key="admin_leave_reason",
                )
            if st.button("管理者有給調整を追加", type="primary", key="admin_leave_add"):
                days_in_selected_month = monthrange(int(adj_year), int(adj_month))[1]
                dates = parse_day_list_text(adj_dates_text, days_in_selected_month)
                add_admin_paid_leave_adjustment(
                    int(adj_year),
                    int(adj_month),
                    str(adj_employee),
                    int(adj_days),
                    dates=dates,
                    reason=adj_reason or "管理者による有給調整",
                    actor="管理者",
                )
                st.success("管理者有給調整を追加しました。")
                st.rerun()

        import json as _json
        from collections import defaultdict
        backup_dir = BACKUP_DIR
        admin_leave_data = load_admin_paid_leave_data()

        # 月別・従業員別の有給集計
        # data[ym][employee] = {paid_leave_days, submitted_at, base_holidays, ...}
        leave_data: dict[str, dict[str, dict]] = defaultdict(dict)
        if backup_dir.exists():
            for month_dir in sorted(backup_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                ym = month_dir.name  # "2026-05"
                # 従業員ごとに最新の提出を記録
                for f in sorted(month_dir.glob("preferences_*.json")):
                    try:
                        with open(f, encoding="utf-8") as fp:
                            d = _json.load(fp)
                        author = d.get("author", "")
                        if not author or author == "system":
                            continue
                        saved_at = d.get("saved_at", "")
                        # 最新のもののみ採用
                        if (author not in leave_data[ym]
                                or saved_at > leave_data[ym][author].get("saved_at", "")):
                            submitted_paid_leave_days = int(d.get("paid_leave_days", 0))
                            leave_data[ym][author] = {
                                "submitted_paid_leave_days": submitted_paid_leave_days,
                                "admin_paid_leave_days": 0,
                                "admin_paid_leave_dates": [],
                                "admin_paid_leave_reasons": [],
                                "paid_leave_days": submitted_paid_leave_days,
                                "monthly_target_workdays": d.get("monthly_target_workdays"),
                                "base_holidays": d.get("base_holidays"),
                                "saved_at": saved_at,
                            }
                    except Exception:
                        continue

        for adj in admin_leave_data.get("adjustments", []):
            try:
                adj_year = int(adj.get("year"))
                adj_month = int(adj.get("month"))
                emp = str(adj.get("employee", "")).strip()
                days = int(adj.get("days", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not emp or days <= 0:
                continue
            ym = f"{adj_year:04d}-{adj_month:02d}"
            info = leave_data[ym].setdefault(emp, {
                "submitted_paid_leave_days": 0,
                "admin_paid_leave_days": 0,
                "admin_paid_leave_dates": [],
                "admin_paid_leave_reasons": [],
                "paid_leave_days": 0,
                "monthly_target_workdays": None,
                "base_holidays": None,
                "saved_at": "",
            })
            info["admin_paid_leave_days"] = int(info.get("admin_paid_leave_days", 0) or 0) + days
            info["paid_leave_days"] = (
                int(info.get("submitted_paid_leave_days", 0) or 0)
                + int(info.get("admin_paid_leave_days", 0) or 0)
            )
            info.setdefault("admin_paid_leave_dates", []).extend(adj.get("dates", []) or [])
            if adj.get("reason"):
                info.setdefault("admin_paid_leave_reasons", []).append(str(adj.get("reason")))

        if not leave_data:
            st.info("まだ提出データがありません。従業員が希望を提出すると集計が表示されます。")
        else:
            # 月選択
            available_months = sorted(leave_data.keys(), reverse=True)
            selected_ym = st.selectbox(
                "表示する月",
                options=["すべて"] + available_months,
                index=1 if available_months else 0,
            )

            display_months = available_months if selected_ym == "すべて" else [selected_ym]

            for ym in display_months:
                # 月内サマリー
                month_data = leave_data[ym]
                total_paid_leave = sum(
                    info["paid_leave_days"] for info in month_data.values()
                )
                total_users = sum(
                    1 for info in month_data.values() if info["paid_leave_days"] > 0
                )

                st.markdown(f"#### 📅 {ym}")
                # サマリー
                lc1, lc2, lc3 = st.columns(3)
                with lc1:
                    st.metric("提出済み従業員数", f"{len(month_data)} 名")
                with lc2:
                    st.metric("有給申請者数", f"{total_users} 名")
                with lc3:
                    st.metric("月間有給合計", f"{total_paid_leave} 日")

                    # 従業員別テーブル
                    table_data = []
                    for emp, info in sorted(month_data.items()):
                        table_data.append({
                            "氏名": emp,
                            "本人希望": info.get("submitted_paid_leave_days", 0),
                            "管理者調整": info.get("admin_paid_leave_days", 0),
                            "有給合計": info["paid_leave_days"],
                            "調整日": format_day_list(info.get("admin_paid_leave_dates", [])),
                            "基準勤務日数": info.get("monthly_target_workdays") or "-",
                            "基準休日数": info.get("base_holidays") or "-",
                            "希望休日合計": (
                                (info.get("base_holidays") or 0) + info["paid_leave_days"]
                                if info.get("base_holidays") is not None else "-"
                            ),
                            "提出日時": info["saved_at"][:19].replace("T", " ") if info["saved_at"] else "-",
                        })
                    st.dataframe(table_data, width="stretch", hide_index=True)

                    # 有給を申請している人だけ強調表示
                    applicants = [
                        (emp, info) for emp, info in month_data.items()
                        if info["paid_leave_days"] > 0
                    ]
                    if applicants:
                        st.markdown("**🏖 有給申請者の詳細**")
                        for emp, info in applicants:
                            admin_note = ""
                            if info.get("admin_paid_leave_days", 0):
                                date_label = format_day_list(info.get("admin_paid_leave_dates", []))
                                admin_note = f" / 管理者調整 {info['admin_paid_leave_days']}日"
                                if date_label:
                                    admin_note += f"（{date_label}）"
                            st.markdown(
                                f'<div style="background:#fef3c7; padding:8px 12px; '
                                f'margin:4px 0; border-radius:6px; border-left:3px solid #f59e0b;">'
                                f'<strong>{emp}</strong>: 有給 {info["paid_leave_days"]}日{admin_note} '
                                f'（基準休{info.get("base_holidays") or "?"}日 + 有給{info["paid_leave_days"]}日 '
                                f'= 合計 {(info.get("base_holidays") or 0) + info["paid_leave_days"]}日休み希望）'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                st.markdown("---")

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

            # 在籍中の従業員 + 山本さん（補助・特別枠）
            active_emps = _link_active_emps()
            display_emps = [e for e in active_emps if not e.is_auxiliary]
            try:
                yamamoto_emp = get_employee("山本")
                if yamamoto_emp.name not in {e.name for e in display_emps}:
                    display_emps.append(yamamoto_emp)
            except Exception:
                pass

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
                width="stretch",
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
        check_labels = {
            "store_capacity": "店舗別必要人数",
            "eco_required": "東口・西口の必須エコ要員",
            "consec_work": "最大連勤チェック",
            "holiday_days": "月内最低休日数",
            "consec_off_3": "3連休の確認",
            "two_off_per_month": "月内 2連休回数（最低1回・最大2回）",
            "off_request": "休み希望厳守",
            "work_request": "出勤希望・希望店舗の考慮",
            "omiya_anchor": "大宮アンカー（春山 or 下地必須）",
            "higashi_monday": "東口の月曜休店",
            "omiya_short_warning": "大宮人数少（エコ1名運営）の警告表示",
        }

        param_specs = {
            "max_consec_work": {
                "label": "最大連勤日数（ハード上限）",
                "min": 1, "max": 10, "default": 5,
                "help": "この日数を超える連勤はエラーになります。推奨: 4〜7日",
                "safe": (4, 7),
            },
            "soft_consec_threshold": {
                "label": "推奨連勤上限（ソフト・ペナルティ閾値）",
                "min": 1, "max": 10, "default": 4,
                "help": "この日数を超えるとシフト生成時にペナルティ加算（できる限り回避）。推奨: 3〜5日",
                "safe": (3, 5),
            },
            "default_holiday_days": {
                "label": "既定の月内最低休日数",
                "min": 0, "max": 15, "default": 8,
                "help": "個別オーバーライドがない従業員の月内必要休日数。推奨: 6〜10日",
                "safe": (6, 10),
            },
            "min_2off_per_month": {
                "label": "2連休 月内最低回数",
                "min": 0, "max": 10, "default": 1,
                "help": "推奨: 0〜2回",
                "safe": (0, 2),
            },
            "max_2off_per_month": {
                "label": "2連休 月内最大回数",
                "min": 0, "max": 10, "default": 2,
                "help": "推奨: 1〜4回",
                "safe": (1, 4),
            },
            "solver_seed": {
                "label": "ソルバーシード",
                "min": 0, "max": 999999, "default": 42,
                "help": "同じ入力に対して毎回同じシフトを生成するためのシード値。別のパターンを試したい時は数値を変えてください。",
                "safe": None,
            },
            "solver_time_limit_seconds": {
                "label": "ソルバー最大実行時間（秒）",
                "min": 10, "max": 600, "default": 180,
                "help": "シフト生成に使う最大秒数です。",
                "safe": None,
            },
        }

        def _clone_custom_rule(rule: CustomRule) -> CustomRule:
            return CustomRule(
                id=rule.id,
                name=rule.name,
                description=rule.description,
                enabled=rule.enabled,
                severity=rule.severity,
                created_at=rule.created_at,
                created_by=rule.created_by,
                target_year=rule.target_year,
                target_month=rule.target_month,
                rule_type=rule.rule_type,
                employee=rule.employee,
                stores=list(rule.stores),
                count=rule.count,
                comparison=getattr(rule, "comparison", "min"),
            )

        def _sync_rule_widgets(source_cfg: RuleConfig) -> None:
            for key in check_labels:
                st.session_state[f"chk_{key}"] = source_cfg.enabled_checks.get(key, True)
            for key, spec in param_specs.items():
                st.session_state[f"param_{key}"] = int(
                    source_cfg.parameters.get(key, spec["default"])
                )
            for rule in cfg.custom_rules:
                st.session_state[f"rule_en_{rule.id}"] = rule.enabled
            st.session_state["rule_added_custom_rules"] = []
            st.session_state["rule_deleted_ids"] = []
            st.session_state["rule_apply_confirm"] = False

        cfg_signature = repr(cfg.to_dict())
        discard_requested = st.session_state.pop("rule_discard_requested", False)
        default_draft_requested = st.session_state.pop("rule_default_draft_requested", False)
        if default_draft_requested:
            _sync_rule_widgets(RuleConfig())
            st.session_state["rule_deleted_ids"] = [r.id for r in cfg.custom_rules]
            st.session_state["rule_cfg_loaded_sig"] = cfg_signature
        elif discard_requested or st.session_state.get("rule_cfg_loaded_sig") != cfg_signature:
            _sync_rule_widgets(cfg)
            st.session_state["rule_cfg_loaded_sig"] = cfg_signature

        st.caption(
            "この画面は2段階です。値を変えた段階では **仮設定**、"
            "下の確認で本変更を適用するまで **本設定** には保存されません。"
        )
        if cfg.updated_at:
            st.info(
                f"現在の本設定: {cfg.updated_at[:16].replace('T', ' ')} 更新"
                f" / 更新者: {cfg.updated_by or '不明'}"
            )
        else:
            st.info("現在の本設定: デフォルト設定")

        st.markdown("#### ルール台帳 v1.0")
        st.caption(
            "現時点でこのフォルダ上にある固定ルールを、"
            "絶対条件・強い目標・弱い目標・月別例外に分類して保存しています。"
        )

        def _rule_row(
            category: str,
            name: str,
            detail: str,
            generation: str,
            validation: str,
            display: str,
            editable: str,
            status: str,
            note: str = "",
        ) -> dict:
            return {
                "分類": category,
                "ルール": name,
                "内容": detail,
                "生成": generation,
                "検証": validation,
                "画面/出力": display,
                "編集": editable,
                "状態": status,
                "メモ": note,
            }

        rule_inventory = [
            _rule_row(
                "店舗・人数", "赤羽駅前店の基本体制",
                "基本はエコ対応1名以上+合計3名。エコ担当はチケット対応も可。チケット対応不足時は山本さん補助。",
                "反映中", "反映中", "人員少表示・Excel/PDFに反映",
                "コード固定", "反映中",
            ),
            _rule_row(
                "店舗・人数", "赤羽東口店の1名体制",
                "月曜定休。エコ1名のみ。例外なし。土井さんメイン、休みの日は他エコが代替。",
                "反映中", "反映中", "2名以上はエラー表示",
                "コード固定", "反映中",
            ),
            _rule_row(
                "店舗・人数", "大宮駅前店の基本体制",
                "通常はエコ対応1名以上+合計3名。エコ担当はチケット対応も可。不足時だけ2名体制を警告扱い。",
                "反映中", "反映中", "人員少表示・Excel/PDFに反映",
                "コード固定", "反映中",
                "春山さん・下地さんのどちらか必須。",
            ),
            _rule_row(
                "店舗・人数", "大宮西口店の1名体制",
                "原則エコ1名のみ。楯さんメイン。人数余り・研修・チケット補助で追加配置の余地あり。",
                "反映中", "反映中", "人数不足表示に反映",
                "コード固定", "反映中",
            ),
            _rule_row(
                "店舗・人数", "大宮すずらん通り店の基本体制",
                "エコ対応1名以上+合計3名。エコ担当はチケット対応も可。チケット専任は多すぎないよう調整。",
                "反映中", "反映中", "人数不足表示に反映",
                "コード固定", "反映中",
            ),
            _rule_row(
                "店舗・営業日", "営業モード",
                "通常・省人員・最小営業・営業停止を月日から自動判定。",
                "反映中", "反映中", "シフト表・検証に反映",
                "コード固定", "反映中",
                "祝日連携は簡易判定のため、将来拡張候補。",
            ),
            _rule_row(
                "連勤・休日", "最大連勤",
                f"現在の本設定: 最大{cfg.parameters.get('max_consec_work', 5)}連勤。",
                "反映中", "反映中", "検証結果に表示",
                "この画面で変更可", "反映中",
            ),
            _rule_row(
                "連勤・休日", "推奨連勤上限",
                f"現在の本設定: {cfg.parameters.get('soft_consec_threshold', 4)}連勤超を避けたい設定。",
                "未接続", "未接続", "設定保存のみ",
                "この画面で変更可", "未接続",
                "生成側は現在コード内の基準値を使っています。",
            ),
            _rule_row(
                "連勤・休日", "前月末から月初の連勤",
                "前月末の連勤日数を当月月初に引き継いで判定。",
                "一部反映", "一部反映", "検証結果に表示",
                "データ連携待ち", "一部反映",
                "2026年5月サンプルでは反映。本番提出データでは前月データ取得が未整備。",
            ),
            _rule_row(
                "連勤・休日", "既定の月内最低休日数",
                f"現在の本設定: {cfg.parameters.get('default_holiday_days', 8)}日。",
                "反映中", "反映中", "検証結果に表示",
                "この画面で変更可", "反映中",
                "個別指定がないスタッフの最低休日数として使います。",
            ),
            _rule_row(
                "連勤・休日", "2連休の最低・最大回数",
                f"現在の本設定: 最低{cfg.parameters.get('min_2off_per_month', 1)}回 / 最大{cfg.parameters.get('max_2off_per_month', 2)}回。",
                "コード固定", "コード固定", "警告として表示",
                "この画面で変更可", "未接続",
                "原則ルール。例外的に0回・2回以上もあり得るため警告扱いです。",
            ),
            _rule_row(
                "連勤・休日", "3連休の確認",
                "原則避けるが、人員過多や本人希望がある場合は許容。",
                "ソフト反映", "参考情報", "提出一覧・入力サマリに表示",
                "コード固定", "一部反映",
            ),
            _rule_row(
                "希望・提出", "休み希望厳守",
                "提出された×は例外なく勤務にしない。出勤希望と重なった場合も×を優先。",
                "反映中", "反映中", "検証結果に表示",
                "提出データ依存", "反映中",
            ),
            _rule_row(
                "希望・提出", "出勤希望・希望店舗の考慮",
                "出勤希望はできる限り出勤にし、出勤になった場合は希望店舗を優先する。調整で休み・別店舗になる場合あり。",
                "ソフト反映", "警告表示", "検証結果に表示",
                "提出データ依存", "反映中",
            ),
            _rule_row(
                "希望・提出", "柔軟休み希望",
                "候補日のうち指定日数を休みにする。",
                "反映中", "未接続", "提出内容に反映",
                "提出データ依存", "一部反映",
            ),
            _rule_row(
                "スタッフ別", "配置不可店舗",
                "従業員マスタの不可店舗には配置しない。",
                "反映中", "未接続", "従業員マスタで管理",
                "従業員マスタ", "一部反映",
                "生成では効きますが、手動・AI変更後の検証は今後強化余地あり。",
            ),
            _rule_row(
                "スタッフ別", "赤羽東口店の代替要員",
                "土井さんメイン。休みの日は楯さん・春山さん・長尾さん・今津さんのいずれか。",
                "反映中", "警告表示", "検証結果に表示",
                "コード固定", "反映中",
                "牧野さんの東口1名体制は当面対象外です。",
            ),
            _rule_row(
                "スタッフ別", "固定店舗",
                "店舗固定は土井さん（赤羽東口）・下地さん（大宮駅前）の2名のみ。",
                "反映中", "一部反映", "従業員マスタで管理",
                "従業員マスタ", "反映中",
            ),
            _rule_row(
                "スタッフ別", "メイン店舗以外への月3日勤務",
                "楯さん・春山さん・長尾さんは月3日、自分のメイン店舗以外で勤務する。",
                "反映中", "反映中", "検証結果に表示",
                "コード固定", "反映中",
            ),
            _rule_row(
                "スタッフ別", "在勤要望（強・中・弱）",
                "強・中・弱を生成時の重みとして使う。春山さんの西口代替・研修も弱として反映。",
                "一部反映", "未接続", "従業員マスタで管理",
                "従業員マスタ", "一部反映",
                "割合の厳密チェックではなく、現状は配置の好みとして扱います。",
            ),
            _rule_row(
                "スタッフ別", "南さんの出勤希望日のみ稼働",
                "提出画面で `○` にした日だけを出勤候補として扱う。`×` と `△` の日は配置しない。",
                "対象外", "一部反映", "従業員マスタで管理",
                "従業員マスタ", "反映中",
            ),
            _rule_row(
                "スタッフ別", "大塚さんの5月10日勤務",
                "2026年5月のみ、月10日勤務に固定。最大連勤はチェック対象。",
                "反映中", "一部反映", "検証結果に表示",
                "コード固定", "反映中",
            ),
            _rule_row(
                "スタッフ別", "山本さん補助ロジック",
                "赤羽駅前店のチケット対応が不足する時だけ補助配置。その他は手動入力対象。",
                "反映中", "反映中", "シフト表では空白/赤羽補助として表示",
                "コード固定", "反映中",
            ),
            _rule_row(
                "月次ルール", "月ごとの追加条件",
                "研修・一時的な店舗移動・配置禁止など、その月だけの条件を追加する。",
                "反映中", "反映中", "カスタムルールに保存",
                "この画面で変更可", "反映中",
                "最低回数・最大回数・ちょうど回数・配置禁止を入力できます。",
            ),
            _rule_row(
                "スタッフ別", "すずらん不在時の補填",
                "野澤さん不在時に岩野さんまたは大類さんで補填する考え方。",
                "未接続", "未接続", "メモのみ",
                "コード固定", "未接続",
            ),
            _rule_row(
                "AI・手動変更", "AI対話のプレビュー変更",
                "AIの変更はまずプレビュー表示し、本シフト反映はボタンで確定。",
                "対象外", "一部反映", "AI対話画面に表示",
                "画面操作", "反映中",
            ),
            _rule_row(
                "AI・手動変更", "AI対話中の検証",
                "プレビュー状態のシフトを検証してエラー・警告を表示。",
                "対象外", "一部反映", "AI対話画面に表示",
                "コード固定", "一部反映",
                "希望休・前月連勤などは、生成時の入力がある場合だけ反映。",
            ),
            _rule_row(
                "設定画面", "検証チェック ON/OFF",
                "店舗人数・連勤・休日などのON/OFFを設定ファイルに保存。",
                "未接続", "未接続", "設定画面に表示",
                "この画面で変更可", "未接続",
                "細分化して実処理へ接続していく候補です。",
            ),
            _rule_row(
                "設定画面", "カスタムルール",
                "メモ保存に加えて、月別の指定店舗回数ルールは生成・検証に反映する。",
                "一部反映", "一部反映", "ルール変更履歴に記録",
                "この画面で変更可", "一部反映",
            ),
        ]
        ledger = load_rule_ledger_v1()
        rule_inventory = list(ledger.get("rules", []))
        employee_suitability_rows = list(ledger.get("employee_store_suitability", []))
        numeric_ledger_rows = list(ledger.get("numeric_parameters", []))
        if not employee_suitability_rows:
            employee_suitability_rows = build_employee_suitability_rows_from_master()
        if not numeric_ledger_rows:
            numeric_ledger_rows = build_numeric_ledger_rows_from_parameters(cfg.parameters)

        category_counts = {}
        for row in rule_inventory:
            category_counts[row["分類"]] = category_counts.get(row["分類"], 0) + 1
        stat_cols = st.columns(4)
        stat_cols[0].metric("絶対条件", category_counts.get("絶対条件", 0))
        stat_cols[1].metric("強い目標", category_counts.get("強い目標", 0))
        stat_cols[2].metric("弱い目標", category_counts.get("弱い目標", 0))
        stat_cols[3].metric("月別例外", category_counts.get("月別例外", 0))

        category_order = ["絶対条件", "強い目標", "弱い目標", "月別例外"]
        existing_categories = {row.get("分類", "") for row in rule_inventory}
        categories = (
            ["すべて"]
            + [c for c in category_order if c in existing_categories]
            + sorted(existing_categories - set(category_order) - {""})
        )
        filter_col1, filter_col2, filter_col3, filter_col4 = st.columns([1.1, 2, 0.8, 0.8])
        with filter_col1:
            selected_rule_category = st.selectbox(
                "分類で絞り込み",
                categories,
                key="rule_inventory_category",
            )
        with filter_col2:
            rule_search_input = st.text_input(
                "キーワード検索",
                key="rule_inventory_search_input",
                placeholder="例: 牧野、赤羽、連勤",
            )
        with filter_col3:
            search_requested = st.button(
                "検索する",
                key="rule_inventory_search_button",
                width="stretch",
            )
        with filter_col4:
            clear_search_requested = st.button(
                "クリア",
                key="rule_inventory_clear_button",
                width="stretch",
            )

        if search_requested:
            st.session_state["rule_inventory_search_term"] = rule_search_input.strip()
        if clear_search_requested:
            st.session_state["rule_inventory_search_term"] = ""

        rule_search_normalized = st.session_state.get("rule_inventory_search_term", "")
        visible_rule_inventory = [
            row for row in rule_inventory
            if (selected_rule_category == "すべて" or row.get("分類") == selected_rule_category)
            and (
                not rule_search_normalized
                or rule_search_normalized in json.dumps(row, ensure_ascii=False)
            )
        ]
        if rule_search_normalized:
            st.caption(f"検索中: {rule_search_normalized}")
        st.caption(f"表示中: {len(visible_rule_inventory)}件 / 台帳全体: {len(rule_inventory)}件")
        st.dataframe(
            visible_rule_inventory,
            width="stretch",
            hide_index=True,
            height=360,
        )

        with st.expander("分類の見方", expanded=False):
            st.markdown(
                """
                - **絶対条件**: 破るとシフト生成できない、または検証ERRORになる条件です。
                - **強い目標**: できる限り守る条件です。無理な月は警告や候補表示に回ります。
                - **弱い目標**: 配置の好みや補助的な判断です。
                - **月別例外**: その月だけ追加・調整する条件です。
                """
            )
        with st.expander(
            f"従業員別の店舗適性（{len(employee_suitability_rows)}件）",
            expanded=True,
        ):
            if employee_suitability_rows:
                st.dataframe(
                    employee_suitability_rows,
                    width="stretch",
                    hide_index=True,
                    height=360,
                )
            else:
                st.warning("店舗適性を表示できません。従業員マスターと台帳ファイルを確認してください。")
        with st.expander(
            f"台帳に残した数値基準（{len(numeric_ledger_rows)}件）",
            expanded=True,
        ):
            if numeric_ledger_rows:
                st.dataframe(
                    numeric_ledger_rows,
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.warning("数値基準を表示できません。ルール設定ファイルを確認してください。")
        st.caption(
            f"ルール台帳 v{ledger.get('version', '1.0')} は "
            f"`config/rule_ledger_v1_0.json` にバックアップとして保存されています。"
        )

        st.markdown("---")
        draft_status_area = st.container()

        # 検証ON/OFFは実処理への接続が不完全だったため、画面からは外す。
        # 既存configとの互換性のため、保存値自体はそのまま保持する。
        new_enabled = dict(cfg.enabled_checks)

        # サブセクション: 数値パラメータ
        st.markdown("---")
        st.markdown("#### 🔢 数値パラメータ")
        st.warning(
            "⚠ **注意**: ここの値を極端な値に変えると、**シフトを生成できなくなる**ことがあります。"
            "推奨範囲外の入力には自動で警告が表示されます。"
            "困った時は「デフォルトを仮設定にする」ボタンを使ってください。"
        )

        param_col1, param_col2 = st.columns(2)
        new_params = dict(cfg.parameters)
        for key, spec in param_specs.items():
            new_params.setdefault(key, int(spec["default"]))

        def warn_if_unsafe(key: str, value: int) -> None:
            """値が推奨範囲外なら警告を表示"""
            safe_range = param_specs[key].get("safe")
            if safe_range:
                lo, hi = safe_range
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
            for key in ("max_consec_work", "soft_consec_threshold"):
                spec = param_specs[key]
                new_params[key] = int(st.number_input(
                    spec["label"],
                    min_value=spec["min"], max_value=spec["max"],
                    help=spec["help"],
                    key=f"param_{key}",
                ))
                warn_if_unsafe(key, new_params[key])
        with param_col2:
            for key in ("solver_time_limit_seconds", "solver_seed"):
                spec = param_specs[key]
                new_params[key] = int(st.number_input(
                    spec["label"],
                    min_value=spec["min"], max_value=spec["max"],
                    help=spec["help"],
                    key=f"param_{key}",
                ))
                warn_if_unsafe(key, new_params[key])

        # ソフト閾値 > ハード上限 はおかしい
        if new_params["soft_consec_threshold"] > new_params["max_consec_work"]:
            st.error(
                f"❌ 設定矛盾: 推奨連勤上限（{new_params['soft_consec_threshold']}）が"
                f"ハード上限（{new_params['max_consec_work']}）を超えています。"
                "推奨 ≤ ハード になるよう調整してください。"
            )

        # 月ごとの特別ルールは、実際に対象月を見ながら判断できる
        # シフト生成画面側へ一本化する。
        st.markdown("---")
        st.markdown("#### 📌 月ごとの特別ルール")
        st.info(
            "月ごとの研修・例外配置などは、各月のシフト生成画面にある"
            "「今月だけの特別ルール」で追加します。"
            "この画面では全体ルールと数値設定だけを扱います。"
        )
        new_custom_rules = [_clone_custom_rule(r) for r in cfg.custom_rules]

        # 保存・リセットボタン
        st.markdown("---")
        save_col1, save_col2 = st.columns([2, 3])
        with save_col1:
            save_actor = st.text_input("保存実行者名", value="代表取締役", key="cfg_actor")
            save_note = st.text_input("変更メモ（任意）", placeholder="例: 連勤上限を4に戻す")

        draft_cfg = RuleConfig(
            enabled_checks=new_enabled,
            parameters=new_params,
            custom_rules=new_custom_rules,
        )
        draft_changes = rule_mgr._compute_diff(
            cfg, draft_cfg, actor=save_actor, note=save_note,
        )
        has_setting_error = (
            new_params["soft_consec_threshold"] > new_params["max_consec_work"]
        )

        def _change_text(change) -> str:
            labels = {**check_labels, **{k: v["label"] for k, v in param_specs.items()}}
            if change.category == "custom_rule_add":
                return f"カスタムルール追加: {change.after.get('name', change.target)}"
            if change.category == "custom_rule_remove":
                return f"カスタムルール削除: {change.before.get('name', change.target)}"
            if change.category == "custom_rule_toggle":
                return f"カスタムルール切替: {change.target} {change.before} → {change.after}"
            return f"{labels.get(change.target, change.target)}: {change.before} → {change.after}"

        with draft_status_area:
            if draft_changes:
                st.warning(
                    f"仮変更が **{len(draft_changes)}件** あります。"
                    "まだ本設定には保存されていません。"
                )
                with st.expander("仮変更の内容を見る", expanded=False):
                    for change in draft_changes:
                        st.write(f"- {_change_text(change)}")
            else:
                st.success("本設定と画面上の値は一致しています。仮変更はありません。")

        with save_col2:
            if st.session_state.get("rule_apply_confirm") and draft_changes:
                st.warning(
                    "本変更を適用します。保存後はシフト生成・検証にこの設定が使われます。"
                )
                apply_col, cancel_col, reset_col = st.columns([1.2, 1, 1.4])
                with apply_col:
                    if st.button(
                        "本変更を適用",
                        type="primary",
                        width="stretch",
                        disabled=has_setting_error,
                    ):
                        changes = rule_mgr.save(draft_cfg, actor=save_actor, note=save_note)
                        if changes:
                            try:
                                from prototype.github_backup import push_config_to_github
                                push_config_to_github("rule_config", draft_cfg.to_dict())
                            except Exception:
                                pass
                        st.session_state["rule_apply_confirm"] = False
                        st.session_state["rule_added_custom_rules"] = []
                        st.session_state["rule_deleted_ids"] = []
                        if changes:
                            st.success(f"{len(changes)}件の変更を本設定に保存しました")
                        else:
                            st.info("変更点はありません")
                        st.rerun()
                with cancel_col:
                    if st.button("やめる", width="stretch"):
                        st.session_state["rule_apply_confirm"] = False
                        st.rerun()
                with reset_col:
                    if st.button("取り消す", width="stretch"):
                        st.session_state["rule_discard_requested"] = True
                        st.rerun()
            else:
                apply_col, discard_col, default_col = st.columns([1, 1, 1.4])
                with apply_col:
                    if st.button(
                        "反映する",
                        type="primary",
                        width="stretch",
                        disabled=(not draft_changes or has_setting_error),
                        help="仮設定を本設定として保存する前に確認します",
                    ):
                        st.session_state["rule_apply_confirm"] = True
                        st.rerun()
                with discard_col:
                    if st.button(
                        "取り消す",
                        width="stretch",
                        disabled=not draft_changes,
                        help="画面上の仮設定を捨てて、現在の本設定に戻します",
                    ):
                        st.session_state["rule_discard_requested"] = True
                        st.rerun()
                with default_col:
                    if st.button("デフォルトを仮設定にする", width="stretch"):
                        st.session_state["rule_default_draft_requested"] = True
                        st.rerun()

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
            st.dataframe(history_data, width="stretch", hide_index=True)

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
                    options=["すべて", "正社員・パートのみ", "退職者のみ", "顧問・補助のみ"],
                    key="emp_filter",
                )

            # 表示する従業員リスト
            if show_filter == "正社員・パートのみ":
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
                st.dataframe(emp_data, width="stretch", hide_index=True)
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
                        store_options = [NO_HOME_STORE_LABEL] + [s.name for s in Store if s != Store.OFF]
                        current_home = target.home_store.name if target.home_store else NO_HOME_STORE_LABEL
                        new_home_store = st.selectbox(
                            "ホーム店舗",
                            options=store_options,
                            index=store_options.index(current_home) if current_home in store_options else 0,
                            format_func=store_select_label,
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
                                None if new_home_store == NO_HOME_STORE_LABEL
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
                    home_store_options = [NO_HOME_STORE_LABEL] + [s.name for s in Store if s != Store.OFF]
                    new_emp_home = st.selectbox(
                        "ホーム店舗（固定配置の場合のみ）",
                        options=home_store_options,
                        format_func=store_select_label,
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
                                None if new_emp_home == NO_HOME_STORE_LABEL
                                else Store[new_emp_home]
                            ),
                            station_type=(
                                StationType.FIXED if new_emp_home != NO_HOME_STORE_LABEL
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
                st.dataframe(hist_data, width="stretch", hide_index=True)

    # ============================================================
    # タブ4: APIキー
    # ============================================================
    with setting_tab4:
        st.markdown("### 🔑 Claude API キー")
        st.caption(
            "自然言語の希望解析・AI対話に使用します。"
            "https://console.anthropic.com/ で取得してください。"
        )
        api_key_source = get_anthropic_api_key_source()
        if api_key_source == "secrets":
            st.success(
                "✅ Streamlit Secrets に `ANTHROPIC_API_KEY` が設定済みです。"
                "ログインのたびに入力する必要はありません。"
            )
        elif api_key_source == "environment":
            st.info(
                "✅ 環境変数 `ANTHROPIC_API_KEY` から読み込んでいます。"
                "ローカル実行中はこのまま利用できます。"
            )
        elif api_key_source == "session":
            st.warning(
                "⚠ APIキーはこの画面の一時入力として保存されています。"
                "アプリを開き直すと再入力が必要になる場合があります。"
            )
        else:
            st.warning(
                "⚠ APIキーが未設定です。Streamlit Cloud で運用する場合は、"
                "GitHubではなく Settings → Secrets に登録してください。"
            )

        st.markdown("**Streamlit Cloud に登録する内容**")
        st.code('ANTHROPIC_API_KEY = "sk-ant-..."', language="toml")
        st.caption(
            "このキーはGitHubにアップロードしません。"
            "Streamlit Cloud のアプリ設定にだけ保存してください。"
        )

        api_key = st.text_input(
            "一時的にこの画面で試す場合だけ入力",
            type="password",
            placeholder="sk-ant-...",
            help="本番運用では Streamlit Secrets への登録がおすすめです。",
        )
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            st.session_state["api_key"] = api_key
            st.success("✅ APIキーを一時的にセッションへ登録しました")

    # ============================================================
    # タブ5: バックアップ状況
    # ============================================================
    with setting_tab5:
        from prototype.data_export import (
            create_backup_zip, restore_from_zip,
            get_backup_filename, get_all_data_summary,
        )
        from prototype.github_backup import (
            is_github_backup_enabled, test_connection as gh_test_connection,
        )

        st.markdown("### 💾 データバックアップ")

        # ============================================================
        # GitHub 自動バックアップの状態表示
        # ============================================================
        st.markdown("#### ☁ GitHub 自動バックアップの状態")
        if is_github_backup_enabled():
            st.success(
                "✅ **GitHub 自動バックアップ 有効**\n\n"
                "従業員が希望を提出するたびに、自動的に GitHub の専用リポジトリへ"
                "バックアップされています。Streamlit Cloud のデータが消えても、"
                "GitHub には残っているので安心です。"
            )
            if st.button("🔌 接続テスト", key="test_gh_conn"):
                with st.spinner("GitHub に接続中..."):
                    success, msg = gh_test_connection()
                if success:
                    st.success(msg)
                else:
                    st.error(f"❌ {msg}")
        else:
            st.warning(
                "⚠ **GitHub 自動バックアップ 未設定**\n\n"
                "Streamlit Cloud の保存領域は揮発性のため、コード更新時にデータが消える"
                "可能性があります。**GitHub の専用リポジトリに自動バックアップを設定**することで、"
                "データ消失リスクをほぼゼロにできます。"
            )
            with st.expander("📖 GitHub 自動バックアップを設定する手順", expanded=False):
                st.markdown(
                    """
                    ### 手順（10〜15分）

                    #### 1. バックアップ用リポジトリを作成

                    1. https://github.com/new を開く
                    2. **Repository name**: `daikokuya-shift-data`
                    3. **Private** を選択（重要！従業員データが入るため）
                    4. その他はデフォルトのまま「Create repository」

                    #### 2. Personal Access Token (PAT) を作成

                    1. https://github.com/settings/tokens?type=beta を開く
                    2. 「**Generate new token**」をクリック
                    3. **Token name**: `Daikokuya Shift Backup`
                    4. **Expiration**: 1 year（推奨）
                    5. **Repository access**: `Only select repositories` → `daikokuya-shift-data` を選択
                    6. **Permissions** → **Repository permissions** → **Contents**: `Read and write`
                    7. ページ最下部の「**Generate token**」をクリック
                    8. 表示されたトークン（`github_pat_...` で始まる長い文字列）を **必ずコピーして保存**
                       （この画面を閉じると二度と見られません）

                    #### 3. Streamlit Secrets に追加

                    https://share.streamlit.io/ で自分のアプリの Settings → Secrets に以下を追加：

                    ```toml
                    GITHUB_TOKEN = "github_pat_あなたのトークン"
                    GITHUB_BACKUP_REPO = "kinoshita-cmyk/daikokuya-shift-data"
                    ```

                    （他のシークレットはそのまま残してください）

                    #### 4. 「Save」をクリック

                    アプリが自動再起動した後、このページに戻ってきて
                    上に「✅ GitHub 自動バックアップ 有効」と表示されれば完了！
                    """
                )

        st.markdown("---")

        # ⚠ 重要な警告
        st.warning(
            "🚨 **重要**: このシステムが動いているサーバー（Streamlit Cloud）の保存領域は"
            "**コード更新時にリセット**されることがあります。"
            "**従業員から提出された希望データを失わないよう、定期的に下のボタンで"
            "バックアップをダウンロードしてご自分のPCに保存してください。**"
        )

        # ============================================================
        # 現在のデータ状況サマリー
        # ============================================================
        st.markdown("#### 📊 現在の保存データ状況")
        data_summary = get_all_data_summary()

        sm_col1, sm_col2, sm_col3, sm_col4 = st.columns(4)
        with sm_col1:
            st.metric("従業員提出", f"{data_summary['submissions_total']} 件")
        with sm_col2:
            st.metric("確定シフト", f"{data_summary['finalized_shifts']} 件")
        with sm_col3:
            st.metric("ロック済み月", f"{data_summary['locked_months']} 件")
        with sm_col4:
            st.metric("従業員変更履歴", f"{data_summary['employees_modified']} 件")

        # 月別の提出状況
        if data_summary["submissions_by_month"]:
            st.markdown("##### 月別の提出データ件数")
            month_data = []
            for ym in sorted(data_summary["submissions_by_month"].keys(), reverse=True):
                month_data.append({
                    "年月": ym,
                    "提出ファイル数": data_summary["submissions_by_month"][ym],
                })
            st.dataframe(month_data, width="stretch", hide_index=True)

        # ============================================================
        # ダウンロード（エクスポート）
        # ============================================================
        st.markdown("---")
        st.markdown("#### ⬇ データをまとめてダウンロード（バックアップ作成）")
        st.caption(
            "全データを ZIP ファイルにまとめてダウンロードします。"
            "**従業員提出データを失う前に必ず実行してください。**"
        )

        dl_col1, dl_col2 = st.columns([2, 3])
        with dl_col1:
            include_output = st.checkbox(
                "生成済みExcel/PDFも含める",
                value=False,
                help="output/ ディレクトリの生成ファイルも含めます。容量が大きくなります。",
            )
        with dl_col2:
            st.caption(
                "💡 **おすすめのバックアップ頻度**: \n"
                "- 提出締切日（毎月25日）の翌日\n"
                "- シフト確定後\n"
                "- コードを更新する前"
            )

        # ZIP 生成（クリックで作成 → ダウンロードボタン表示）
        if st.button("📦 バックアップZIPを作成", type="primary", width="stretch"):
            with st.spinner("ZIPファイルを作成中..."):
                zip_bytes, summary = create_backup_zip(include_output=include_output)
            st.session_state["backup_zip_bytes"] = zip_bytes
            st.session_state["backup_summary"] = summary
            st.success(
                f"✅ バックアップ作成完了！\n"
                f"- ファイル数: **{summary.total_files} 件** "
                f"（提出データ {summary.submission_files} / "
                f"確定シフト {summary.finalized_shifts} / "
                f"ロック {summary.locked_months}）\n"
                f"- 圧縮後サイズ: **{len(zip_bytes) / 1024:.1f} KB**"
            )

        # ダウンロードボタン
        if "backup_zip_bytes" in st.session_state:
            st.download_button(
                label="⬇ バックアップZIPをダウンロード",
                data=st.session_state["backup_zip_bytes"],
                file_name=get_backup_filename(),
                mime="application/zip",
                width="stretch",
                key="dl_backup_zip",
            )
            st.caption(
                "↑ ダウンロードしたZIPは **PC・iCloud Drive・Google Drive など信頼できる場所**に保存してください。"
            )

        # ============================================================
        # 復元（インポート）
        # ============================================================
        st.markdown("---")
        st.markdown("#### ⬆ バックアップから復元")
        st.caption(
            "以前ダウンロードしたバックアップZIPをアップロードして復元します。"
            "**コード更新後にデータが消えた場合の復旧用です。**"
        )

        with st.expander("⚠ 復元の操作（クリックで展開）", expanded=False):
            uploaded_file = st.file_uploader(
                "バックアップZIPファイルを選択",
                type="zip",
                help="以前ダウンロードした daikokuya-shift-backup-*.zip を選択",
            )
            if uploaded_file:
                overwrite = st.checkbox(
                    "既存ファイルを上書きする",
                    value=False,
                    help="チェックを外すと、既に存在するファイルはスキップされます（安全モード）",
                )
                if st.button("📥 復元を実行", type="primary"):
                    with st.spinner("復元中..."):
                        zip_bytes = uploaded_file.read()
                        result = restore_from_zip(zip_bytes, overwrite=overwrite)
                    if result.success:
                        st.success(
                            f"✅ 復元完了！\n"
                            f"- 復元されたファイル: **{result.restored_files}** 件\n"
                            f"- スキップ: {result.skipped_files} 件"
                        )
                        if result.metadata:
                            exported_at = result.metadata.get("exported_at", "")
                            st.caption(f"バックアップ作成日時: {exported_at[:19]}")
                        if result.errors:
                            st.warning(
                                f"⚠ 一部エラーがありました:\n"
                                + "\n".join(f"- {e}" for e in result.errors)
                            )
                        st.info("✨ 画面をリロード（Cmd+R）すると復元されたデータが反映されます。")
                    else:
                        st.error(
                            f"❌ 復元に失敗しました:\n"
                            + "\n".join(f"- {e}" for e in result.errors)
                        )

        # ============================================================
        # ロック状況
        # ============================================================
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
