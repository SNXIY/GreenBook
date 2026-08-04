from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


@dataclass(frozen=True)
class CapabilityGrant:
    token: str
    capability_id: str
    expires_at: str


class CommunityClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        timeout = httpx.Timeout(
            connect=float(
                getattr(settings, "tool_http_connect_timeout_seconds", 5.0)
            ),
            read=float(getattr(settings, "tool_http_read_timeout_seconds", 30.0)),
            write=float(getattr(settings, "tool_http_read_timeout_seconds", 30.0)),
            pool=float(getattr(settings, "tool_http_pool_timeout_seconds", 5.0)),
        )
        client_kwargs: dict[str, Any] = {
            "base_url": settings.java_base_url.rstrip("/"),
            "timeout": timeout,
            "trust_env": bool(getattr(settings, "tool_http_trust_env", False)),
            "verify": bool(getattr(settings, "tool_http_verify_tls", True)),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        elif getattr(settings, "tool_http_proxy", None):
            client_kwargs["proxy"] = settings.tool_http_proxy
        self.http = httpx.AsyncClient(**client_kwargs)
        self.headers = {
            "X-Assistant-Service-Secret": settings.service_shared_secret
        }

    async def close(self) -> None:
        await self.http.aclose()

    async def issue_capability(
        self,
        *,
        access_token: str,
        run_id: str,
        actions: list[str],
        resources: list[str],
        ttl_seconds: int = 120,
        max_uses: int = 1,
        trace_id: str | None = None,
    ) -> CapabilityGrant:
        response = await self.http.post(
            "/api/v1/assistant-tools/capabilities",
            headers={
                **self.headers,
                "Authorization": f"Bearer {access_token}",
                **({"X-Trace-ID": trace_id} if trace_id else {}),
            },
            json={
                "runId": run_id,
                "actions": actions,
                "resources": resources,
                "ttlSeconds": max(30, min(ttl_seconds, 604_800)),
                "maxUses": max(1, min(max_uses, 5)),
            },
        )
        response.raise_for_status()
        payload = response.json()
        return CapabilityGrant(
            token=str(payload["token"]),
            capability_id=str(payload["capabilityId"]),
            expires_at=str(payload["expiresAt"]),
        )

    async def revoke_capability(
        self, *, access_token: str, capability_id: str
    ) -> None:
        response = await self.http.delete(
            f"/api/v1/assistant-tools/capabilities/{capability_id}",
            headers={
                **self.headers,
                "Authorization": f"Bearer {access_token}",
            },
        )
        response.raise_for_status()

    def _capability_headers(
        self, capability_token: str, trace_id: str | None = None
    ) -> dict[str, str]:
        return {
            **self.headers,
            "X-Assistant-Capability": capability_token,
            **({"X-Trace-ID": trace_id} if trace_id else {}),
        }

    async def search_posts(
        self,
        query: str,
        limit: int,
        *,
        capability_token: str,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        response = await self.http.get(
            "/api/v1/assistant-tools/posts/search",
            params={"q": query, "limit": max(1, min(limit, 10))},
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return list(response.json())

    async def get_post(
        self,
        post_id: str,
        *,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.get(
            f"/api/v1/assistant-tools/posts/{post_id}",
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return dict(response.json())

    async def get_own_draft(
        self,
        post_id: str,
        *,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.get(
            f"/api/v1/assistant-tools/posts/{post_id}/draft-content",
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return dict(response.json())

    async def analyze_engagement(
        self,
        *,
        topic: str | None,
        days: int,
        limit: int,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "days": max(1, min(days, 365)),
            "limit": max(1, min(limit, 20)),
        }
        if topic:
            params["topic"] = topic
        response = await self.http.get(
            "/api/v1/assistant-tools/analytics/engagement",
            params=params,
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return dict(response.json())

    async def list_active_users(
        self,
        *,
        days: int,
        limit: int,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.get(
            "/api/v1/assistant-tools/analytics/active-users",
            params={
                "days": max(1, min(days, 365)),
                "limit": max(1, min(limit, 20)),
            },
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return dict(response.json())

    async def list_posts_by_users(
        self,
        *,
        user_ids: list[str],
        days: int,
        limit: int,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.get(
            "/api/v1/assistant-tools/analytics/user-posts",
            params={
                "userIds": user_ids,
                "days": max(1, min(days, 365)),
                "limitPerUser": max(1, min(limit, 20)),
            },
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return {"posts": list(response.json())}

    async def aggregate_post_topics(
        self,
        *,
        user_ids: list[str],
        days: int,
        limit: int,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.get(
            "/api/v1/assistant-tools/analytics/post-topics",
            params={
                "userIds": user_ids,
                "days": max(1, min(days, 365)),
                "limit": max(1, min(limit, 20)),
            },
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return {"topics": list(response.json())}

    async def list_own_posts(
        self,
        *,
        limit: int,
        offset: int,
        capability_token: str,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        response = await self.http.get(
            "/api/v1/assistant-tools/posts/mine",
            params={
                "limit": max(1, min(limit, 100)),
                "offset": max(0, offset),
            },
            headers=self._capability_headers(capability_token, trace_id),
        )
        response.raise_for_status()
        return list(response.json())

    async def publish_ai_draft(
        self,
        *,
        post_id: str,
        creator_id: str,
        idempotency_key: str,
        capability_token: str,
        expected_content_sha256: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"/api/v1/assistant-tools/posts/{post_id}/publish",
            headers={
                **self._capability_headers(capability_token, trace_id),
                "Idempotency-Key": idempotency_key,
            },
            json={
                "creatorId": creator_id,
                "expectedContentSha256": expected_content_sha256,
            },
        )
        response.raise_for_status()
        return dict(response.json())

    async def reply_comment(
        self,
        *,
        post_id: str,
        parent_comment_id: str,
        content: str,
        assistant_run_id: str,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.post(
            "/api/v1/assistant-tools/comments/replies",
            headers={
                **self._capability_headers(capability_token, trace_id),
                "Idempotency-Key": f"assistant-comment-{assistant_run_id}",
            },
            json={
                "postId": post_id,
                "parentCommentId": parent_comment_id,
                "assistantRunId": assistant_run_id,
                "content": content[:1000],
            },
        )
        response.raise_for_status()
        return dict(response.json())

    async def delete_post(
        self,
        *,
        post_id: str,
        idempotency_key: str,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.delete(
            f"/api/v1/assistant-tools/posts/{post_id}",
            headers={
                **self._capability_headers(capability_token, trace_id),
                "Idempotency-Key": idempotency_key,
            },
        )
        response.raise_for_status()
        return {"post_id": post_id, "status": "deleted"}

    async def delete_posts_batch(
        self,
        *,
        post_ids: list[str],
        idempotency_key: str,
        capability_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.http.post(
            "/api/v1/assistant-tools/posts/batch-delete",
            headers={
                **self._capability_headers(capability_token, trace_id),
                "Idempotency-Key": idempotency_key,
            },
            json={"postIds": post_ids},
        )
        response.raise_for_status()
        return dict(response.json())


class CreatorClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.creator_base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def create_draft(
        self,
        *,
        instruction: str,
        references: list[dict[str, Any]],
        access_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        task = await self.submit_draft(
            instruction=instruction,
            references=references,
            access_token=access_token,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        task_id = str(task["task_id"])
        snapshot: dict[str, Any] = {}
        deadline = asyncio.get_running_loop().time() + self.settings.creator_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.get_task(
                task_id,
                access_token=access_token,
                trace_id=trace_id,
            )
            status = snapshot.get("status")
            if status == "COMPLETED":
                break
            if status in {"FAILED", "CANCELLED"}:
                raise RuntimeError(
                    f"Creator task {task_id} ended with {status}: "
                    f"{snapshot.get('error_message') or snapshot.get('error_code') or ''}"
                )
            await asyncio.sleep(1.25)
        else:
            raise TimeoutError(f"Creator task {task_id} did not finish in time")
        return await self.create_handoff(
            task_id=task_id,
            snapshot=snapshot,
            access_token=access_token,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def submit_draft(
        self,
        *,
        instruction: str,
        references: list[dict[str, Any]],
        access_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        compact: list[dict[str, str]] = []
        if references:
            compact = [
                {
                    "id": str(item.get("id") or item.get("post_id") or "")[:256],
                    "title": _safe_reference_text(item.get("title"), 200),
                    "summary": _safe_reference_text(
                        item.get("description") or item.get("summary"), 600
                    ),
                    "body_excerpt": _safe_reference_text(
                        item.get("body_markdown") or item.get("body"), 1_600
                    ),
                }
                for item in references[:8]
                if item.get("id") or item.get("post_id")
            ]
        return await self._request(
            "POST",
            "/api/v1/creator/tasks",
            access_token,
            headers={
                "Idempotency-Key": idempotency_key,
                **({"X-Trace-ID": trace_id} if trace_id else {}),
            },
            json={
                "kind": "CREATE_CONTENT",
                "goal": instruction,
                "constraints": {
                    "interaction_mode": "AUTO",
                    "execution_profile": "ASSISTANT_BALANCED",
                    "format": "POST",
                    "target_length": 1200,
                    "tone": "PRACTICAL",
                    "reference_evidence": compact,
                    "audience": "知光知识社区用户",
                    "reader_takeaway": "读完后能获得清晰、可执行的方法",
                },
                "source_scope": {
                    "include_creator_profile": False,
                    "include_creator_history": False,
                    "include_community_posts": False,
                },
            },
        )

    async def get_task(
        self,
        task_id: str,
        *,
        access_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/creator/tasks/{task_id}",
            access_token,
            headers=({"X-Trace-ID": trace_id} if trace_id else None),
        )

    async def create_handoff(
        self,
        *,
        task_id: str,
        snapshot: dict[str, Any],
        access_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        artifact_id = str(snapshot.get("final_artifact_id") or "").strip()
        artifact = (
            await self.get_artifact(
                task_id,
                artifact_id,
                access_token=access_token,
                trace_id=trace_id,
            )
            if artifact_id
            else {}
        )
        document = _creator_document(artifact)
        handoff = await self._request(
            "POST",
            f"/api/v1/creator/tasks/{task_id}/publication-handoffs",
            access_token,
            headers={
                "Idempotency-Key": f"{idempotency_key}-handoff",
                **({"X-Trace-ID": trace_id} if trace_id else {}),
            },
            json={"source_artifact_id": snapshot.get("final_artifact_id")},
        )
        return {
            "task_id": task_id,
            "draft_id": str(handoff["external_draft_id"]),
            "title": handoff.get("title"),
            "handoff_id": handoff.get("handoff_id"),
            "status": handoff.get("status"),
            "content_sha256": str(handoff["source_content_sha256"]),
            "description": document.get("description"),
            "body_markdown": document.get("body_markdown"),
        }

    async def get_artifact(
        self,
        task_id: str,
        artifact_id: str,
        *,
        access_token: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/creator/tasks/{task_id}/artifacts/{artifact_id}",
            access_token,
            headers=({"X-Trace-ID": trace_id} if trace_id else None),
        )

    async def wait_for_terminal_event(
        self,
        task_id: str,
        *,
        access_token: str,
        trace_id: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {access_token}",
            **({"X-Trace-ID": trace_id} if trace_id else {}),
        }
        async with asyncio.timeout(timeout_seconds):
            initial = await self.get_task(
                task_id,
                access_token=access_token,
                trace_id=trace_id,
            )
            if initial.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                return initial
            async with self.http.stream(
                "GET",
                f"/api/v1/creator/tasks/{task_id}/events",
                headers=headers,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    snapshot = await self.get_task(
                        task_id,
                        access_token=access_token,
                        trace_id=trace_id,
                    )
                    if snapshot.get("status") in {
                        "COMPLETED",
                        "FAILED",
                        "CANCELLED",
                    }:
                        return snapshot
        return None

    async def cancel_task(
        self,
        task_id: str,
        *,
        access_token: str,
        trace_id: str | None = None,
    ) -> None:
        snapshot = await self.get_task(
            task_id,
            access_token=access_token,
            trace_id=trace_id,
        )
        if snapshot.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
            return
        await self._request(
            "POST",
            f"/api/v1/creator/tasks/{task_id}/cancel",
            access_token,
            headers=({"X-Trace-ID": trace_id} if trace_id else None),
            json={"expected_version": int(snapshot["version"])},
        )

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.http.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {access_token}",
                **(headers or {}),
            },
            json=json,
        )
        response.raise_for_status()
        return dict(response.json())


def _safe_reference_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    return text[:limit]


def _creator_document(artifact: dict[str, Any]) -> dict[str, str | None]:
    content = artifact.get("content")
    if not isinstance(content, dict):
        return {"description": None, "body_markdown": None}
    document = content.get("document")
    if not isinstance(document, dict):
        document = content
    body = (
        document.get("body_markdown")
        or document.get("content_markdown")
        or document.get("body")
    )
    description = document.get("description") or document.get("summary")
    return {
        "description": _safe_reference_text(description, 2_000) or None,
        "body_markdown": _safe_reference_text(body, 524_288) or None,
    }
