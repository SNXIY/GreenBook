"""Phase 3.7 TemporalResolver matrix and no-drift tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.temporal_resolver import (
    TemporalResolver,
    format_beijing_time,
    resolve_schedule_time,
    run_at_utc_isoformat,
)

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 4, 15, 30, tzinfo=SH)
EXISTING = datetime(2026, 8, 5, 8, 0, tzinfo=SH)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("修改为五分钟之后", datetime(2026, 8, 4, 15, 35, tzinfo=SH)),
        ("半小时后发布", datetime(2026, 8, 4, 16, 0, tzinfo=SH)),
        ("两小时以后", datetime(2026, 8, 4, 17, 30, tzinfo=SH)),
    ],
)
def test_group_a_relative_to_now(message: str, expected: datetime) -> None:
    result = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.mode == "RELATIVE_TO_NOW"
    assert result.base_type == "current_time"
    assert result.base_time == NOW
    assert result.run_at == expected
    assert result.run_at is not None and result.run_at.tzinfo is not None


@pytest.mark.parametrize(
    ("message", "expected", "offset"),
    [
        ("延迟十分钟", datetime(2026, 8, 5, 8, 10, tzinfo=SH), 600),
        ("推迟半小时", datetime(2026, 8, 5, 8, 30, tzinfo=SH), 1800),
        ("提前一小时", datetime(2026, 8, 5, 7, 0, tzinfo=SH), -3600),
        ("提前半小时", datetime(2026, 8, 5, 7, 30, tzinfo=SH), -1800),
    ],
)
def test_group_b_relative_to_existing(
    message: str, expected: datetime, offset: int
) -> None:
    result = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.mode == "RELATIVE_TO_EXISTING"
    assert result.base_type == "existing_run_at"
    assert result.base_time == EXISTING
    assert result.offset_seconds == offset
    assert result.run_at == expected


def test_group_b_missing_existing_is_ambiguous() -> None:
    result = resolve_schedule_time(
        message="延迟十分钟",
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=None,
    )
    assert result.mode == "AMBIGUOUS"
    assert result.run_at is None
    assert result.error_code == "MISSING_EXISTING_RUN_AT"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("改到今晚八点", datetime(2026, 8, 4, 20, 0, tzinfo=SH)),
        ("明天上午八点", datetime(2026, 8, 5, 8, 0, tzinfo=SH)),
        ("后天下午三点", datetime(2026, 8, 6, 15, 0, tzinfo=SH)),
        ("2026年8月6日20点", datetime(2026, 8, 6, 20, 0, tzinfo=SH)),
    ],
)
def test_group_c_absolute(message: str, expected: datetime) -> None:
    result = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.mode == "ABSOLUTE"
    assert result.run_at == expected


@pytest.mark.parametrize(
    "message",
    [
        "调整一下发布时间",
        "改个时间",
        "延迟发布",
        "稍后发布",
    ],
)
def test_group_d_incomplete_ambiguous(message: str) -> None:
    result = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.mode == "AMBIGUOUS"
    assert result.run_at is None


def test_group_e_past_time_not_rolled() -> None:
    evening = datetime(2026, 8, 4, 21, 0, tzinfo=SH)
    result = resolve_schedule_time(
        message="今天晚上八点",
        current_time=evening,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.mode == "PAST_TIME"
    assert result.error_code == "TIME_IN_PAST"
    assert result.run_at is None


def test_group_f_timezone_utc_and_beijing_display() -> None:
    result = resolve_schedule_time(
        message="明天上午八点",
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.run_at is not None
    assert run_at_utc_isoformat(result.run_at) == "2026-08-05T00:00:00Z"
    assert "北京时间" in format_beijing_time(result.run_at)
    assert "8月5日" in format_beijing_time(result.run_at)


def test_no_drift_uses_message_created_at_not_worker_now() -> None:
    message_created = NOW
    worker_now = NOW + timedelta(minutes=2)
    result = resolve_schedule_time(
        message="五分钟之后",
        current_time=message_created,
        timezone="Asia/Shanghai",
    )
    drifted = resolve_schedule_time(
        message="五分钟之后",
        current_time=worker_now,
        timezone="Asia/Shanghai",
    )
    assert result.run_at == datetime(2026, 8, 4, 15, 35, tzinfo=SH)
    assert drifted.run_at == datetime(2026, 8, 4, 15, 37, tzinfo=SH)
    # Control plane must keep using message_created, never worker_now.
    assert result.run_at != drifted.run_at


def test_relative_existing_not_relative_to_now() -> None:
    result = resolve_schedule_time(
        message="把第一篇延迟十分钟",
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert result.run_at == datetime(2026, 8, 5, 8, 10, tzinfo=SH)
    assert result.run_at != NOW + timedelta(minutes=10)


def test_naive_current_time_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_schedule_time(
            message="五分钟之后",
            current_time=datetime(2026, 8, 4, 15, 30),
            timezone="Asia/Shanghai",
        )


def test_resolver_instance_matches_module_entry() -> None:
    via_class = TemporalResolver().resolve_schedule_time(
        message="延迟十分钟",
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    via_fn = resolve_schedule_time(
        message="延迟十分钟",
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING,
    )
    assert via_class.run_at == via_fn.run_at
    assert via_class.mode == via_fn.mode
