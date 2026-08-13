"""Deterministic parsing for the small set of relative times used by Agent.

The model may still choose the scheduling tool, but a user supplied relative
time must not depend on the model guessing a date or timezone.  This module
returns the Java contract's UTC ISO-8601 representation (with ``Z``).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_NUMBER_RE = r"[零〇一二两三四五六七八九十百\d]+"
_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def _parse_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if not value or any(char not in _CN_DIGITS and char not in "十百" for char in value):
        return None

    # Handle the common Chinese forms used for hours and short delays:
    # 八、十二、二十、两分钟.  The small unit parser also accepts 百 safely.
    total = 0
    current = 0
    for char in value:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            continue
        unit = 10 if char == "十" else 100
        total += (current or 1) * unit
        current = 0
    result = total + current
    return result if result > 0 else 0


def _as_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _load_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows Python installations do not always ship the IANA tzdata
        # package.  The product default is fixed UTC+08:00, so keep local
        # development deterministic without weakening validation for other
        # unknown timezone names.
        if name == "Asia/Shanghai":
            return dt_timezone(timedelta(hours=8), name=name)
        return None


def _parse_explicit_date(text: str) -> date | None:
    match = re.search(
        r"(?P<year>20\d{2})[-年/](?P<month>\d{1,2})[-月/](?P<day>\d{1,2})日?",
        text,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def parse_natural_schedule_time(
    text: str,
    timezone: str = "Asia/Shanghai",
    *,
    now: datetime | None = None,
) -> str | None:
    """Parse an explicit/relative user time into a UTC ``runAt`` value.

    Supported examples include ``明天上午八点`` and ``两分钟后``.  ``None``
    means the request did not contain a time expression this parser owns; in
    that case the caller may use a fully qualified model-supplied timestamp.
    """

    zone = _load_timezone(timezone)
    if zone is None:
        return None

    base = now or datetime.now(zone)
    base_local = (
        base.replace(tzinfo=zone) if base.tzinfo is None else base.astimezone(zone)
    )

    delay_after = re.search(
        rf"(?P<amount>{_NUMBER_RE})\s*"
        rf"(?P<unit>\u5206\u949f|\u5c0f\u65f6|\u5929)\s*"
        rf"(?:\u4e4b\u540e|\u540e)",
        text,
    )
    if delay_after:
        amount = _parse_number(delay_after.group("amount"))
        if amount is None:
            return None
        unit = delay_after.group("unit")
        if unit == "\u5206\u949f":
            target = base_local + timedelta(minutes=amount)
        elif unit == "\u5c0f\u65f6":
            target = base_local + timedelta(hours=amount)
        else:
            target = base_local + timedelta(days=amount)
        return _as_utc_iso(target)

    delay = re.search(
        rf"(?P<amount>{_NUMBER_RE})\s*(?P<unit>分钟|分|小时|个小时|天)\s*后",
        text,
    )
    if delay:
        amount = _parse_number(delay.group("amount"))
        if amount is None:
            return None
        unit = delay.group("unit")
        if unit in {"分钟", "分"}:
            target = base_local + timedelta(minutes=amount)
        elif unit in {"小时", "个小时"}:
            target = base_local + timedelta(hours=amount)
        else:
            target = base_local + timedelta(days=amount)
        return _as_utc_iso(target)

    explicit_day = _parse_explicit_date(text)
    relative_day: date | None = None
    if "后天" in text:
        relative_day = base_local.date() + timedelta(days=2)
    elif "明天" in text:
        relative_day = base_local.date() + timedelta(days=1)
    elif "今天" in text:
        relative_day = base_local.date()

    # Resolve the calendar meaning of phrases such as "下周一晚上8点"
    # deterministically.  "下周" means the following calendar week, so
    # "下周一" on a Monday is seven days away rather than today.  This is
    # intentionally a small temporal contract; it does not ask the model to
    # calculate dates and it keeps retries anchored to the same reference.
    if explicit_day is None and relative_day is None:
        weekday_match = re.search(
            r"下(?:周|星期|礼拜|个星期|个礼拜)"
            r"(?P<weekday>[一二三四五六日天])",
            text,
        )
        if weekday_match:
            target_weekday = _WEEKDAY_INDEX[weekday_match.group("weekday")]
            days_until_next_week = 7 - base_local.weekday() + target_weekday
            relative_day = base_local.date() + timedelta(days=days_until_next_week)

    target_day = explicit_day or relative_day
    if target_day is None:
        return None

    clock = re.search(
        rf"(?P<period>凌晨|早上|早晨|上午|中午|下午|晚上|今晚)?\s*"
        rf"(?P<hour>{_NUMBER_RE})(?:点|时|:)"
        rf"(?:(?P<minute>{_NUMBER_RE})(?:分|分钟)?)?",
        text,
    )
    if not clock:
        return None

    hour = _parse_number(clock.group("hour"))
    minute_value = clock.group("minute")
    minute = _parse_number(minute_value) if minute_value else 0
    if hour is None or minute is None or minute > 59:
        return None

    period = clock.group("period") or ""
    if period in {"下午", "晚上", "今晚"} and hour < 12:
        hour += 12
    elif period in {"凌晨", "早上", "早晨", "上午"} and hour == 12:
        hour = 0
    elif period == "中午" and hour < 11:
        hour += 12
    if hour > 23:
        return None

    try:
        target = datetime.combine(target_day, time(hour, minute), tzinfo=zone)
    except ValueError:
        return None
    return _as_utc_iso(target)


def format_local_schedule_time(run_at: str, timezone: str) -> str:
    """Format a Java ``runAt`` value for the user-facing confirmation."""
    try:
        parsed = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except ValueError:
        return run_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    zone = _load_timezone(timezone)
    if zone is None:
        return run_at
    local = parsed.astimezone(zone)
    return f"{local.year}年{local.month}月{local.day}日 {local:%H:%M}"
