"""Offline contract tests for the Agent -> Python -> Java draft boundary."""

from __future__ import annotations

import pytest
from greenbook_java_client.models import (
    DESCRIPTION_MAX_LENGTH,
    AgentDraftCreateRequest,
)
from greenbook_mcp_server.tools.content import normalize_text_artifact
from pydantic import ValidationError


def test_description_exact_contract_limit_is_valid() -> None:
    request = AgentDraftCreateRequest(
        title="A draft",
        content="# Body",
        summary="x" * DESCRIPTION_MAX_LENGTH,
    )
    assert len(request.summary or "") == DESCRIPTION_MAX_LENGTH


def test_description_over_contract_limit_is_rejected_before_java() -> None:
    with pytest.raises(ValidationError):
        normalize_text_artifact(
            {
                "title": "A draft",
                "description": "x" * (DESCRIPTION_MAX_LENGTH + 1),
                "body_markdown": "# Body",
            },
            fallback_title="fallback",
            fallback_content="# Body",
        )


def test_description_is_not_silently_truncated() -> None:
    long_summary = "x" * (DESCRIPTION_MAX_LENGTH + 1)
    with pytest.raises(ValidationError):
        AgentDraftCreateRequest(title="A draft", content="# Body", summary=long_summary)
