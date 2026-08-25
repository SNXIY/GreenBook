"""Real read-only Java reconciliation adapter.

Deterministically verifies a RESULT_UNKNOWN operation against Java
authoritative state, per SemanticAction, using the persisted expected
postcondition (never the LLM, never a replayed write).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .operation_tracking import OperationStatus


def _is_not_found(result: Any) -> bool:
    if result is None:
        return False
    code = str(getattr(result, "code", "") or "").upper()
    return "NOT_FOUND" in code or code == "404" or getattr(result, "ok", True) is False and (
        "404" in code or "NOT_FOUND" in code
    )


def _get(result: Any, *keys: str, default: Any = None) -> Any:
    data = getattr(result, "data", None)
    for key in keys:
        value = getattr(data, key, None)
        if value is not None:
            return value
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
    return default


def _normalize_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return str(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


class JavaReconciliationAdapter:
    """Verify an operation's postcondition against a real JavaClient.

    ``token_provider`` returns the bearer token used for read queries.  Reads
    never mutate Java state and never replay the original write.
    """

    name = "java-reconciliation"

    def __init__(
        self,
        java: Any,
        token_provider: Callable[[], Awaitable[str] | str],
        *,
        trace_id: str = "",
    ) -> None:
        self._java = java
        self._token_provider = token_provider
        self._trace_id = trace_id

    async def reconcile(self, operation: Any) -> OperationStatus:
        """Return the authoritative outcome for one RESULT_UNKNOWN operation."""
        action = str(operation.semantic_action or "").upper()
        resource_id = str(operation.resource_id or operation.external_operation_id or "")
        if not resource_id:
            evidence = getattr(operation, "evidence", None)
            for ref in getattr(evidence, "resource_refs", ()) or ():
                if isinstance(ref, dict):
                    kind = str(ref.get("kind") or ref.get("resource_type") or "").upper()
                    candidate = str(ref.get("resource_id") or "")
                else:
                    kind = str(getattr(ref, "kind", "") or getattr(ref, "resource_type", "") or "").upper()
                    candidate = str(getattr(ref, "resource_id", "") or "")
                if candidate and (not operation.resource_type or kind == str(operation.resource_type).upper()):
                    resource_id = candidate
                    break
        expected = dict((operation.expected_postcondition or {}).get("expected") or {})
        arguments = dict((operation.expected_postcondition or {}).get("arguments") or {})
        if not resource_id:
            return OperationStatus.UNKNOWN
        # API-managed workers recover the validated user credential from the
        # queued execution that owns this operation. Standalone workers keep
        # the existing no-argument service-token provider contract.
        try:
            token_value = self._token_provider(operation)
        except TypeError:
            token_value = self._token_provider()
        token = await token_value if inspect.isawaitable(token_value) else token_value
        try:
            if action == "UPDATE_DRAFT":
                return await self._verify_update_draft(resource_id, token, expected, arguments)
            if action in {"CREATE_DRAFT", "GENERATE_CONTENT"}:
                return await self._verify_create_draft(resource_id, token)
            if action == "DELETE_DRAFT":
                return await self._verify_delete_draft(resource_id, token)
            if action in {"UPDATE_SCHEDULE", "CREATE_SCHEDULE", "MANAGE_SCHEDULE"}:
                return await self._verify_update_schedule(resource_id, token, expected, arguments)
            if action == "CANCEL_SCHEDULE":
                return await self._verify_cancel_schedule(resource_id, token)
            if action == "PUBLISH_NOW":
                return await self._verify_publish_now(resource_id, token, expected, arguments)
        except Exception:  # noqa: BLE001 - a read outage is itself unknown
            return OperationStatus.UNKNOWN
        return OperationStatus.UNKNOWN

    async def _verify_create_draft(self, resource_id: str, token: str) -> OperationStatus:
        draft = await self._java.get_draft(resource_id, bearer_token=token, trace_id=self._trace_id)
        if draft.ok:
            return OperationStatus.SUCCEEDED
        if _is_not_found(draft):
            return OperationStatus.FAILED
        return OperationStatus.UNKNOWN

    async def _verify_update_draft(
        self,
        resource_id: str,
        token: str,
        expected: dict[str, Any],
        arguments: dict[str, Any],
    ) -> OperationStatus:
        draft = await self._java.get_draft(resource_id, bearer_token=token, trace_id=self._trace_id)
        if not draft.ok:
            return OperationStatus.UNKNOWN
        for field in ("title", "content", "summary"):
            want = expected.get(field)
            if want is None and arguments.get(field):
                want = arguments.get(field)
            if want is None:
                continue
            if _get(draft, field) != want:
                return OperationStatus.FAILED
        return OperationStatus.SUCCEEDED

    async def _verify_delete_draft(self, resource_id: str, token: str) -> OperationStatus:
        draft = await self._java.get_draft(resource_id, bearer_token=token, trace_id=self._trace_id)
        if _is_not_found(draft):
            return OperationStatus.SUCCEEDED  # authoritative 404 -> deleted
        if draft.ok:
            return OperationStatus.FAILED  # still present -> not deleted
        return OperationStatus.UNKNOWN

    async def _verify_update_schedule(
        self,
        resource_id: str,
        token: str,
        expected: dict[str, Any],
        arguments: dict[str, Any],
    ) -> OperationStatus:
        schedule = await self._java.get_schedule(resource_id, bearer_token=token, trace_id=self._trace_id)
        if not schedule.ok:
            return OperationStatus.UNKNOWN
        want = expected.get("run_at") or arguments.get("run_at")
        if want:
            actual = _normalize_time(_get(schedule, "run_at"))
            if actual and _normalize_time(want) not in (actual,):
                return OperationStatus.FAILED
            if not actual:
                return OperationStatus.UNKNOWN
        want_status = expected.get("status")
        if want_status and str(_get(schedule, "status") or "").upper() != str(want_status).upper():
            return OperationStatus.FAILED
        return OperationStatus.SUCCEEDED

    async def _verify_cancel_schedule(self, resource_id: str, token: str) -> OperationStatus:
        schedule = await self._java.get_schedule(resource_id, bearer_token=token, trace_id=self._trace_id)
        if not schedule.ok:
            return OperationStatus.UNKNOWN
        status = str(_get(schedule, "status") or "").upper()
        return OperationStatus.SUCCEEDED if status == "CANCELLED" else OperationStatus.FAILED

    async def _verify_publish_now(
        self,
        resource_id: str,
        token: str,
        expected: dict[str, Any],
        arguments: dict[str, Any],
    ) -> OperationStatus:
        post_id = expected.get("post_id") or arguments.get("post_id")
        if post_id:
            post = await self._java.get_post(post_id, bearer_token=token, trace_id=self._trace_id)
            return OperationStatus.SUCCEEDED if post.ok else (
                OperationStatus.FAILED if not _is_not_found(post) else OperationStatus.UNKNOWN
            )
        # Fall back to the schedule holding this draft: a published post id on
        # the schedule proves the publication happened.
        if resource_id:
            schedule = await self._java.get_schedule(resource_id, bearer_token=token, trace_id=self._trace_id)
            if schedule.ok and _get(schedule, "published_post_id"):
                return OperationStatus.SUCCEEDED
        return OperationStatus.UNKNOWN


__all__ = ["JavaReconciliationAdapter"]
