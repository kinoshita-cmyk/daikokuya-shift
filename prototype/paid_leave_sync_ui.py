"""Manager-only paid-leave integration panel; no network calls on ordinary reruns."""
from __future__ import annotations

from datetime import datetime

from .paid_leave_sync import (
    AttendanceSender, GitHubSyncRepository, JST, SyncError, format_jst, load_state,
    read_locked_payload, sending_is_active, set_paused, sync_month,
)


def render_paid_leave_sync_panel():
    import streamlit as st
    from .github_backup import _get_secret

    st.markdown("#### 勤務表への有給連携")
    now = datetime.now(JST)
    months = []
    for delta in range(2, -13, -1):
        index = now.year * 12 + now.month - 1 + delta
        months.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
    month = st.selectbox("連携する月", months, index=2, key="leave_sync_month")
    url = _get_secret("ATTENDANCE_PAID_LEAVE_SYNC_URL")
    token = _get_secret("ATTENDANCE_PAID_LEAVE_SYNC_TOKEN")
    connected = bool(url and token)
    if not connected:
        st.info("勤務表の接続先・専用連携キーは未設定です。送信はまだできません。")

    def repo():
        return GitHubSyncRepository(
            _get_secret("GITHUB_TOKEN"), _get_secret("GITHUB_BACKUP_REPO"),
            _get_secret("GITHUB_BACKUP_BRANCH", "main"),
        )

    cache_key = f"leave_sync_view_{month}"

    def refresh():
        state, _ = load_state(repo(), month)
        st.session_state[cache_key] = {"state": state, "checked_at": datetime.now(JST).strftime("%m/%d %H:%M:%S")}
        return state

    if st.button("連携状態を確認・更新", key="leave_sync_refresh"):
        try:
            refresh()
        except SyncError as exc:
            st.session_state.pop(cache_key, None)
            st.error(str(exc))

    view = st.session_state.get(cache_key)
    if not view:
        st.caption("連携状態：未確認")
        return
    state = view["state"]
    status = "保留中" if state["paused"] else {
        "waiting": "月初の連携待ち", "sending": "送信中（結果確認待ち）",
        "failed": "未連携（再送待ち）", "sent": "連携済み",
    }[state["status"]]
    st.write(f"**{month}：{status}**")
    st.caption(f"状態確認：{view['checked_at']} 日本時間 / 送信試行：{state['attempts']}回")
    if state.get("error"):
        st.warning(state["error"])
    if state.get("receipt"):
        receipt = state["receipt"]
        st.success(f"勤務表で受領済み：{receipt['employee_count']}名 / 有給合計{receipt['total_paid_leave_days']}日")
        st.caption(f"受領日時：{format_jst(receipt['received_at'])} 日本時間")

    payload = state.get("payload")
    if payload:
        st.dataframe([
            {"氏名": row["employee_name"], "有給日数": row["paid_leave_days"]}
            for row in payload["employees"]
        ], hide_index=True, width="stretch")
        st.caption(f"送信元の確定版：{payload['source_snapshot']['file']}")
    else:
        if st.button("ロック済みの連携候補を確認", key="leave_sync_preview"):
            try:
                view["candidate"] = read_locked_payload(repo(), month, now)
            except SyncError as exc:
                view.pop("candidate", None)
                st.warning(str(exc))
        candidate = view.get("candidate")
        if candidate:
            st.dataframe([
                {"氏名": row["employee_name"], "有給日数": row["paid_leave_days"]}
                for row in candidate["employees"]
            ], hide_index=True, width="stretch")
            st.caption(f"候補の確定版：{candidate['source_snapshot']['file']}")

    sent = state["status"] == "sent"
    if sent:
        st.caption("連携済みの値は上書きしません。追加・取消は勤務表側で管理します。")
    active = sending_is_active(state, now)
    hold_label = "保留を解除する" if state["paused"] else "今月の連携を保留する"
    if st.button(hold_label, disabled=active or sent, key="leave_sync_hold"):
        try:
            set_paused(repo(), month, not state["paused"], now, actor="シフト管理画面")
            refresh()
            st.rerun()
        except SyncError as exc:
            st.error(str(exc))
    if payload:
        st.caption("再送時も最初に保存した同じ日数を使います。再ロックしても送信内容は置き換えません。")
    reviewed = payload or view.get("candidate")
    confirm = st.checkbox("対象月と日数を確認して送信する", key=f"leave_sync_confirm_{month}", disabled=sent)
    if st.button(
        "勤務表へ送信・再送", key="leave_sync_send",
        disabled=not (connected and confirm and reviewed) or active or sent or state["paused"] or month > now.strftime("%Y-%m"),
    ):
        try:
            result = sync_month(
                repo(), AttendanceSender(url, token), month, now, manual=True,
                expected_snapshot_sha256=reviewed["source_snapshot"]["sha256"],
            )
            refresh()
            st.session_state[f"leave_sync_last_result_{month}"] = (result.status, result.message)
            st.rerun()
        except SyncError as exc:
            st.error(str(exc))
    notice = st.session_state.pop(f"leave_sync_last_result_{month}", None)
    if notice:
        (st.success if notice[0] == "sent" else st.warning)(notice[1])
