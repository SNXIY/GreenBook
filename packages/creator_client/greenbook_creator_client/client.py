"""Async client for the Creator Agent Task API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from greenbook_contracts.tool_result import ResourceRef, ToolResult

logger = logging.getLogger(__name__)


class CreatorClient:
    """Async HTTPX client for the Creator Agent Task API.

    Creator is responsible for: Research → Outline → Writer → Critic →
    Revision → Finalize → Artifact → Checkpoint.

    Creator does NOT own: user identity, Java drafts, comments, publications,
    or scheduled publications.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8093",
        *,
        timeout: float = 240.0,
        poll_interval: float = 1.5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval
        self.http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
        )

    async def close(self) -> None:
        await self.http.aclose()

    # ── Task submission ────────────────────────────────────────

    async def create_task(
        self,
        *,
        kind: str,
        goal: str,
        constraints: dict[str, Any] | None = None,
        reference_notes: str = "",
        bearer_token: str | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult[dict[str, Any]]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if trace_id:
            headers["X-Trace-ID"] = trace_id

        body: dict[str, Any] = {
            "kind": kind,
            "goal": goal,
            "constraints": {
                "interaction_mode": "AUTO",
                "format": "POST",
                "target_length": 1200,
                "tone": "PRACTICAL",
                "audience": "知光知识社区用户",
                "reader_takeaway": "读完后能获得清晰、可执行的方法",
                **(constraints or {}),
            },
            "source_scope": {
                "include_creator_profile": False,
                "include_creator_history": False,
                "include_community_posts": False,
            },
        }
        if reference_notes:
            body["constraints"]["reference_notes"] = reference_notes[:12_000]

        try:
            resp = await self.http.post(
                "/api/v1/creator/tasks",
                json=body,
                headers=headers,
            )
        except httpx.ConnectError as exc:
            return ToolResult.dependency_unavailable(
                "Creator Agent is unreachable. No draft was created. You may safely retry.",
                trace_id=trace_id,
            )
        except httpx.TimeoutException:
            return ToolResult.timeout("Creator Agent request timed out")

        if 200 <= resp.status_code < 300:
            return ToolResult.success(resp.json(), trace_id=trace_id)

        return ToolResult.dependency_unavailable(
            f"Creator returned HTTP {resp.status_code}", trace_id=trace_id
        )

    # ── Wait for completion ────────────────────────────────────

    async def wait_for_completion(
        self,
        task_id: str,
        *,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        deadline_seconds: float = 240.0,
    ) -> ToolResult[dict[str, Any]]:
        """Poll or SSE-wait for Creator task to reach terminal state."""
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if trace_id:
            headers["X-Trace-ID"] = trace_id

        deadline = asyncio.get_running_loop().time() + deadline_seconds

        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await self.http.get(
                    f"/api/v1/creator/tasks/{task_id}", headers=headers
                )
            except (httpx.ConnectError, httpx.TimeoutException):
                await asyncio.sleep(self._poll_interval)
                continue

            if resp.status_code != 200:
                await asyncio.sleep(self._poll_interval)
                continue

            snapshot = resp.json()
            status = snapshot.get("status", "")

            if status == "COMPLETED":
                return ToolResult.success(snapshot, trace_id=trace_id)

            if status in ("FAILED", "CANCELLED"):
                return ToolResult.business_rejected(
                    f"Creator task ended with {status}",
                    user_message=(
                        "Content creation did not complete successfully. "
                        "No draft was saved to the community."
                    ),
                )

            await asyncio.sleep(self._poll_interval)

        return ToolResult.timeout(
            "Creator Agent did not complete in time. No draft was created."
        )

    # ── Get artifact ───────────────────────────────────────────

    async def get_artifact(
        self,
        task_id: str,
        artifact_id: str,
        *,
        bearer_token: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult[dict[str, Any]]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if trace_id:
            headers["X-Trace-ID"] = trace_id

        try:
            resp = await self.http.get(
                f"/api/v1/creator/tasks/{task_id}/artifacts/{artifact_id}",
                headers=headers,
            )
        except httpx.ConnectError:
            return ToolResult.dependency_unavailable(
                "Creator Agent is unreachable", trace_id=trace_id
            )
        except httpx.TimeoutException:
            return ToolResult.timeout("Creator request timed out")

        if resp.status_code == 200:
            return ToolResult.success(resp.json(), trace_id=trace_id)

        return ToolResult.not_found(f"Creator artifact {artifact_id} not found")

    # ── Create handoff ─────────────────────────────────────────

    async def create_handoff(
        self,
        task_id: str,
        *,
        source_artifact_id: str | None = None,
        bearer_token: str | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> ToolResult[dict[str, Any]]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if trace_id:
            headers["X-Trace-ID"] = trace_id

        body: dict[str, Any] = {}
        if source_artifact_id:
            body["source_artifact_id"] = source_artifact_id

        try:
            resp = await self.http.post(
                f"/api/v1/creator/tasks/{task_id}/publication-handoffs",
                json=body,
                headers=headers,
            )
        except httpx.ConnectError:
            return ToolResult.dependency_unavailable(
                "Creator Agent is unreachable", trace_id=trace_id
            )
        except httpx.TimeoutException:
            return ToolResult.timeout("Creator handoff timed out")

        if 200 <= resp.status_code < 300:
            return ToolResult.success(resp.json(), trace_id=trace_id)

        return ToolResult.dependency_unavailable(
            f"Creator handoff returned {resp.status_code}", trace_id=trace_id
        )


def extract_creator_document(artifact: dict[str, Any]) -> dict[str, str | None]:
    """Extract title, description, body_markdown from a Creator artifact."""
    content = artifact.get("content")
    if not isinstance(content, dict):
        return {"title": None, "description": None, "body_markdown": None}

    document = content.get("document") or content
    if not isinstance(document, dict):
        return {"title": None, "description": None, "body_markdown": None}

    title = document.get("title")
    description = document.get("description") or document.get("summary")
    body = (
        document.get("body_markdown")
        or document.get("content_markdown")
        or document.get("body")
    )

    return {
        "title": str(title) if title else None,
        "description": str(description) if description else None,
        "body_markdown": str(body) if body else None,
    }
