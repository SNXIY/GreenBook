"""Phase 1 write-tool contracts: verified state, not optimistic success.

All downstream doubles in this file are test-only.  Production handlers must
use the Java Agent Facade and return RESULT_UNKNOWN when read-back cannot
prove the requested postcondition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.context import PendingApproval, SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_java_client.models import (
    AgentCommentResponse,
    DraftResponse,
    PublishResponse,
    ScheduledPublicationResponse,
)
from greenbook_mcp_server.context import ToolContext
from greenbook_mcp_server.tool_schemas import UpdateDraftArguments, UpdateScheduleArguments
from greenbook_mcp_server.tools import content, interaction, publication


def _ctx(java: Any, *, approval: bool = False) -> ToolContext:
    return ToolContext(
        auth=AuthContext(
            user_id="user-1",
            tenant_id="tenant-1",
            raw_access_token="token",
        ),
        session=SessionContext(
            conversation_id="conversation-1",
            user_id="user-1",
            tenant_id="tenant-1",
        ),
        java=java,
        trace_id="trace-1",
        approval_granted=approval,
    )


class _DraftJava:
    def __init__(self, *, apply_updates: bool = True, verify_delete: bool = True) -> None:
        self.apply_updates = apply_updates
        self.verify_delete = verify_delete
        self.update_requests: list[Any] = []
        self.deleted = False
        self.draft = DraftResponse(
            draftId="draft-1",
            title="Original title",
            content="Original body",
            status="draft",
            version=1,
            updatedAt=datetime(2026, 8, 15, 8, 0, tzinfo=UTC),
        )

    async def get_draft(self, draft_id: str, **_: Any) -> ToolResult[DraftResponse]:
        assert draft_id == "draft-1"
        if self.deleted and self.verify_delete:
            return ToolResult.success(
                self.draft.model_copy(update={"status": "deleted", "version": 2})
            )
        return ToolResult.success(self.draft)

    async def update_draft(self, draft_id: str, request: Any, **_: Any) -> ToolResult[DraftResponse]:
        assert draft_id == "draft-1"
        self.update_requests.append(request)
        if self.apply_updates:
            self.draft = self.draft.model_copy(
                update={
                    "title": request.title if request.title is not None else self.draft.title,
                    "content": request.content if request.content is not None else self.draft.content,
                    "version": (self.draft.version or 0) + 1,
                    "updated_at": (self.draft.updated_at or datetime.now(UTC)) + timedelta(seconds=1),
                }
            )
        return ToolResult.success(self.draft, receipt_id="update-receipt")

    async def delete_draft(self, draft_id: str, **_: Any) -> ToolResult[dict[str, Any]]:
        assert draft_id == "draft-1"
        self.deleted = True
        return ToolResult.success({}, receipt_id="delete-receipt")


@pytest.mark.asyncio
async def test_update_draft_is_partial_and_completed_only_after_readback() -> None:
    java = _DraftJava()
    ctx = _ctx(java)

    result = await content.update_draft(ctx, draft_id="draft-1", title="Better title")

    assert result.ok is True
    assert result.data == {
        "draft_id": "draft-1",
        "title": "Better title",
        "content": "Original body",
        "summary": None,
        "status": "draft",
        "version": 2,
        "updated_at": "2026-08-15T08:00:01+00:00",
    }
    assert java.update_requests[0].title == "Better title"
    assert java.update_requests[0].content is None
    assert result.operation_receipt is not None
    assert result.operation_receipt.semantic_action == "UPDATE_DRAFT"
    assert result.operation_receipt.result_known is True
    assert result.operation_receipt.status == "COMPLETED"


@pytest.mark.asyncio
async def test_update_draft_readback_mismatch_is_result_unknown_not_completed() -> None:
    java = _DraftJava(apply_updates=False)
    ctx = _ctx(java)

    result = await content.update_draft(ctx, draft_id="draft-1", title="Expected title")

    assert result.ok is False
    assert result.code == "RESULT_UNKNOWN"
    assert result.operation_receipt is not None
    assert result.operation_receipt.result_known is False
    assert result.operation_receipt.status == "RESULT_UNKNOWN"


@pytest.mark.asyncio
async def test_delete_draft_requires_approval_and_verifies_soft_delete() -> None:
    java = _DraftJava()
    blocked = await content.delete_draft(_ctx(java), draft_id="draft-1")
    assert blocked.ok is False
    assert blocked.code == "APPROVAL_REQUIRED"
    assert java.deleted is False

    ctx = _ctx(java, approval=True)
    ctx.session.active_draft_id = "draft-1"
    result = await content.delete_draft(ctx, draft_id="draft-1")

    assert result.ok is True
    assert result.data == {"draft_id": "draft-1", "status": "deleted"}
    assert ctx.session.active_draft_id is None
    assert result.operation_receipt is not None
    assert result.operation_receipt.semantic_action == "DELETE_DRAFT"


@pytest.mark.asyncio
async def test_delete_draft_without_readback_confirmation_is_result_unknown() -> None:
    java = _DraftJava(verify_delete=False)
    result = await content.delete_draft(_ctx(java, approval=True), draft_id="draft-1")

    assert result.ok is False
    assert result.code == "RESULT_UNKNOWN"


class _ScheduleJava:
    def __init__(self) -> None:
        self.schedule = ScheduledPublicationResponse(
            scheduleId="schedule-1",
            draftId="draft-1",
            runAt=datetime(2026, 8, 16, 1, 0, tzinfo=UTC),
            timezone="Asia/Shanghai",
            status="SCHEDULED",
            version=3,
        )

    async def get_schedule(self, schedule_id: str, **_: Any) -> ToolResult[ScheduledPublicationResponse]:
        assert schedule_id == "schedule-1"
        return ToolResult.success(self.schedule)

    async def cancel_schedule(self, schedule_id: str, **_: Any) -> ToolResult[dict[str, Any]]:
        assert schedule_id == "schedule-1"
        # Simulate a downstream 204 that did not produce the requested
        # business postcondition. The handler must not report cancellation.
        return ToolResult.success({}, receipt_id="cancel-receipt")


@pytest.mark.asyncio
async def test_cancel_schedule_requires_verified_cancelled_state() -> None:
    ctx = _ctx(_ScheduleJava())
    ctx.session.active_schedule_id = "schedule-1"

    result = await publication.cancel_schedule(ctx, schedule_id="schedule-1")

    assert result.ok is False
    assert result.code == "RESULT_UNKNOWN"
    assert ctx.session.active_schedule_id == "schedule-1"


class _UpdatingScheduleJava:
    def __init__(self) -> None:
        self.schedule = ScheduledPublicationResponse(
            scheduleId="schedule-1",
            draftId="draft-1",
            runAt=datetime(2026, 8, 15, 8, 0, tzinfo=UTC),
            timezone="Asia/Shanghai",
            status="SCHEDULED",
            version=3,
        )
        self.requests: list[Any] = []

    async def get_schedule(self, schedule_id: str, **_: Any) -> ToolResult[ScheduledPublicationResponse]:
        assert schedule_id == "schedule-1"
        return ToolResult.success(self.schedule)

    async def update_schedule(self, schedule_id: str, request: Any, **_: Any) -> ToolResult[ScheduledPublicationResponse]:
        assert schedule_id == "schedule-1"
        self.requests.append(request)
        self.schedule = self.schedule.model_copy(update={
            "run_at": datetime.fromisoformat(request.run_at.replace("Z", "+00:00")),
            "version": 4,
        })
        return ToolResult.success(self.schedule, receipt_id="update-schedule-receipt")


@pytest.mark.asyncio
async def test_update_schedule_uses_java_schedule_as_existing_time_base() -> None:
    java = _UpdatingScheduleJava()

    result = await publication.update_schedule(
        _ctx(java),
        schedule_id="schedule-1",
        run_at="比原计划晚十分钟",
        temporal_base="EXISTING_SCHEDULE_TIME",
    )

    assert result.ok is True
    assert java.requests[0].run_at == "2026-08-15T08:10:00Z"
    assert result.data["run_at"] == "2026-08-15T08:10:00+00:00"
    assert result.operation_receipt is not None
    assert result.operation_receipt.result_known is True


class _PublishJava:
    def __init__(self, *, published: bool) -> None:
        self.published = published

    async def publish_now(self, *_: Any, **__: Any) -> ToolResult[PublishResponse]:
        return ToolResult.success(
            PublishResponse(postId="draft-1", status="published"),
            receipt_id="publish-receipt",
        )

    async def get_draft(self, draft_id: str, **_: Any) -> ToolResult[DraftResponse]:
        assert draft_id == "draft-1"
        return ToolResult.success(DraftResponse(
            draftId="draft-1",
            title="Published draft",
            content="Body",
            status="published" if self.published else "draft",
        ))


@pytest.mark.asyncio
async def test_publish_now_requires_authoritative_published_readback() -> None:
    completed = await publication.publish_now_execute(_ctx(_PublishJava(published=True)), "draft-1")
    assert completed.ok is True
    assert completed.operation_receipt is not None
    assert completed.operation_receipt.semantic_action == "PUBLISH_NOW"
    assert completed.operation_receipt.result_known is True

    unknown = await publication.publish_now_execute(_ctx(_PublishJava(published=False)), "draft-1")
    assert unknown.ok is False
    assert unknown.code == "RESULT_UNKNOWN"
    assert unknown.operation_receipt is not None
    assert unknown.operation_receipt.result_known is False


class _ReplyJava:
    def __init__(self, *, matches: bool = True) -> None:
        self.matches = matches
        self.created = AgentCommentResponse(
            id="reply-1",
            postId="post-1",
            parentId="parent-1",
            content="谢谢你的分享",
        )

    async def reply_to_comment(self, *_: Any, **__: Any) -> ToolResult[AgentCommentResponse]:
        return ToolResult.success(self.created, receipt_id="reply-receipt")

    async def get_comment(self, comment_id: str, **_: Any) -> ToolResult[AgentCommentResponse]:
        assert comment_id == "reply-1"
        if self.matches:
            return ToolResult.success(self.created)
        return ToolResult.success(self.created.model_copy(update={"content": "other"}))


@pytest.mark.asyncio
async def test_reply_requires_authoritative_comment_readback() -> None:
    ctx = _ctx(_ReplyJava(), approval=True)
    ctx.session.pending_approval = PendingApproval(
        approval_id="approval-1",
        operation="interaction.send_reply",
        resource_id="parent-1",
        description="reply",
    )

    completed = await interaction.send_reply(
        ctx,
        post_id="post-1",
        parent_comment_id="parent-1",
        content="谢谢你的分享",
    )
    assert completed.ok is True
    assert completed.operation_receipt is not None
    assert completed.operation_receipt.semantic_action == "REPLY_COMMENT"
    assert completed.operation_receipt.result_known is True

    unknown_ctx = _ctx(_ReplyJava(matches=False), approval=True)
    unknown_ctx.session.pending_approval = PendingApproval(
        approval_id="approval-2",
        operation="interaction.send_reply",
        resource_id="parent-1",
        description="reply",
    )
    unknown = await interaction.send_reply(
        unknown_ctx,
        post_id="post-1",
        parent_comment_id="parent-1",
        content="谢谢你的分享",
    )
    assert unknown.ok is False
    assert unknown.code == "RESULT_UNKNOWN"


def test_tool_schema_normalizes_camel_case_at_boundary() -> None:
    update = UpdateDraftArguments.model_validate(
        {"draftId": "draft-1", "body": "New body"}
    )
    assert update.draft_id == "draft-1"
    assert update.content == "New body"

    schedule = UpdateScheduleArguments.model_validate(
        {"scheduleId": "schedule-1", "publish_at": "2026-08-16T01:00:00Z"}
    )
    assert schedule.schedule_id == "schedule-1"
    assert schedule.run_at == "2026-08-16T01:00:00Z"
