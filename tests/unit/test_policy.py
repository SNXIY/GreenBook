"""Unit tests for risk-based tool policy."""

from __future__ import annotations

from greenbook_security.policy import RiskLevel, requires_approval, tool_risk_level


def test_read_tools_are_low_risk():
    assert tool_risk_level("community.search_public_posts") == RiskLevel.LOW
    assert tool_risk_level("community.get_post") == RiskLevel.LOW
    assert tool_risk_level("content.get_draft") == RiskLevel.LOW
    assert tool_risk_level("analytics.get_account_summary") == RiskLevel.LOW


def test_write_tools_are_medium_risk():
    assert tool_risk_level("content.create_draft") == RiskLevel.MEDIUM
    assert tool_risk_level("publication.schedule") == RiskLevel.MEDIUM
    assert tool_risk_level("publication.cancel_schedule") == RiskLevel.MEDIUM


def test_high_risk_tools_require_approval():
    assert requires_approval("publication.publish_now") is True
    assert requires_approval("interaction.send_reply") is True


def test_read_tools_no_approval():
    assert requires_approval("community.search_public_posts") is False
    assert requires_approval("analytics.get_post_performance") is False


def test_medium_tools_no_approval():
    assert requires_approval("content.create_draft") is False
    assert requires_approval("publication.schedule") is False


def test_unknown_tool_defaults_high():
    assert tool_risk_level("unknown.weird_tool") == RiskLevel.HIGH
    assert requires_approval("unknown.weird_tool") is True
