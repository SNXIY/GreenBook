"""Type-safe async Java Agent Facade client.

All community business writes go through the Java Agent Facade.
Single source of truth: contracts/java-openapi.yaml
SHA256: 1409b6d825a11dc161b501668ac09e07349a38b0690f060396ac77c60668eeef
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
from contextlib import suppress
from typing import Any

import httpx
from greenbook_contracts.tool_result import ToolResult

from greenbook_java_client.models import (
    AgentCommentPageResponse,
    AgentCommentReplyRequest,
    AgentCommentResponse,
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    AgentErrorResponse,
    AgentOwnPostSummary,
    AgentPostContext,
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
_active_agent_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "greenbook_java_agent_run_id", default=""
)


@contextlib.contextmanager
def agent_run_scope(run_id: str | None):
    token = _active_agent_run_id.set(str(run_id or ""))
    try:
        yield
    finally:
        _active_agent_run_id.reset(token)
_SENSITIVE_RE = re.compile(
    r"(Authorization|Bearer|access_token|refresh_token|api_key|secret)"
    r"[\s:=]+[^\s,;)]+",
    re.IGNORECASE,
)


def _sanitize(value: str) -> str:
    return _SENSITIVE_RE.sub(r"\1=[REDACTED]", value)


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


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


# 鈹€鈹€ Public API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


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

    @classmethod
    def from_env(cls, *, base_url: str | None = None) -> JavaClient:
        """Build the Java client from the canonical Java service config."""

        return cls(
            base_url=base_url or _env_first(
                "GREENBOOK_JAVA_BASE_URL",
                default="http://127.0.0.1:8080",
            ),
            connect_timeout=_positive_float(
                "GREENBOOK_JAVA_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            read_timeout=_positive_float(
                "GREENBOOK_JAVA_READ_TIMEOUT_SECONDS", 30.0
            ),
            write_timeout=_positive_float(
                "GREENBOOK_JAVA_WRITE_TIMEOUT_SECONDS", 30.0
            ),
            pool_timeout=_positive_float(
                "GREENBOOK_JAVA_POOL_TIMEOUT_SECONDS", 5.0
            ),
            max_connections=_positive_int("GREENBOOK_JAVA_MAX_CONNECTIONS", 20),
            max_keepalive=_positive_int("GREENBOOK_JAVA_MAX_KEEPALIVE", 10),
            verify=_boolean_env("GREENBOOK_JAVA_VERIFY_TLS", True),
        )

    async def close(self) -> None:
        await self.http.aclose()

    # 鈹€鈹€ Header builders 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
        agent_run_id = agent_run_id or _active_agent_run_id.get()
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

    # 鈹€鈹€ Low-level request 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def _map_structured_error(
        self,
        resp: httpx.Response,
        trace_id: str | None,
        receipt_id: str | None,
    ) -> ToolResult[Any]:
        """Map the structured downstream contract before status fallbacks."""
        body_text = ""
        with suppress(Exception):
            body_text = (resp.text or "")[:2000]
        structured: AgentErrorResponse | None = None
        try:
            payload = resp.json()
            structured = AgentErrorResponse.model_validate(payload.get("error", payload))
        except Exception:
            pass

        code = str(structured.code) if structured else ""
        user_msg = structured.user_message if structured else None
        state = {
            "java_status": resp.status_code,
            "java_error_code": code or None,
            "field": structured.field if structured else None,
            "max_length": structured.max_length if structured else None,
            "actual_length": structured.actual_length if structured else None,
            "execution_id": structured.execution_id if structured else None,
            "receipt_id": receipt_id,
        }
        if 400 <= resp.status_code < 500 and code not in {
            "RESULT_UNKNOWN",
            "UNKNOWN_SIDE_EFFECT_OUTCOME",
        }:
            # A structured 4xx is a known Java rejection.  The request
            # crossed the HTTP boundary, but the requested mutation did not
            # start; this must not become RESULT_UNKNOWN merely because
            # request_sent is true.
            state.update({
                "side_effect_started": False,
                "side_effect_state": "NOT_STARTED",
                "result_known": True,
            })

        if code in {"FIELD_TOO_LONG", "INVALID_DRAFT_METADATA"}:
            return ToolResult.permanent_input(
                code, body_text,
                user_message=user_msg or "The draft metadata does not meet the publishing requirements.",
                state=state,
            )
        if code in {"VALIDATION_ERROR", "BAD_REQUEST"} or resp.status_code in {400, 422}:
            return ToolResult.permanent_input(
                code or "VALIDATION_ERROR", body_text,
                user_message=user_msg or "The downstream service rejected the request parameters.",
                state=state,
            )
        if resp.status_code == 401 or code in {"AUTHENTICATION_REQUIRED", "UNAUTHORIZED"}:
            return ToolResult.failure(
                "AUTHENTICATION_FAILED", body_text or "Java rejected the access token",
                user_msg or "Authentication is required.", request_sent=True,
                trace_id=trace_id, state=state,
            )
        if resp.status_code == 403 or code in {"FORBIDDEN", "PERMISSION_DENIED"}:
            return ToolResult.failure(
                "AUTHORIZATION_DENIED", body_text or "Java denied this operation",
                user_msg or "You do not have permission to perform this action.",
                request_sent=True, trace_id=trace_id, state=state,
            )
        if resp.status_code == 404 or code == "NOT_FOUND":
            result = ToolResult.not_found(message=body_text)
            result.state = state
            return result
        if code == "DRAFT_VERSION_CONFLICT":
            result = ToolResult.draft_version_conflict(message=body_text)
            result.state = state
            return result
        if code == "IDEMPOTENCY_CONFLICT":
            result = ToolResult.idempotency_conflict(message=body_text)
            result.state = state
            return result
        if code == "BUSINESS_REJECTED":
            result = ToolResult.business_rejected(message=body_text, user_message=user_msg or "")
            result.state = state
            return result
        if code in {"RESULT_UNKNOWN", "UNKNOWN_SIDE_EFFECT_OUTCOME"}:
            result = ToolResult.result_unknown(message=body_text, trace_id=trace_id)
            result.state = {
                **state,
                "side_effect_started": True,
                "side_effect_state": "POSSIBLE",
                "result_known": False,
            }
            return result
        if resp.status_code == 409 or code == "CONFLICT":
            result = ToolResult.conflict(message=body_text, user_message=user_msg or "")
            result.state = state
            return result
        if code in {"DEPENDENCY_UNAVAILABLE", "BACKEND_TEMPORARY_UNAVAILABLE"} \
                or resp.status_code in {502, 503, 504}:
            result = ToolResult.java_backend_unavailable(
                f"Java backend temporarily unavailable: {body_text}", trace_id=trace_id
            )
            result.state = state
            return result
        if code in {"INTERNAL_ERROR", "SERVER_FAILURE"} or resp.status_code >= 500:
            result = ToolResult.server_failure(body_text, trace_id=trace_id)
            result.state = state
            return result

        result = ToolResult.internal_error(f"Unexpected {resp.status_code}: {body_text}", trace_id=trace_id)
        result.state = state
        return result

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ToolResult[Any]:
        req_headers = dict(headers or {})
        trace_id = self._trace_id(req_headers)

        is_write = method in ("POST", "PUT", "DELETE", "PATCH")

        try:
            resp = await self.http.request(
                method, path, json=body, params=params, headers=req_headers
            )
        except httpx.ConnectError:
            logger.warning("Java connect failed path=%s", path)
            return ToolResult.java_backend_unavailable(
                "Java backend unreachable 鈥?connection failed. No request was sent.",
                trace_id=trace_id,
            )
        except httpx.ConnectTimeout:
            logger.warning("Java connect timeout path=%s", path)
            return ToolResult.java_backend_unavailable(
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
                    state={
                        "idempotency_key": req_headers.get("Idempotency-Key"),
                        "side_effect_started": True,
                        "side_effect_state": "POSSIBLE",
                        "result_known": False,
                    },
                )
            result = ToolResult.timeout(
                "Java backend read timed out. You may safely retry."
            )
            result.state = {
                "side_effect_started": False,
                "side_effect_state": "NONE",
                "result_known": False,
            }
            return result
        except httpx.WriteTimeout:
            logger.warning("Java write timeout path=%s method=%s", path, method)
            result = ToolResult.request_not_sent(
                "Request body could not be fully sent. You may safely retry."
            )
            result.trace_id = trace_id
            return result
        except httpx.PoolTimeout:
            logger.warning("Java pool timeout path=%s", path)
            return ToolResult.java_backend_unavailable(
                "Java backend connection pool exhausted. You may safely retry.",
                trace_id=trace_id,
            )
        except httpx.TimeoutException:
            logger.warning("Java timeout path=%s method=%s", path, method)
            if is_write:
                return ToolResult.result_unknown(
                    "Write request timed out 鈥?result is unknown. "
                    "Use the same Idempotency-Key to query or replay.",
                    trace_id=trace_id,
                    state={
                        "idempotency_key": req_headers.get("Idempotency-Key"),
                        "side_effect_started": True,
                        "side_effect_state": "POSSIBLE",
                        "result_known": False,
                    },
                )
            result = ToolResult.timeout(
                "Java backend request timed out. You may safely retry."
            )
            result.state = {
                "side_effect_started": False,
                "side_effect_state": "NONE",
                "result_known": False,
            }
            return result
        except (httpx.RemoteProtocolError, httpx.NetworkError):
            if is_write:
                return ToolResult.result_unknown(
                    "Java connection was lost during a write; commit state is unknown. "
                    "Use the same Idempotency-Key for reconciliation or replay.",
                    trace_id=trace_id,
                    state={
                        "idempotency_key": req_headers.get("Idempotency-Key"),
                        "side_effect_started": True,
                        "side_effect_state": "POSSIBLE",
                        "result_known": False,
                    },
                )
            return ToolResult.java_backend_unavailable(
                "Java backend network error. No request was processed.",
                trace_id=trace_id,
            )

        resp_trace_id = resp.headers.get("X-Trace-ID") or trace_id
        receipt_id = resp.headers.get("X-Receipt-ID")

        if resp.status_code == 204:
            return ToolResult.success(
                {},
                trace_id=resp_trace_id,
                receipt_id=receipt_id,
            )

        if 200 <= resp.status_code < 300:
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                return ToolResult.failure(
                    "BAD_GATEWAY",
                    "Java returned a non-JSON success response",
                    "Downstream returned an unreadable success response; please retry later.",
                    request_sent=is_write,
                    trace_id=resp_trace_id,
                )
            return ToolResult.success(data, trace_id=resp_trace_id, receipt_id=receipt_id)

        if resp.status_code == 201:
            data = resp.json() if resp.content else {}
            return ToolResult.success(data, trace_id=resp_trace_id, receipt_id=receipt_id)

        result = await self._map_error(resp, resp_trace_id, receipt_id)
        # An HTTP response proves that the request crossed the downstream
        # boundary, even when the response is an auth/business/server error.
        # Preserve this fact for the evidence envelope and carry the receipt
        # when the facade supplied one.
        result.request_sent = True
        if receipt_id and not result.receipt_id:
            result.receipt_id = receipt_id
        return result

    async def _map_error(
        self,
        resp: httpx.Response,
        trace_id: str | None,
        receipt_id: str | None,
    ) -> ToolResult[Any]:
        return await self._map_structured_error(resp, trace_id, receipt_id)

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

    # 鈹€鈹€ Community 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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

    async def delete_post(
        self,
        post_id: str,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[Any]:
        """Soft-delete one owned published post through the canonical API."""
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        return await self._request(
            "DELETE", f"/api/v1/agent/posts/{post_id}", headers=headers,
        )

    # 鈹€鈹€ Draft 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
            f"/api/v1/agent/drafts/{draft_id}",
            headers=headers,
            body=request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        if result.ok and result.data is not None:
            result.data = DraftResponse.model_validate(result.data)
        return result

    async def delete_draft(
        self,
        draft_id: str,
        *,
        bearer_token: str,
        idempotency_key: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult[Any]:
        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
        )
        return await self._request(
            "DELETE",
            f"/api/v1/agent/drafts/{draft_id}",
            headers=headers,
        )

    # 鈹€鈹€ Publication 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
    ) -> ToolResult[Any]:
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

    # 鈹€鈹€ Interaction 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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

    async def get_comment(
        self,
        comment_id: str,
        *,
        bearer_token: str,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolResult[AgentCommentResponse]:
        """Read one comment for authoritative reply verification."""

        headers = self._headers(
            bearer_token=bearer_token,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
        result = await self._request(
            "GET", f"/api/v1/agent/comments/{comment_id}", headers=headers,
        )
        if result.ok and result.data is not None:
            result.data = AgentCommentResponse.model_validate(result.data)
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

    # 鈹€鈹€ Analytics 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
