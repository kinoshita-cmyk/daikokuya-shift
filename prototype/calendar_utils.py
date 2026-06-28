"""カレンダー表示用の小さな祝日判定ユーティリティ。"""

from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """指定月の第n weekdayを返す。weekdayは月曜=0。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _vernal_equinox_day(year: int) -> int:
    """春分の日の簡易計算。実運用範囲の21世紀では十分安定。"""
    if 1980 <= year <= 2099:
        return int(20.8431 + 0.242194 * (year - 1980) - ((year - 1980) // 4))
    return 20


def _autumn_equinox_day(year: int) -> int:
    """秋分の日の簡易計算。実運用範囲の21世紀では十分安定。"""
    if 1980 <= year <= 2099:
        return int(23.2488 + 0.242194 * (year - 1980) - ((year - 1980) // 4))
    return 23


def japanese_holidays(year: int) -> set[date]:
    """日本の国民の祝日・振替休日・国民の休日を返す。"""
    y = int(year)
    holidays: set[date] = {
        date(y, 1, 1),   # 元日
        _nth_weekday(y, 1, 0, 2),  # 成人の日
        date(y, 2, 11),  # 建国記念の日
        date(y, 2, 23),  # 天皇誕生日
        date(y, 3, _vernal_equinox_day(y)),
        date(y, 4, 29),  # 昭和の日
        date(y, 5, 3),   # 憲法記念日
        date(y, 5, 4),   # みどりの日
        date(y, 5, 5),   # こどもの日
        _nth_weekday(y, 7, 0, 3),  # 海の日
        date(y, 8, 11),  # 山の日
        _nth_weekday(y, 9, 0, 3),  # 敬老の日
        date(y, 9, _autumn_equinox_day(y)),
        _nth_weekday(y, 10, 0, 2),  # スポーツの日
        date(y, 11, 3),  # 文化の日
        date(y, 11, 23), # 勤労感謝の日
    }

    # 振替休日: 祝日が日曜の場合、直後の平日で祝日でない日を休日にする。
    for holiday in sorted(list(holidays)):
        if holiday.weekday() != 6:
            continue
        substitute = holiday + timedelta(days=1)
        while substitute in holidays:
            substitute += timedelta(days=1)
        holidays.add(substitute)

    # 国民の休日: 前後を祝日に挟まれた平日を休日にする。
    current = date(y, 1, 2)
    end = date(y, 12, 30)
    while current <= end:
        if (
            current not in holidays
            and current.weekday() < 5
            and current - timedelta(days=1) in holidays
            and current + timedelta(days=1) in holidays
        ):
            holidays.add(current)
        current += timedelta(days=1)

    return holidays


def is_japanese_holiday(value: date) -> bool:
    """日付が日本の祝日か。"""
    return value in japanese_holidays(value.year)


def is_weekend_or_japanese_holiday(value: date) -> bool:
    """土日または日本の祝日か。"""
    return value.weekday() >= 5 or is_japanese_holiday(value)
