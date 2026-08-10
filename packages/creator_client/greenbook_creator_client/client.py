"""Async client for the Creator Agent Task API."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from greenbook_contracts.tool_result import ToolResult

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
        base_url: str = "http://127.0.0.1:8092",
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

    @classmethod
    def from_env(cls, *, base_url: str | None = None) -> CreatorClient:
        """Build the real Creator client from Assistant deployment config."""

        return cls(
            base_url=base_url or _env_first(
                "ASSISTANT_CREATOR_BASE_URL",
                "GREENBOOK_CREATOR_BASE_URL",
                default="http://127.0.0.1:8092",
            ),
            timeout=_positive_float("ASSISTANT_CREATOR_TIMEOUT_SECONDS", 240.0),
            poll_interval=_positive_float(
                "ASSISTANT_CREATOR_POLL_INTERVAL_SECONDS", 1.5
            ),
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
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return ToolResult.creator_unavailable(
                "Creator Agent is unreachable. No draft was created. You may safely retry.",
                trace_id=trace_id,
            )
        except httpx.WriteTimeout:
            result = ToolResult.request_not_sent(
                "Creator request body was not fully sent. You may safely retry."
            )
            result.trace_id = trace_id
            return result
        except httpx.ReadTimeout:
            return ToolResult.result_unknown(
                "Creator accepted a write request but its response timed out. "
                "Do not submit a duplicate task.",
                trace_id=trace_id,
            )
        except (httpx.RemoteProtocolError, httpx.NetworkError):
            return ToolResult.failure(
                "CREATOR_UNAVAILABLE",
                "Creator network state is unknown after the write boundary",
                "内容创作服务网络异常，无法确认任务是否已提交。",
                retryable=True,
                request_sent=None,
                trace_id=trace_id,
            )

        if 200 <= resp.status_code < 300:
            return self._json_success(resp, trace_id=trace_id)

        return self._map_http_error(resp, trace_id=trace_id, write=True)

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
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                return ToolResult.creator_unavailable(
                    "Creator Agent is unreachable while checking the task.",
                    trace_id=trace_id,
                )
            except (httpx.RemoteProtocolError, httpx.NetworkError):
                return ToolResult.creator_unavailable(
                    "Creator Agent network error while checking the task.",
                    trace_id=trace_id,
                )
            except httpx.TimeoutException:
                await asyncio.sleep(self._poll_interval)
                continue

            if resp.status_code != 200:
                if resp.status_code in {401, 403, 404} or resp.status_code >= 500:
                    return self._map_http_error(resp, trace_id=trace_id, write=False)
                await asyncio.sleep(self._poll_interval)
                continue

            try:
                snapshot = resp.json()
            except (TypeError, ValueError):
                return ToolResult.failure(
                    "CREATOR_INVALID_RESPONSE",
                    "Creator returned invalid task JSON",
                    "内容创作服务返回了无法识别的任务状态。",
                    request_sent=True,
                    trace_id=trace_id,
                )
            if not isinstance(snapshot, dict):
                return ToolResult.failure(
                    "CREATOR_INVALID_RESPONSE",
                    "Creator returned a non-object task response",
                    "内容创作服务返回了无法识别的任务状态。",
                    request_sent=True,
                    trace_id=trace_id,
                )
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
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return ToolResult.creator_unavailable(
                "Creator Agent is unreachable", trace_id=trace_id
            )
        except httpx.TimeoutException:
            result = ToolResult.timeout("Creator request timed out")
            result.trace_id = trace_id
            return result
        except (httpx.RemoteProtocolError, httpx.NetworkError):
            return ToolResult.creator_unavailable(
                "Creator Agent network error", trace_id=trace_id
            )

        if resp.status_code == 200:
            return self._json_success(resp, trace_id=trace_id)

        return self._map_http_error(resp, trace_id=trace_id, write=False)

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
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return ToolResult.creator_unavailable(
                "Creator Agent is unreachable", trace_id=trace_id
            )
        except httpx.WriteTimeout:
            result = ToolResult.request_not_sent("Creator handoff was not sent")
            result.trace_id = trace_id
            return result
        except httpx.ReadTimeout:
            return ToolResult.result_unknown(
                "Creator handoff response timed out; do not submit a duplicate.",
                trace_id=trace_id,
            )
        except httpx.TimeoutException:
            result = ToolResult.timeout("Creator handoff timed out")
            result.trace_id = trace_id
            return result
        except (httpx.RemoteProtocolError, httpx.NetworkError):
            return ToolResult.failure(
                "CREATOR_UNAVAILABLE",
                "Creator handoff network state is unknown",
                "内容创作服务网络异常，无法确认交接是否已提交。",
                retryable=True,
                request_sent=None,
                trace_id=trace_id,
            )

        if 200 <= resp.status_code < 300:
            return self._json_success(resp, trace_id=trace_id)

        return self._map_http_error(resp, trace_id=trace_id, write=True)

    @staticmethod
    def _json_success(
        response: httpx.Response,
        *,
        trace_id: str | None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return ToolResult.failure(
                "CREATOR_INVALID_RESPONSE",
                "Creator returned a non-JSON success response",
                "内容创作服务返回了无法识别的结果。",
                request_sent=True,
                trace_id=trace_id,
            )
        if not isinstance(payload, dict):
            return ToolResult.failure(
                "CREATOR_INVALID_RESPONSE",
                "Creator returned a non-object success response",
                "内容创作服务返回了无法识别的结果。",
                request_sent=True,
                trace_id=trace_id,
            )
        headers = getattr(response, "headers", {}) or {}
        return ToolResult.success(
            payload,
            trace_id=trace_id,
            receipt_id=headers.get("X-Receipt-ID"),
        )

    @staticmethod
    def _map_http_error(
        response: httpx.Response,
        *,
        trace_id: str | None,
        write: bool,
    ) -> ToolResult[Any]:
        body = (getattr(response, "text", "") or "")[:2000]
        code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                source = error if isinstance(error, dict) else payload
                code = str(source.get("code") or source.get("error_code") or "")
        except (TypeError, ValueError):
            pass
        normalized = code.strip().upper()
        if response.status_code == 401 or normalized in {
            "INVALID_AUDIENCE",
            "INVALID_TOKEN",
            "UNAUTHORIZED",
        }:
            return ToolResult.failure(
                "AUTHENTICATION_FAILED",
                "Creator rejected the access token or audience",
                "内容创作服务认证配置无效，请检查 issuer 和 audience。",
                request_sent=write,
                trace_id=trace_id,
            )
        if response.status_code == 403:
            return ToolResult.failure(
                "AUTHORIZATION_DENIED",
                "Creator denied the operation",
                "当前凭证没有执行内容创作操作的权限。",
                request_sent=write,
                trace_id=trace_id,
            )
        if response.status_code == 404:
            return ToolResult.not_found(body)
        if response.status_code >= 500:
            return ToolResult.failure(
                "CREATOR_UNAVAILABLE",
                f"Creator returned HTTP {response.status_code}",
                "内容创作服务暂时不可用，请稍后重试。",
                retryable=True,
                request_sent=write,
                trace_id=trace_id,
            )
        return ToolResult.failure(
            "CREATOR_REQUEST_REJECTED",
            body or f"Creator returned HTTP {response.status_code}",
            "内容创作服务拒绝了本次请求。",
            request_sent=write,
            trace_id=trace_id,
        )


def _env_first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


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
