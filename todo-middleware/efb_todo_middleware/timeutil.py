# coding=utf-8

"""Times as the user types and reads them, in the configured time zone."""

import re
from datetime import datetime, time as dtime, timedelta, tzinfo
from typing import List, Optional, Tuple

WEEKDAYS = "一二三四五六日"

INPUT = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T]+(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?$")
INPUT_FORMAT = "yyyy-mm-dd HH:mm:ss（秒可以省略）"

OFFSETS: List[Tuple[str, str, timedelta]] = [
    ("1d", "前 1 天", timedelta(days=1)),
    ("6h", "前 6 小时", timedelta(hours=6)),
    ("3h", "前 3 小时", timedelta(hours=3)),
    ("1h", "前 1 小时", timedelta(hours=1)),
    ("10m", "前 10 分钟", timedelta(minutes=10)),
]
"""Reminders offered before the start of a to-do: key, label, offset."""
OFFSET_BY_KEY = {key: (label, delta) for key, label, delta in OFFSETS}

DEFAULT_TIME = dtime(8, 0)
"""Time of day used when only the date of a to-do is known, and the
reminder suggested for a to-do whose start was typed by hand."""


def parse_input(text: str, tz: tzinfo) -> Optional[datetime]:
    """A time typed as ``yyyy-mm-dd HH:mm[:ss]``; ``None`` if it is not one."""
    match = INPUT.match(text.strip())
    if not match:
        return None
    try:
        return datetime(*(int(i or 0) for i in match.groups()), tzinfo=tz)  # type: ignore[misc]
    except ValueError:  # e.g. 2026-02-30
        return None


def fmt(when: datetime, tz: tzinfo, seconds: bool = False) -> str:
    """``2026-10-26 09:00 周一``"""
    local = when.astimezone(tz)
    return "{} 周{}".format(local.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"),
                           WEEKDAYS[local.weekday()])


def fmt_short(when: datetime, tz: tzinfo) -> str:
    """``10-26 09:00 周一``"""
    local = when.astimezone(tz)
    return "{} 周{}".format(local.strftime("%m-%d %H:%M"), WEEKDAYS[local.weekday()])


def at_default_time(day: datetime, tz: tzinfo) -> datetime:
    """08:00 of the day of ``day``."""
    local = day.astimezone(tz)
    return datetime.combine(local.date(), DEFAULT_TIME, tzinfo=tz)


def offset_time(event_at: datetime, key: str) -> datetime:
    return event_at - OFFSET_BY_KEY[key][1]


def future_offsets(event_at: Optional[datetime], now: datetime) -> List[str]:
    """Keys of the offsets whose reminder is still ahead of ``now``."""
    if event_at is None:
        return []
    return [key for key, _, delta in OFFSETS if event_at - delta > now]


def ago(seconds: float) -> str:
    """How late, e.g. ``3 小时 5 分钟``."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return "{} 分钟".format(max(minutes, 1))
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return "{} 小时".format(hours) + (" {} 分钟".format(minutes) if minutes else "")
    return "{} 天".format(hours // 24)
