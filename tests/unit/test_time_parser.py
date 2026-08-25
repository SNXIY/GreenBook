from datetime import datetime
from zoneinfo import ZoneInfo

from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
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


def test_english_relative_schedule_from_now() -> None:
    """Real-chain regression: a reasoning model rendered the temporal
    constraint in English ("5 minutes from now"); the execution boundary must
    resolve it to an ISO instant instead of forwarding natural language to the
    Java scheduler (observed: SERVER_FAILURE)."""
    now = datetime(2026, 8, 14, 12, 45, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "5 minutes from now",
        now=now,
    ) == "2026-08-14T04:50:00Z"


def test_english_relative_schedule_in_preposition() -> None:
    now = datetime(2026, 8, 14, 12, 45, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "Schedule the draft for publication in 2 hours",
        now=now,
    ) == "2026-08-14T06:45:00Z"


def test_english_relative_schedule_later() -> None:
    now = datetime(2026, 8, 14, 12, 45, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "publish 1 day later",
        now=now,
    ) == "2026-08-15T04:45:00Z"


def test_chinese_absolute_schedule_allows_spaces_around_clock_marker() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "明天下午 2 点发布",
        now=now,
    ) == "2026-08-21T06:00:00Z"


def test_chinese_relative_schedule_supports_yihou_suffix() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "五分钟以后发布",
        now=now,
    ) == "2026-08-20T02:05:00Z"


def test_v1_chinese_relative_schedule_contract() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time("五分钟后发布", now=now) == "2026-08-20T02:05:00Z"
    assert parse_natural_schedule_time("10分钟后发布", now=now) == "2026-08-20T02:10:00Z"


def test_chinese_relative_schedule_supports_leading_waiting_verbs() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time("过五分钟把刚才那篇发出去", now=now) == "2026-08-20T02:05:00Z"
    assert parse_natural_schedule_time("等五分钟再发", now=now) == "2026-08-20T02:05:00Z"


def test_v1_chinese_absolute_schedule_contract() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time("明天上午九点发布", now=now) == "2026-08-21T01:00:00Z"
    assert parse_natural_schedule_time("后天下午两点发布", now=now) == "2026-08-22T06:00:00Z"


def test_english_absolute_schedule_and_explicit_timezone() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert parse_natural_schedule_time(
        "tomorrow at 2 PM",
        now=now,
    ) == "2026-08-21T06:00:00Z"
    assert parse_natural_schedule_time(
        "at 14:00 JST tomorrow",
        now=now,
    ) == "2026-08-21T05:00:00Z"


def test_unresolved_temporal_input_is_not_treated_as_no_temporal_requirement() -> None:
    resolution = TemporalResolver(
        now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ).resolve_result("sometime later")

    assert resolution.intent == "FUTURE"
    assert resolution.temporal_kind == "UNRESOLVED"
    assert resolution.resolved is False
    assert resolution.run_at is None


def test_current_day_is_now_only_with_explicit_immediate_intent() -> None:
    resolver = TemporalResolver(
        now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    immediate = resolver.resolve_result("今天", immediate=True)
    assert immediate.intent == "NOW"
    assert immediate.temporal_kind == "NOW"
    assert immediate.resolved is True

    unresolved = resolver.resolve_result("今天")
    assert unresolved.intent == "FUTURE"
    assert unresolved.temporal_kind == "UNRESOLVED"
    assert unresolved.resolved is False


def test_canonical_iso_instant_is_not_shifted_when_reparsed() -> None:
    assert parse_natural_schedule_time(
        "2026-08-21T06:00:00Z",
        timezone="Asia/Shanghai",
    ) == "2026-08-21T06:00:00Z"
