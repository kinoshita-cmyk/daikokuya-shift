"""
希望提出の受付期間と時刻表示の共通処理。

Streamlit Cloud は UTC で時刻を保存することがあるため、画面表示は日本時間へ揃える。
また、運用開始前のテスト提出が本番の提出状況に混ざらないよう、月別の受付開始時刻を扱う。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .paths import CONFIG_DIR


JST = timezone(timedelta(hours=9))
UTC = timezone.utc
SUBMISSION_WINDOWS_FILE = CONFIG_DIR / "submission_windows.json"


def now_jst() -> datetime:
    """現在時刻を日本時間で返す。"""
    return datetime.now(JST)


def parse_submission_timestamp(value: str | None) -> Optional[datetime]:
    """
    保存済みの提出時刻を timezone-aware datetime にする。

    既存データの多くはタイムゾーンなしで保存されている。
    Streamlit Cloud 上では UTC として保存されていたため、タイムゾーンなしは UTC とみなす。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def timestamp_sort_key(value: str | None) -> float:
    """提出時刻の比較用キー。読めない時刻は最古扱いにする。"""
    parsed = parse_submission_timestamp(value)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


def format_timestamp_jst(value: str | None) -> str:
    """提出時刻を画面表示用の日本時間へ変換する。"""
    parsed = parse_submission_timestamp(value)
    if parsed is None:
        return str(value or "")
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def _load_submission_windows() -> dict:
    if not SUBMISSION_WINDOWS_FILE.exists():
        return {"version": 1, "windows": {}}
    try:
        with open(SUBMISSION_WINDOWS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "windows": {}}
    if not isinstance(data, dict):
        return {"version": 1, "windows": {}}
    windows = data.get("windows", {})
    if not isinstance(windows, dict):
        windows = {}
    return {"version": data.get("version", 1), "windows": windows}


def get_submission_window(year: int, month: int) -> dict:
    """指定月の受付期間設定を返す。未設定なら空 dict。"""
    data = _load_submission_windows()
    return data.get("windows", {}).get(f"{int(year):04d}-{int(month):02d}", {}) or {}


def is_submission_in_window(year: int, month: int, saved_at: str | None) -> bool:
    """提出ファイルが月別受付期間内かどうか。受付期間未設定なら常に True。"""
    window = get_submission_window(year, month)
    start_at = parse_submission_timestamp(window.get("start_at"))
    end_at = parse_submission_timestamp(window.get("end_at"))
    submitted_at = parse_submission_timestamp(saved_at)
    if submitted_at is None:
        return True
    if start_at is not None and submitted_at < start_at:
        return False
    if end_at is not None and submitted_at > end_at:
        return False
    return True
