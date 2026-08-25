"""Deterministic parsing for the small set of relative times used by Agent.

The model may still choose the scheduling tool, but a user supplied relative
time must not depend on the model guessing a date or timezone.  This module
returns the Java contract's UTC ISO-8601 representation (with ``Z``).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TemporalBase(StrEnum):
    """Authoritative anchor for a relative publication-time expression."""

    CURRENT_TIME = "CURRENT_TIME"
    EXISTING_SCHEDULE_TIME = "EXISTING_SCHEDULE_TIME"
    EXPLICIT_DATETIME = "EXPLICIT_DATETIME"

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
_EN_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
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

_EXPLICIT_TIMEZONE_ALIASES = {
    "UTC": "UTC",
    "GMT": "UTC",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
}


def _timezone_from_text(text: str, fallback: str):
    """Return an explicitly requested zone, otherwise the caller's zone."""

    offset = re.search(
        r"\b(?:UTC|GMT)\s*(?P<sign>[+-])(?P<hour>\d{1,2})"
        r"(?::?(?P<minute>\d{2}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if offset:
        minutes = int(offset.group("hour")) * 60 + int(offset.group("minute") or 0)
        if offset.group("sign") == "-":
            minutes = -minutes
        return dt_timezone(timedelta(minutes=minutes), name=offset.group(0).upper())

    for token, zone_name in _EXPLICIT_TIMEZONE_ALIASES.items():
        if re.search(rf"(?<![A-Za-z]){token}(?![A-Za-z])", text, re.IGNORECASE):
            zone = _load_timezone(zone_name)
            if zone is not None:
                return zone
    return _load_timezone(fallback)


def _english_relative_day(text: str, base_local: datetime) -> date | None:
    lower = text.lower()
    if re.search(r"\bday\s+after\s+tomorrow\b", lower):
        return base_local.date() + timedelta(days=2)
    if re.search(r"\btomorrow\b", lower):
        return base_local.date() + timedelta(days=1)
    if re.search(r"\btoday\b", lower):
        return base_local.date()
    return None


def _english_daypart(text: str) -> str:
    lower = text.lower()
    for value in ("afternoon", "evening", "night", "morning", "noon"):
        if re.search(rf"\b{value}\b", lower):
            return value
    return ""


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

    text = str(text or "").strip()
    if not text:
        return None

    zone = _timezone_from_text(text, timezone)
    if zone is None:
        return None

    base = now or datetime.now(zone)
    base_local = (
        base.replace(tzinfo=zone) if base.tzinfo is None else base.astimezone(zone)
    )

    # Canonical instants are already authoritative.  Re-parsing the wall
    # clock in the caller's timezone would shift a UTC value during an update
    # or retry, so pass ISO timestamps through the same canonical owner.
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
        r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})",
        text,
    ):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return _as_utc_iso(value)

    delay_after = re.search(
        rf"(?P<amount>{_NUMBER_RE})\s*"
        rf"(?P<unit>\u5206\u949f|\u5c0f\u65f6|\u5929)\s*"
        rf"(?:\u4e4b\u540e|\u4ee5\u540e|\u540e)",
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

    # Natural Chinese variants such as "过五分钟再发" and "等五分钟发出" use
    # a leading waiting verb instead of the canonical "五分钟后" suffix.
    # They still describe a relative delay from the current time.
    delay_prefix = re.search(
        rf"(?:过|等)\s*(?P<amount>{_NUMBER_RE})\s*"
        rf"(?P<unit>分钟|分|小时|个小时|天)",
        text,
    )
    if delay_prefix:
        amount = _parse_number(delay_prefix.group("amount"))
        if amount is None:
            return None
        unit = delay_prefix.group("unit")
        if unit in {"分钟", "分"}:
            target = base_local + timedelta(minutes=amount)
        elif unit in {"小时", "个小时"}:
            target = base_local + timedelta(hours=amount)
        else:
            target = base_local + timedelta(days=amount)
        return _as_utc_iso(target)

    # Rescheduling language is anchored by the caller's ``TemporalBase``.
    # With EXISTING_SCHEDULE_TIME, "比原计划晚十分钟" means the persisted
    # schedule time plus ten minutes, not current time plus ten minutes.
    reschedule_delta = re.search(
        rf"(?P<direction>比(?:原计划|原定|原来)?(?:晚|推迟|延后)|晚|推迟|延后|提前)\s*"
        rf"(?P<amount>{_NUMBER_RE})\s*(?P<unit>分钟|分|小时|个小时|天)",
        text,
    )
    if reschedule_delta:
        amount = _parse_number(reschedule_delta.group("amount"))
        if amount is None:
            return None
        unit = reschedule_delta.group("unit")
        if unit in {"分钟", "分"}:
            delta = timedelta(minutes=amount)
        elif unit in {"小时", "个小时"}:
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        direction = reschedule_delta.group("direction")
        target = base_local - delta if direction == "提前" else base_local + delta
        return _as_utc_iso(target)

    delay = re.search(
        rf"(?P<amount>{_NUMBER_RE})\s*(?P<unit>分钟|分|小时|个小时|天)\s*"
        rf"(?:后|以后|之后)",
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

    # English relative schedules ("in 5 minutes", "5 minutes from now", "2
    # hours later").  A reasoning model occasionally renders the temporal
    # constraint in English even for a Chinese request; the execution boundary
    # must still resolve it deterministically to an ISO instant instead of
    # forwarding natural language to the Java scheduler (observed:
    # SERVER_FAILURE with run_at="5 minutes from now").
    en_delay = re.search(
        r"(?:in\s+)?(?P<amount>\d+(?:\.\d+)?|[a-z]+(?:[-\s][a-z]+)?)\s*"
        r"(?P<unit>minutes?|hours?|days?|seconds?|weeks?)"
        r"(?:\s+(?:from\s+now|later))?",
        text,
        flags=re.IGNORECASE,
    )
    if en_delay and (
        re.search(r"\bin\s+", text, re.IGNORECASE)
        or re.search(r"from\s+now|later", text, re.IGNORECASE)
    ):
        raw_amount = en_delay.group("amount").lower().replace("-", " ")
        if raw_amount.isdigit():
            amount = float(raw_amount)
        else:
            words = raw_amount.split()
            if len(words) == 1 and words[0] in _EN_NUMBER_WORDS:
                amount = float(_EN_NUMBER_WORDS[words[0]])
            elif len(words) == 2 and all(word in _EN_NUMBER_WORDS for word in words):
                amount = float(_EN_NUMBER_WORDS[words[0]] + _EN_NUMBER_WORDS[words[1]])
            else:
                return None
        unit = en_delay.group("unit").lower()
        if unit.startswith("second"):
            target = base_local + timedelta(seconds=amount)
        elif unit.startswith("minute"):
            target = base_local + timedelta(minutes=amount)
        elif unit.startswith("hour"):
            target = base_local + timedelta(hours=amount)
        elif unit.startswith("week"):
            target = base_local + timedelta(weeks=amount)
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
    if relative_day is None:
        relative_day = _english_relative_day(text, base_local)

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
    # A bare time-of-day ("下午四点", "晚上八点") has no day; default it to
    # today and roll to the next day if the instant has already passed.
    defaulted_day = target_day is None
    if target_day is None:
        target_day = base_local.date()

    clock = re.search(
        rf"(?P<period>凌晨|早上|早晨|上午|中午|下午|晚上|今晚)?\s*"
        rf"(?P<hour>{_NUMBER_RE})\s*(?:点|时|:)\s*"
        rf"(?:(?P<minute>{_NUMBER_RE})(?:分|分钟)?)?",
        text,
    )
    if clock:
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
    else:
        # English absolute clock forms share this canonical path.  The
        # relative-day detection above makes both "tomorrow at 2 PM" and
        # "at 14:00 JST tomorrow" resolve in the requested/local zone.
        english_clock = re.search(
            r"(?<![A-Za-z])(?:at\s+)?"
            r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
            r"(?P<ampm>a\.?m\.?|p\.?m\.?)?(?![A-Za-z])",
            text,
            flags=re.IGNORECASE,
        )
        if not english_clock:
            return None
        hour = int(english_clock.group("hour"))
        minute = int(english_clock.group("minute") or 0)
        if minute > 59:
            return None
        ampm = (english_clock.group("ampm") or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm:
            period = _english_daypart(text)
            if period in {"afternoon", "evening", "night"} and hour < 12:
                hour += 12
            elif period == "noon" and hour < 11:
                hour += 12
    if hour > 23:
        return None

    try:
        target = datetime.combine(target_day, time(hour, minute), tzinfo=zone)
    except ValueError:
        return None
    if defaulted_day and target < base_local:
        target += timedelta(days=1)
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
