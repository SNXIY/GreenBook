"""Contract tests: Java OpenAPI model validation."""

from __future__ import annotations

import pytest
from greenbook_java_client.models import (
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    AgentErrorResponse,
    DraftResponse,
    ScheduleCreateRequest,
    ScheduleStatus,
    ScheduledPublicationResponse,
    SearchPageResponse,
)


def test_draft_create_request():
    req = AgentDraftCreateRequest(
        title="Test Draft",
        content="# Hello\nWorld",
        summary="A test summary",
    )
    data = req.model_dump(mode="json", by_alias=True)
    assert data["title"] == "Test Draft"
    assert data["content"] == "# Hello\nWorld"
    assert data["summary"] == "A test summary"


def test_draft_update_request_with_expected_version():
    req = AgentDraftUpdateRequest(
        title="Updated",
        content="New content",
        expectedVersion="2026-08-06T10:00:00Z",
    )
    data = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert data["title"] == "Updated"
    assert data["expectedVersion"] == "2026-08-06T10:00:00Z"


def test_schedule_create_request():
    req = ScheduleCreateRequest(
        draftId="123",
        runAt="2026-08-07T09:00:00+08:00",
        timezone="Asia/Shanghai",
    )
    data = req.model_dump(mode="json", by_alias=True)
    assert data["draftId"] == "123"
    assert data["runAt"] == "2026-08-07T09:00:00+08:00"


def test_draft_response_deserialization():
    json_data = {
        "draftId": "456",
        "ownerId": "user-1",
        "title": "My Draft",
        "content": "Body content",
        "version": 1,
        "status": "DRAFT",
        "createdAt": "2026-08-06T08:00:00Z",
        "updatedAt": "2026-08-06T09:00:00Z",
    }
    draft = DraftResponse.model_validate(json_data)
    assert draft.draft_id == "456"
    assert draft.owner_id == "user-1"
    assert draft.version == 1
    assert draft.updated_at is not None


def test_search_page_response():
    json_data = {
        "items": [
            {
                "postId": "340415383330754560",
                "title": "RAG实践分享",
                "summary": "一篇关于RAG的帖子",
                "tags": ["RAG", "AI"],
                "likeCount": 42,
                "commentCount": 7,
                "favoriteCount": 15,
                "publishedAt": "2026-08-01T10:00:00Z",
                "hotScore": 9.5,
            }
        ],
        "page": 1,
        "size": 20,
        "total": 1,
        "totalPages": 1,
        "hasMore": False,
        "sort": "hot",
    }
    page = SearchPageResponse.model_validate(json_data)
    assert len(page.items) == 1
    assert page.items[0].post_id == "340415383330754560"
    assert page.items[0].like_count == 42


def test_schedule_status_enum():
    assert ScheduleStatus.SCHEDULED.value == "SCHEDULED"
    assert ScheduleStatus.PROCESSING.value == "PROCESSING"
    assert ScheduleStatus.PUBLISHED.value == "PUBLISHED"
    assert ScheduleStatus.CANCELLED.value == "CANCELLED"
    assert ScheduleStatus.FAILED.value == "FAILED"


def test_agent_error_response():
    json_data = {
        "code": "DRAFT_VERSION_CONFLICT",
        "message": "Draft was modified by another process",
        "userMessage": "草稿已被他人修改，请刷新后重试",
        "retryable": False,
        "requestCommitted": True,
        "traceId": "abc123",
    }
    err = AgentErrorResponse.model_validate(json_data)
    assert err.code == "DRAFT_VERSION_CONFLICT"
    assert err.user_message == "草稿已被他人修改，请刷新后重试"
    assert err.retryable is False


def test_schedule_response_all_statuses():
    response = ScheduledPublicationResponse.model_validate({
        "scheduleId": "1",
        "draftId": "2",
        "status": "SCHEDULED",
        "version": 1,
    })
    assert response.status == "SCHEDULED"
    assert response.version == 1
