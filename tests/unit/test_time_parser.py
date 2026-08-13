from datetime import datetime
from zoneinfo import ZoneInfo

from greenbook_agent_core.time_parser import parse_natural_schedule_time


def test_next_weekday_schedule_is_resolved_in_user_timezone() -> None:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "下周一晚上8点发布",
        now=now,
    ) == "2026-08-17T12:00:00Z"


def test_next_weekday_on_same_weekday_means_following_week() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "下周一上午9点发布",
        now=now,
    ) == "2026-08-24T01:00:00Z"
