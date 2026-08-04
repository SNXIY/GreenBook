"""Authoritative schedule temporal resolution for the control plane.

Natural-language time expressions are resolved here into timezone-aware absolute
``run_at`` values before ChangeCompiler / ToolRuntime. Relative delays must never
be deferred to tool-execution time (queue/retry drift).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from app.domain import ApiModel

TemporalMode = Literal[
    "ABSOLUTE",
    "RELATIVE_TO_NOW",
    "RELATIVE_TO_EXISTING",
    "AMBIGUOUS",
    "NOT_FOUND",
    "PAST_TIME",
]


class TemporalResolution(ApiModel):
    """Structured result of schedule temporal resolution."""

    mode: TemporalMode
    run_at: datetime | None = None
    offset_seconds: int | None = None
    timezone: str = "Asia/Shanghai"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_text: str | None = None
    base_type: str | None = None  # current_time | existing_run_at
    base_time: datetime | None = None
    error_code: str | None = None
    error: str | None = None

    @field_validator("run_at", "base_time")
    @classmethod
    def _aware_only(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("TemporalResolution datetimes must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _success_has_run_at(self) -> TemporalResolution:
        if self.mode in {"ABSOLUTE", "RELATIVE_TO_NOW", "RELATIVE_TO_EXISTING"}:
            if self.run_at is None:
                raise ValueError(f"{self.mode} requires run_at")
        elif self.run_at is not None:
            raise ValueError(f"{self.mode} must not set run_at")
        return self


_CN_DIGITS = {
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
    "零": 0,
}

# Relative to existing schedule (delay/postpone/advance).
_RELATIVE_TO_EXISTING = re.compile(
    r"(?P<verb>延迟|延后|延遲|推迟|提前)"
    r"(?P<half>半)?"
    r"(?:(?P<num>\d{1,4}|[一二两三四五六七八九十百]{1,4})\s*)?"
    r"(?P<unit>分钟|分鐘|小时|小時|钟头)?"
    r"(?:发布|發佈)?",
    re.IGNORECASE,
)

# Relative to message/current time: N minutes/hours later.
_RELATIVE_TO_NOW_DURATION = re.compile(
    r"(?P<half>半)?"
    r"(?:(?P<num>\d{1,4}|[一二两三四五六七八九十百]{1,4})\s*)?"
    r"(?P<unit>分钟|分鐘|小时|小時|钟头)"
    r"\s*(?:之后|之後|以后|以後|后|後)",
    re.IGNORECASE,
)
_RELATIVE_TO_NOW_HALF = re.compile(
    r"半\s*(?:个|個)?\s*(?:小时|小時|钟头)\s*(?:之后|之後|以后|以後|后|後)?",
    re.IGNORECASE,
)

# Incomplete schedule-change asks (no concrete new time).
_INCOMPLETE_SCHEDULE = re.compile(
    r"(?:"
    r"调整一下(?:一下)?发布时间|调整(?:一下)?发布时间|"
    r"改个时间|改个发布时间|改一下时间|改一下发布时间|"
    r"延迟发布|延后发布|推迟发布|稍后发布|稍后再发|"
    r"改个时候|调整时间(?!到|为|成|至)"
    r")",
    re.IGNORECASE,
)

_CALENDAR_DATETIME = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
    r"\s*(?:(?P<period>凌晨|早上|上午|中午|下午|晚上|今晚))?\s*"
    r"(?P<hour>\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:点|时)"
    r"(?:\s*(?P<minute>半|\d{1,2})\s*分?)?",
    re.IGNORECASE,
)

_CLOCK_TIME = re.compile(
    r"(?:(?P<day>今天|明天|后天)\s*)?"
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上|今晚)?\s*"
    r"(?P<hour>\d{1,2}|[一二两三四五六七八九十]{1,3})"
    r"(?:点|时)\s*(?P<minute>半|\d{1,2}\s*分?)?",
    re.IGNORECASE,
)
_COLON_TIME = re.compile(
    r"(?:(?P<day>今天|明天|后天)\s*)?"
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上|今晚)?\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})",
    re.IGNORECASE,
)

# Soft gate: expressions that look like a schedule-time mutation.
_SCHEDULE_TIME_INTENT = re.compile(
    r"(?:"
    r"发布(?:时间)?|定时|排期|"
    r"改成|改为|改到|换成|调整到|调整为|修改为|变更为|设置为|设为|定在|安排在|"
    r"提前到|延后到|推迟到|延迟|延后|推迟|提前|"
    r"分钟|小時|小时|钟头|今晚|明天|后天|今天|"
    r"之后|之後|以后|以後"
    r")",
    re.IGNORECASE,
)


class TemporalResolver:
    """Single authority for schedule NL → absolute run_at."""

    DEFAULT_TIMEZONE = "Asia/Shanghai"

    def resolve_schedule_time(
        self,
        *,
        message: str,
        current_time: datetime,
        timezone: str = DEFAULT_TIMEZONE,
        existing_run_at: datetime | None = None,
    ) -> TemporalResolution:
        text = (message or "").strip()
        zone = self._zone(timezone)
        current = self._as_zone(current_time, zone)
        existing = (
            self._as_zone(existing_run_at, zone) if existing_run_at is not None else None
        )

        if not text:
            return self._fail(
                mode="NOT_FOUND",
                timezone=timezone,
                error_code="EMPTY_MESSAGE",
                error="empty schedule message",
            )

        incomplete = _INCOMPLETE_SCHEDULE.search(text)
        if incomplete and not self._has_concrete_duration_or_clock(text):
            return TemporalResolution(
                mode="AMBIGUOUS",
                timezone=timezone,
                confidence=0.9,
                source_text=incomplete.group(0),
                error_code="INCOMPLETE_SCHEDULE_TIME",
                error="schedule change requested without a concrete new time",
            )

        # 1) Relative to existing schedule (延迟/推迟/提前 + duration).
        existing_rel = self._match_relative_to_existing(text)
        if existing_rel is not None:
            verb, offset, source = existing_rel
            if existing is None:
                return TemporalResolution(
                    mode="AMBIGUOUS",
                    timezone=timezone,
                    confidence=0.85,
                    source_text=source,
                    offset_seconds=offset if verb != "提前" else -offset,
                    error_code="MISSING_EXISTING_RUN_AT",
                    error="relative-to-existing requires existing_run_at",
                )
            signed = -offset if verb == "提前" else offset
            run_at = existing + timedelta(seconds=signed)
            return TemporalResolution(
                mode="RELATIVE_TO_EXISTING",
                run_at=run_at,
                offset_seconds=signed,
                timezone=timezone,
                confidence=0.95,
                source_text=source,
                base_type="existing_run_at",
                base_time=existing,
            )

        # 2) Absolute calendar / clock (preferred over "N minutes later" when
        # compound utterances contain both, e.g. "五分钟之后改成下午两点半").
        absolute = self._match_absolute(text, current=current, zone=zone)
        if absolute is not None:
            return absolute

        # 3) Relative to message/current time.
        now_rel = self._match_relative_to_now(text)
        if now_rel is not None:
            offset, source = now_rel
            run_at = current + timedelta(seconds=offset)
            return TemporalResolution(
                mode="RELATIVE_TO_NOW",
                run_at=run_at,
                offset_seconds=offset,
                timezone=timezone,
                confidence=0.95,
                source_text=source,
                base_type="current_time",
                base_time=current,
            )

        if _SCHEDULE_TIME_INTENT.search(text):
            return TemporalResolution(
                mode="AMBIGUOUS",
                timezone=timezone,
                confidence=0.7,
                source_text=text[:120],
                error_code="UNRESOLVED_SCHEDULE_TIME",
                error="schedule intent detected but no concrete time parsed",
            )
        return self._fail(
            mode="NOT_FOUND",
            timezone=timezone,
            error_code="NO_TEMPORAL_EXPRESSION",
            error="no schedule temporal expression found",
            source_text=text[:120],
        )

    # ── matchers ────────────────────────────────────────────────────────

    def _match_relative_to_existing(
        self, text: str
    ) -> tuple[str, int, str] | None:
        match = _RELATIVE_TO_EXISTING.search(text)
        if match is None:
            return None
        verb = match.group("verb")
        # "延迟发布" without duration is incomplete (handled earlier); still
        # reject bare verb with no unit/num/half here.
        if not match.group("half") and not match.group("num") and not match.group("unit"):
            return None
        seconds = self._duration_seconds(
            half=bool(match.group("half")),
            num_text=match.group("num"),
            unit=match.group("unit"),
        )
        if seconds is None or seconds <= 0:
            return None
        return verb, seconds, match.group(0)

    def _match_relative_to_now(self, text: str) -> tuple[int, str] | None:
        half = _RELATIVE_TO_NOW_HALF.search(text)
        if half is not None:
            return 1800, half.group(0)
        match = _RELATIVE_TO_NOW_DURATION.search(text)
        if match is None:
            return None
        seconds = self._duration_seconds(
            half=bool(match.group("half")),
            num_text=match.group("num"),
            unit=match.group("unit"),
        )
        if seconds is None or seconds <= 0:
            return None
        return seconds, match.group(0)

    def _match_absolute(
        self,
        text: str,
        *,
        current: datetime,
        zone: ZoneInfo,
    ) -> TemporalResolution | None:
        calendar = _CALENDAR_DATETIME.search(text)
        if calendar is not None:
            hour, minute = self._hour_minute(
                hour_text=calendar.group("hour"),
                minute_text=calendar.group("minute"),
                period=calendar.group("period"),
            )
            if hour is None:
                return None
            hour = self._apply_period(hour, calendar.group("period"))
            try:
                candidate = datetime(
                    int(calendar.group("year")),
                    int(calendar.group("month")),
                    int(calendar.group("day")),
                    hour,
                    minute,
                    tzinfo=zone,
                )
            except ValueError:
                return None
            if candidate <= current:
                return TemporalResolution(
                    mode="PAST_TIME",
                    timezone=str(zone),
                    confidence=0.95,
                    source_text=calendar.group(0),
                    base_type="current_time",
                    base_time=current,
                    error_code="TIME_IN_PAST",
                    error="requested calendar time is already in the past",
                )
            return TemporalResolution(
                mode="ABSOLUTE",
                run_at=candidate,
                timezone=str(zone),
                confidence=0.97,
                source_text=calendar.group(0),
                base_type="current_time",
                base_time=current,
            )

        colon = _COLON_TIME.search(text)
        clock = _CLOCK_TIME.search(text) if colon is None else None
        if colon is None and clock is None:
            return None
        if colon is not None:
            day = colon.group("day")
            period = colon.group("period")
            hour = int(colon.group("hour"))
            minute = int(colon.group("minute"))
            source = colon.group(0)
        else:
            assert clock is not None
            day = clock.group("day")
            period = clock.group("period")
            hour, minute = self._hour_minute(
                hour_text=clock.group("hour"),
                minute_text=clock.group("minute"),
                period=period,
            )
            source = clock.group(0)
            if hour is None:
                return None
        hour = self._apply_period(hour, period)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        # Bare "今晚" without day token implies today evening.
        if period == "今晚" and not day:
            day = "今天"
        day_offset = {"明天": 1, "后天": 2}.get(day or "", 0)
        candidate = (current + timedelta(days=day_offset)).replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if day_offset == 0 and candidate <= current:
            # Explicit "今天" (or 今晚) that is already past → PAST_TIME.
            # Unspecified day may roll to next occurrence for soft clock times
            # only when the utterance did not say 今天/今晚.
            if day in {"今天"} or period == "今晚":
                return TemporalResolution(
                    mode="PAST_TIME",
                    timezone=str(zone),
                    confidence=0.95,
                    source_text=source,
                    base_type="current_time",
                    base_time=current,
                    error_code="TIME_IN_PAST",
                    error="requested clock time is already in the past",
                )
            candidate += timedelta(days=1)
        return TemporalResolution(
            mode="ABSOLUTE",
            run_at=candidate,
            timezone=str(zone),
            confidence=0.95,
            source_text=source,
            base_type="current_time",
            base_time=current,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _has_concrete_duration_or_clock(text: str) -> bool:
        if _RELATIVE_TO_NOW_DURATION.search(text) or _RELATIVE_TO_NOW_HALF.search(text):
            return True
        if _CLOCK_TIME.search(text) or _COLON_TIME.search(text):
            return True
        if _CALENDAR_DATETIME.search(text):
            return True
        existing = _RELATIVE_TO_EXISTING.search(text)
        if existing and (
            existing.group("half") or existing.group("num") or existing.group("unit")
        ):
            # "延迟十分钟" is concrete; "延迟发布" is not.
            if existing.group("num") or existing.group("half"):
                return True
            if existing.group("unit") and existing.group("num"):
                return True
        return False

    def _duration_seconds(
        self,
        *,
        half: bool,
        num_text: str | None,
        unit: str | None,
    ) -> int | None:
        if half and (unit is None or "时" in (unit or "") or "鐘" in (unit or "")):
            return 1800
        if half and unit and "分" in unit:
            return 30
        value = self._parse_number(num_text) if num_text else None
        if value is None or value <= 0:
            # "半小时" already handled; bare unit without number is invalid.
            return None
        if unit is None:
            return None
        if "分" in unit:
            return value * 60
        if "时" in unit or "鐘" in unit or "钟" in unit:
            return value * 3600
        return None

    def _hour_minute(
        self,
        *,
        hour_text: str | None,
        minute_text: str | None,
        period: str | None,
    ) -> tuple[int | None, int]:
        del period
        if hour_text is None:
            return None, 0
        hour = self._parse_number(hour_text)
        if hour is None:
            return None, 0
        if minute_text is None or not str(minute_text).strip():
            minute = 0
        elif str(minute_text).strip() == "半":
            minute = 30
        else:
            minute = int(re.sub(r"\D", "", str(minute_text)) or "0")
        return hour, minute

    @staticmethod
    def _apply_period(hour: int, period: str | None) -> int:
        if period in {"下午", "晚上", "今晚"} and hour < 12:
            return hour + 12
        if period == "中午" and hour < 11:
            return hour + 12
        if period in {"凌晨", "上午", "早上"} and hour == 12:
            return 0
        return hour

    @staticmethod
    def _parse_number(value: str | None) -> int | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        if text == "十":
            return 10
        if "百" in text:
            left, _, right = text.partition("百")
            hundreds = _CN_DIGITS.get(left, 1) * 100
            tail = TemporalResolver._parse_number(right) if right else 0
            return hundreds + (tail or 0)
        if "十" in text:
            left, _, right = text.partition("十")
            tens = _CN_DIGITS.get(left, 1) * 10
            return tens + (_CN_DIGITS.get(right, 0) if right else 0)
        if all(char in _CN_DIGITS for char in text):
            result = 0
            for char in text:
                result = result * 10 + _CN_DIGITS[char]
            return result
        return _CN_DIGITS.get(text)

    @staticmethod
    def _zone(timezone: str) -> ZoneInfo:
        try:
            return ZoneInfo(timezone)
        except Exception:
            return ZoneInfo(TemporalResolver.DEFAULT_TIMEZONE)

    @staticmethod
    def _as_zone(value: datetime, zone: ZoneInfo) -> datetime:
        if value.tzinfo is None:
            raise ValueError("current_time/existing_run_at must be timezone-aware")
        return value.astimezone(zone)

    @staticmethod
    def _fail(
        *,
        mode: TemporalMode,
        timezone: str,
        error_code: str,
        error: str,
        source_text: str | None = None,
    ) -> TemporalResolution:
        return TemporalResolution(
            mode=mode,
            timezone=timezone,
            confidence=0.0,
            source_text=source_text,
            error_code=error_code,
            error=error,
        )


temporal_resolver = TemporalResolver()


def resolve_schedule_time(
    *,
    message: str,
    current_time: datetime,
    timezone: str = TemporalResolver.DEFAULT_TIMEZONE,
    existing_run_at: datetime | None = None,
) -> TemporalResolution:
    """Module-level entry used by ChangeCompiler and the legacy facade."""

    return temporal_resolver.resolve_schedule_time(
        message=message,
        current_time=current_time,
        timezone=timezone,
        existing_run_at=existing_run_at,
    )


def run_at_utc_isoformat(run_at: datetime) -> str:
    """Serialize an aware datetime as UTC ISO-8601 with Z suffix."""

    if run_at.tzinfo is None:
        raise ValueError("run_at must be timezone-aware")
    utc = run_at.astimezone(ZoneInfo("UTC"))
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_run_at_for_tool(run_at: datetime | str) -> str:
    """Canonical tool argument: UTC ISO-8601 ending in Z.

    Callers must not guess timezone from string suffixes elsewhere.
    """

    if isinstance(run_at, datetime):
        return run_at_utc_isoformat(run_at)
    text = str(run_at).strip()
    if not text:
        raise ValueError("empty run_at")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("run_at string must include an explicit timezone offset or Z")
    return run_at_utc_isoformat(parsed)


def format_beijing_time(run_at: datetime | str) -> str:
    """User-facing Beijing local time string."""

    if isinstance(run_at, str):
        parsed = datetime.fromisoformat(run_at.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("run_at string must include an explicit timezone offset or Z")
        value = parsed
    else:
        value = run_at
    if value.tzinfo is None:
        raise ValueError("run_at must be timezone-aware")
    local = value.astimezone(ZoneInfo("Asia/Shanghai"))
    period = "上午" if local.hour < 12 else "下午"
    hour12 = local.hour % 12 or 12
    return (
        f"{local.year}年{local.month}月{local.day}日"
        f"{period}{hour12}:{local.minute:02d}（北京时间）"
    )


def format_run_at_for_user(run_at: datetime | str) -> str:
    """Alias for user-facing Beijing formatting."""

    return format_beijing_time(run_at)


__all__ = [
    "TemporalMode",
    "TemporalResolution",
    "TemporalResolver",
    "temporal_resolver",
    "resolve_schedule_time",
    "run_at_utc_isoformat",
    "normalize_run_at_for_tool",
    "format_beijing_time",
    "format_run_at_for_user",
]
