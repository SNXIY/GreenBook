"""Phase-2 control-plane router: QUERY / ACTION / CHAT only."""

from __future__ import annotations

from app.router import ControlPlaneRouter


def test_publish_post_routes_to_action() -> None:
    router = ControlPlaneRouter()
    decision = router.classify("发布帖子")
    assert decision.mode == "ACTION"
    assert decision.confidence >= 0.9


def test_how_many_posts_routes_to_query() -> None:
    router = ControlPlaneRouter()
    decision = router.classify("我发布多少帖子")
    assert decision.mode == "QUERY"
    assert decision.domain == "content"
    assert decision.confidence >= 0.9


def test_introduce_agent_routes_to_chat() -> None:
    router = ControlPlaneRouter()
    decision = router.classify("介绍Agent")
    assert decision.mode == "CHAT"
    assert decision.confidence >= 0.9


def test_scheduled_publish_request_is_action() -> None:
    router = ControlPlaneRouter()
    decision = router.classify(
        "明天上午八点发布一篇关于如何学习Agent设计的帖子"
    )
    assert decision.mode == "ACTION"


def test_help_introduce_agent_is_chat() -> None:
    router = ControlPlaneRouter()
    decision = router.classify("帮我介绍Agent")
    assert decision.mode == "CHAT"


def test_search_then_create_stays_on_action_path() -> None:
    router = ControlPlaneRouter()
    decision = router.classify("search posts and create a draft")
    assert decision.mode == "ACTION"
