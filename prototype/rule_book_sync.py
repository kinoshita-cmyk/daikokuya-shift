"""Non-blocking publication, isolated from shift generation and persistence."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .paths import CONFIG_DIR, BACKUP_DIR
from .rule_book import BASE_URL, build_snapshot, digest
from .submission_window import now_jst

log = logging.getLogger(__name__)
_lock = threading.Lock()
_state = {"running": False, "signature": "", "success_at": "", "error": "", "attempt": 0}


def source_signature():
    paths = list(CONFIG_DIR.glob("*.json"))
    paths += list(BACKUP_DIR.glob("*/preferences_*.json"))
    paths += [Path(__file__).with_name(name) for name in (
        "rules.py", "rule_book.py", "employees.py", "submission_loader.py",
    )]
    return digest({
        "month": now_jst().strftime("%Y-%m"),
        "files": [(str(p), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in sorted(paths) if p.exists()],
    })


def publish_snapshot(snapshot, token, *, post=None):
    import requests
    post = post or requests.post
    response = post(
        BASE_URL + "/api/sync",
        headers={"Authorization": "Bearer " + token},
        json=snapshot, timeout=(3, 12), allow_redirects=False,
    )
    # Never include response bodies in logs: they might contain private data.
    if response.status_code != 200:
        raise RuntimeError(f"条件ブック更新先から HTTP {response.status_code}")
    answer = response.json()
    if answer.get("content_hash") != snapshot["content_hash"]:
        raise RuntimeError("条件ブックの更新内容を照合できませんでした")
    return str(answer["synced_at"])


def schedule_sync(token, *, force=False):
    """Only enqueue after a local change; failure never cancels a shift save."""
    signature = source_signature()
    with _lock:
        if _state["running"]:
            return
        if not force and signature == _state["signature"]:
            return
        if not force and time.monotonic() - _state["attempt"] < 60:
            return
        _state.update(running=True, attempt=time.monotonic())

    def work():
        try:
            snapshot = build_snapshot()
            # Avoid publishing a mixed view if someone saved during the read.
            if signature != source_signature():
                raise RuntimeError("設定更新中のため、次回表示時に再同期します")
            synced = publish_snapshot(snapshot, token)
            with _lock:
                _state.update(signature=signature, success_at=synced, error="")
            log.info("[rule-book] synced hash=%s months=%d", snapshot["content_hash"][:12], len(snapshot["months"]))
        except Exception as exc:
            with _lock:
                _state["error"] = type(exc).__name__
            log.warning("[rule-book] sync failed type=%s (shift save unaffected)", type(exc).__name__)
        finally:
            with _lock:
                _state["running"] = False
    threading.Thread(target=work, daemon=True, name="rule-book-sync").start()


def render_sync_panel():
    import streamlit as st
    from .github_backup import _get_secret
    from .submission_window import format_timestamp_jst
    token = _get_secret("RULE_BOOK_SYNC_TOKEN")
    with st.expander("シフト条件ブック（閲覧ページ・自動更新）", expanded=False):
        st.link_button("条件ブックを開く", BASE_URL)
        st.caption("閲覧用URLを知っている人はログインなしで閲覧できます。原文・確認メモは掲載せず、生成に渡す条件を表示します。")
        if not token:
            st.warning("自動更新は未接続です。StreamlitのSecretsに RULE_BOOK_SYNC_TOKEN を設定してください。")
            return
        force = st.button("条件ブックの更新を再試行", key="rule_book_retry")
        schedule_sync(token, force=force)
        with _lock:
            state = dict(_state)
        if state["error"]:
            st.warning("条件ブックの更新に失敗しました。公開ページは前回の内容です。シフトの保存・生成には影響しません。再試行してください。")
        elif state["running"]:
            st.info("条件ブックを更新中です。完了状況は次の画面表示時に確認できます。")
        elif state["success_at"]:
            st.caption("条件ブック最終同期: " + format_timestamp_jst(state["success_at"]) + "（日本時間）")
