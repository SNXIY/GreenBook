from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenbook_assistant_core.time_parser import parse_natural_schedule_time

from apps.assistant_api.greenbook_assistant_api.api.routes import (
    _append_schedule_confirmation,
    _normalize_schedule_tool_args,
)


def test_tomorrow_morning_eight_is_converted_to_java_utc_run_at() -> None:
    now = datetime(2026, 8, 6, 22, 2, tzinfo=timezone(timedelta(hours=8)))

    assert parse_natural_schedule_time(
        "明天上午八点发布一篇关于如何学好 Java 的帖子",
        "Asia/Shanghai",
        now=now,
    ) == "2026-08-07T00:00:00Z"


def test_short_relative_delay_uses_the_user_timezone() -> None:
    now = datetime(2026, 8, 6, 22, 2, tzinfo=timezone(timedelta(hours=8)))

    assert parse_natural_schedule_time(
        "两分钟后发布一篇测试帖子",
        "Asia/Shanghai",
        now=now,
    ) == "2026-08-06T14:04:00Z"


def test_time_parser_does_not_accept_legacy_timezone_name() -> None:
    assert parse_natural_schedule_time(
        "明天上午八点发布",
        "timezone_name",
        now=datetime(2026, 8, 6, 22, 2, tzinfo=timezone(timedelta(hours=8))),
    ) is None


def test_schedule_tool_args_use_run_at_and_timezone_not_timezone_name() -> None:
    args = _normalize_schedule_tool_args(
        {"run_at": "2099-01-01T00:00:00Z", "timezone_name": "UTC"},
        user_message="明天上午八点发布",
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 8, 6, 22, 2, tzinfo=timezone(timedelta(hours=8))),
    )

    assert args == {
        "run_at": "2026-08-07T00:00:00Z",
        "timezone": "Asia/Shanghai",
    }


def test_frontend_confirmation_contains_verified_schedule_fields() -> None:
    result = _append_schedule_confirmation(
        "已完成。",
        draft={"draft_id": "draft-1", "title": "如何学好 Java"},
        schedule={
            "schedule_id": "schedule-1",
            "draft_id": "draft-1",
            "run_at": "2026-08-07T00:00:00Z",
            "timezone": "Asia/Shanghai",
            "status": "SCHEDULED",
        },
    )

    assert "标题：如何学好 Java" in result
    assert "draftId：draft-1" in result
    assert "scheduleId：schedule-1" in result
    assert "发布时间：2026年8月7日 08:00" in result
    assert "时区：Asia/Shanghai" in result
    assert "当前状态：SCHEDULED" in result
