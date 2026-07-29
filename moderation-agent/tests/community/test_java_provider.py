import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from community.providers.java import JavaCommunityDataProvider
from moderation.schemas import ModerationTaskDetail


@pytest.mark.asyncio
async def test_reads_java_context_with_service_secret() -> None:
    captured: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.url.path,
                request.headers.get("X-Moderation-Service-Secret"),
            )
        )
        if request.url.path.endswith("/context"):
            return httpx.Response(
                200,
                json={
                    "current": {
                        "content_id": "42",
                        "content_type": "POST",
                        "author_id": "7",
                        "content": "真实帖子正文",
                        "title": "帖子标题",
                        "audit_status": "reviewing",
                    },
                    "post": {
                        "content_id": "42",
                        "content_type": "POST",
                        "author_id": "7",
                        "content": "真实帖子正文",
                        "title": "帖子标题",
                        "audit_status": "reviewing",
                    },
                    "parent_comment_required": False,
                },
            )
        if request.url.path.endswith("/contents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "content_id": "41",
                        "content_type": "POST",
                        "author_id": "7",
                        "content": "作者上一篇帖子",
                        "audit_status": "published",
                    }
                ],
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.AsyncClient(
        base_url="http://java.test",
        transport=httpx.MockTransport(handler),
    )
    provider = JavaCommunityDataProvider(
        base_url="http://java.test",
        auth_token="shared-secret",
        client=client,
    )
    try:
        context = await provider.get_content_context("42")
        recent = await provider.get_author_recent_contents("7", 5)
    finally:
        await provider.close()

    assert context.current.content == "真实帖子正文"
    assert recent[0].content_id == "41"
    assert captured == [
        (
            "/api/v1/internal/moderation/contents/42/context",
            "shared-secret",
        ),
        (
            "/api/v1/internal/moderation/authors/7/contents",
            "shared-secret",
        ),
    ]


@pytest.mark.asyncio
async def test_apply_moderation_result_posts_authenticated_callback() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["service_secret"] = request.headers.get(
            "X-Moderation-Service-Secret"
        )
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = httpx.AsyncClient(
        base_url="http://java.test",
        transport=httpx.MockTransport(handler),
    )
    provider = JavaCommunityDataProvider(
        base_url="http://java.test",
        auth_token="shared-secret",
        client=client,
    )
    task_id = uuid4()
    now = datetime.now(UTC)
    task = ModerationTaskDetail.model_validate(
        {
            "id": task_id,
            "thread_id": str(task_id),
            "status": "COMPLETED",
            "content": "待审核正文",
            "content_type": "POST",
            "final_action": "PASS",
            "version": 2,
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
            "content_id": "9527",
            "platform": "zhiguang",
            "creator_id": "42",
            "human_decision": {
                "action": "PASS",
                "reviewer_id": "admin:42",
                "comment": "人工复审通过",
            },
        }
    )

    try:
        await provider.apply_moderation_result(task)
    finally:
        await provider.close()

    assert captured == {
        "path": f"/api/v1/internal/moderation/tasks/{task_id}/result",
        "service_secret": "shared-secret",
        "body": {
            "content_id": "9527",
            "status": "COMPLETED",
            "final_action": "PASS",
            "reason": "人工复审通过",
        },
    }
