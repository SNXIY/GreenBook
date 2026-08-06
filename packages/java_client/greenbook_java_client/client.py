"""Type-safe async Java Agent Facade client.

All community business writes go through the Java Agent Facade.
Single source of truth: contracts/java-openapi.yaml
SHA256: 1409b6d825a11dc161b501668ac09e07349a38b0690f060396ac77c60668eeef
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import httpx
from greenbook_contracts.tool_result import ResourceRef, ToolResult
from greenbook_java_client.models import (
    AgentCommentPageResponse,
    AgentCommentReplyRequest,
    AgentCommentResponse,
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    AgentErrorResponse,
    AgentPostContext,
    AgentOwnPostSummary,
    DraftResponse,
    PostAnalyticsResponse,
    PublishNowRequest,
    PublishResponse,
    ScheduleCreateRequest,
    ScheduledPublicationResponse,
    ScheduleUpdateRequest,
    SearchPageResponse,
    UserAnalyticsSummaryResponse,
)

logger = logging.getLogger(__name__)
_SENSITIVE_RE = re.compile(
    r"(Authorization|Bearer|access_token|refresh_token|api_key|secret)"
    r"[\s:=]+[^\s,;)]+",
    re.IGNORECASE,
)


def _sanitize(value: str) -> str:
    return _SENSITIVE_RE.sub(r"\1=[REDACTED]", value)


# ── Public API ───────────────────────────────────────────────────────


class JavaClient:
    """Async httpx client for the Java Agent Facade.

    - Connection pooling via shared AsyncClient
    - configurable connect/read/write/pool timeouts
    - Bearer token relay from AuthContext
    - Idempotency-Key for all writes
    - X-Trace-Id, X-Conversation-Id, X-Agent-Run-Id, X-Tool-Call-Id, traceparent
    - Structured AgentErrorResponse parsing
    - request_sent / retryable judgment
    - Log sanitisation (no tokens in log messages)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        write_timeout: float = 30.0,
        pool_timeout: float = 5.0,
        max_connections: int = 20,
        max_keepalive: int = 10,
        verify: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
        )
        self.http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            verify=verify,
            limits=limits,
        )

    async def close(self) -> None:
        await self.http.aclose()

    # ── Header builders ──────────────────────────────────────────

    def _headers(
        self,
        *,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        idempotency_key: str | None = None,
        traceparent: str | None = None,
    ) -> dict[str, str]:
        h: dict[str, str] = {}
        if bearer_token:
            h["Authorization"] = f"Bearer {bearer_token}"
        if trace_id:
            h["X-Trace-ID"] = trace_id
        if conversation_id:
            h["X-Conversation-Id"] = conversation_id
        if agent_run_id:
            h["X-Agent-Run-Id"] = agent_run_id
        if tool_call_id:
            h["X-Tool-Call-Id"] = tool_call_id
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        if traceparent:
            h["traceparent"] = traceparent
        return h

    def _trace_id(self, headers: dict[str, str]) -> str | None:
        return headers.get("X-Trace-ID")

    # ── Low-level request ────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult[dict[str, Any]]:
        req_headers = dict(headers or {})
        trace_id = self._trace_id(req_headers)

        is_write = method in ("POST", "PUT", "DELETE", "PATCH")

        try:
            resp = await self.http.request(
                method, path, json=body, params=params, headers=req_headers
            )
        except httpx.ConnectError:
            logger.warning("Java connect failed path=%s", path)
            return ToolResult.dependency_unavailable(
                "Java backend unreachable — connection failed. No request was sent.",
                trace_id=trace_id,
            )
        except httpx.ConnectTimeout:
            logger.warning("Java connect timeout path=%s", path)
            return ToolResult.dependency_unavailable(
                "Java backend connection timed out. No request was sent.",
                trace_id=trace_id,
            )
        except httpx.ReadTimeout:
            logger.warning("Java read timeout path=%s method=%s", path, method)
            if is_write:
                return ToolResult.result_unknown(
                    "Write request was sent but Java response timed out. "
                    "Use the same Idempotency-Key to query or replay.",
                    trace_id=trace_id,
                )
            return ToolResult.timeout(
                "Java backend read timed out. You may safely retry."
            )
        except httpx.WriteTimeout:
            logger.warning("Java write timeout path=%s method=%s", path, method)
            return ToolResult.request_not_sent(
                "Request body could not be fully sent. You may safely retry."
            )
        except httpx.PoolTimeout:
            logger.warning("Java pool timeout path=%s", path)
            return ToolResult.dependency_unavailable(
                "Java backend connection pool exhausted. You may safely retry.",
                trace_id=trace_id,
            )
        except httpx.TimeoutException:
            logger.warning("Java timeout path=%s method=%s", path, method)
            if is_write:
                return ToolResult.result_unknown(
                    "Write request timed out — result is unknown. "
                    "Use the same Idempotency-Key to query or replay.",
                    trace_id=trace_id,
                )
            return ToolResult.timeout(
                "Java backend request timed out. You may safely retry."
            )
        except (httpx.RemoteProtocolError, httpx.NetworkError):
            return ToolResult.dependency_unavailable(
                "Java backend network error. No request was processed.",
                trace_id=trace_id,
            )

        resp_trace_id = resp.headers.get("X-Trace-ID") or trace_id
        receipt_id = resp.headers.get("X-Receipt-ID")

        if 200 <= resp.status_code < 300:
            data = resp.json() if resp.content else {}
            return ToolResult.success(data, trace_id=resp_trace_id, receipt_id=receipt_id)

        if resp.status_code == 204:
            return ToolResult.success({}, trace_id=resp_trace_id)

        if resp.status_code == 201:
            data = resp.json() if resp.content else {}
            return ToolResult.success(data, trace_id=resp_trace_id, receipt_id=receipt_id)

        return await self._map_error(resp, resp_trace_id, receipt_id)

    async def _map_error(
        self,
        resp: httpx.Response,
        trace_id: str | None,
        receipt_id: str | None,
    ) -> ToolResult[dict[str, Any]]:
        body_text = ""
        try:
            body_text = (resp.text or "")[:2000]
        except Exception:
            pass

        structured: AgentErrorResponse | None = None
        try:
            payload = resp.json()
            structured = AgentErrorResponse.model_validate(payload.get("error", payload))
        except Exception:
            pass

        code = str(structured.code) if structured else ""
        user_msg = structured.user_message if structured else None
        retryable = structured.retryable if structured else False
        request_committed = structured.request_committed if structured else False

        if resp.status_code == 400 or code == "VALIDATION_ERROR":
            return ToolResult.validation_error(message=body_text, user_message=user_msg or "")

        if resp.status_code in (401, 403) or code in ("AUTHENTICATION_REQUIRED", "UNAUTHORIZED", "FORBIDDEN"):
            return ToolResult.permission_denied(message=body_text)

        if resp.status_code == 404 or code == "NOT_FOUND":
            return ToolResult.not_found(message=body_text)

        if code == "DRAFT_VERSION_CONFLICT":
            return ToolResult.draft_version_conflict(message=body_text)

        if code == "IDEMPOTENCY_CONFLICT":
            return ToolResult.idempotency_conflict(message=body_text)

        if code == "BUSINESS_REJECTED":
            return ToolResult.business_rejected(message=body_text, user_message=user_msg or "")

        if code == "RESULT_UNKNOWN":
            return ToolResult.result_unknown(message=body_text, trace_id=trace_id)

        if resp.status_code == 409 or code == "CONFLICT":
            return ToolResult.conflict(message=body_text, user_message=user_msg or "")

        if resp.status_code >= 500 or code == "DEPENDENCY_UNAVAILABLE":
            return ToolResult.dependency_unavailable(f"Java backend error: {body_text}", trace_id=trace_id)

        return ToolResult.internal_error(f"Unexpected {resp.status_code}: {body_text}", trace_id=trace_id)

    def _log_call(
        self,
        method: str,
        path: str,
        *,
        status: str,
        latency_ms: int | None = None,
        trace_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        logger.info(
            "java_client method=%s path=%s status=%s latency_ms=%s trace_id=%s error_code=%s",
            method, path, status, latency_ms, trace_id, error_code,
        )

    # ── Community ─────────────────────────────────────────────────

    async def search_posts(
        self,
        *,
        query: str | None = None,
        sort: str = "latest",
        page: int = 1,
        size: int = 20,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[SearchPageResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", "/api/v1/agent/posts/search",
            headers=headers,
            params={"query": query or "", "sort": sort, "page": page, "size": size},
        )
        if result.ok and result.data is not None:
            result.data = SearchPageResponse.model_validate(result.data)
        return result

    async def get_post(
        self,
        post_id: str,
        *,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[AgentPostContext]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request("GET", f"/api/v1/agent/posts/{post_id}", headers=headers)
        if result.ok and result.data is not None:
            result.data = AgentPostContext.model_validate(result.data)
        return result

    async def list_own_posts(
        self,
        *,
        page: int = 1,
        size: int = 20,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[list[AgentOwnPostSummary]]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", "/api/v1/agent/me/posts",
            headers=headers,
            params={"page": page, "size": size},
        )
        if result.ok and isinstance(result.data, list):
            result.data = [AgentOwnPostSummary.model_validate(item) for item in result.data]
        return result

    # ── Draft ────────────────────────────────────────────────────

    async def create_draft(
        self,
        request: AgentDraftCreateRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[DraftResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "POST", "/api/v1/agent/drafts",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = DraftResponse.model_validate(result.data)
        return result

    async def get_draft(
        self,
        draft_id: str,
        *,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[DraftResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request("GET", f"/api/v1/agent/drafts/{draft_id}", headers=headers)
        if result.ok and result.data is not None:
            result.data = DraftResponse.model_validate(result.data)
        return result

    async def list_own_drafts(
        self,
        *,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[list[DraftResponse]]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request("GET", "/api/v1/agent/me/drafts", headers=headers)
        if result.ok and isinstance(result.data, list):
            result.data = [DraftResponse.model_validate(item) for item in result.data]
        return result

    async def update_draft(
        self,
        draft_id: str,
        request: AgentDraftUpdateRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[DraftResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "PUT",
            f"/api/v1/agent/drafts/{draft_id}/update",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = DraftResponse.model_validate(result.data)
        return result

    # ── Publication ──────────────────────────────────────────────

    async def create_schedule(
        self,
        request: ScheduleCreateRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[ScheduledPublicationResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "POST", "/api/v1/agent/publications/schedules",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = ScheduledPublicationResponse.model_validate(result.data)
        return result

    async def get_schedule(
        self,
        schedule_id: str,
        *,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[ScheduledPublicationResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", f"/api/v1/agent/publications/schedules/{schedule_id}", headers=headers
        )
        if result.ok and result.data is not None:
            result.data = ScheduledPublicationResponse.model_validate(result.data)
        return result

    async def update_schedule(
        self,
        schedule_id: str,
        request: ScheduleUpdateRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[ScheduledPublicationResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "PUT", f"/api/v1/agent/publications/schedules/{schedule_id}",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = ScheduledPublicationResponse.model_validate(result.data)
        return result

    async def cancel_schedule(
        self,
        schedule_id: str,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[dict[str, Any]]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        return await self._request(
            "DELETE", f"/api/v1/agent/publications/schedules/{schedule_id}", headers=headers
        )

    async def publish_now(
        self,
        request: PublishNowRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[PublishResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "POST", "/api/v1/agent/publications/publish-now",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = PublishResponse.model_validate(result.data)
        return result

    # ── Interaction ──────────────────────────────────────────────

    async def list_comments(
        self,
        post_id: str,
        *,
        cursor: str | None = None,
        size: int = 20,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[AgentCommentPageResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        params: dict[str, Any] = {"size": size}
        if cursor:
            params["cursor"] = cursor
        result = await self._request(
            "GET", f"/api/v1/agent/posts/{post_id}/comments",
            headers=headers, params=params,
        )
        if result.ok and result.data is not None:
            result.data = AgentCommentPageResponse.model_validate(result.data)
        return result

    async def reply_to_comment(
        self,
        reply: AgentCommentReplyRequest,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[AgentCommentResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        result = await self._request(
            "POST", f"/api/v1/agent/comments/{reply.parent_comment_id}/replies",
            headers=headers,
            body=reply.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = AgentCommentResponse.model_validate(result.data)
        return result

    # ── Analytics ────────────────────────────────────────────────

    async def get_post_analytics(
        self,
        post_id: str,
        *,
        bearer_token: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[PostAnalyticsResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", f"/api/v1/agent/posts/{post_id}/analytics", headers=headers
        )
        if result.ok and result.data is not None:
            result.data = PostAnalyticsResponse.model_validate(result.data)
        return result

    async def get_account_summary(
        self,
        *,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[UserAnalyticsSummaryResponse]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", "/api/v1/agent/me/analytics/summary", headers=headers
        )
        if result.ok and result.data is not None:
            result.data = UserAnalyticsSummaryResponse.model_validate(result.data)
        return result
