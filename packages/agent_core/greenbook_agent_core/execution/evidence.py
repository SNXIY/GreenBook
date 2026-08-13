"""Lossless execution evidence for one tool invocation.

The evidence envelope carries observed facts across Runtime boundaries.  It
does not classify failures, choose recovery actions, or mutate execution
state.  In particular, ``request_sent`` remains a tri-state value: ``None``
means that delivery could not be established.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from greenbook_contracts import SideEffectState
from pydantic import BaseModel, ConfigDict, Field

_MISSING = object()


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _pick(
    *mappings: Mapping[str, Any],
    keys: tuple[str, ...],
    default: Any = _MISSING,
) -> Any:
    for mapping in mappings:
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _string_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_hash(
    *,
    tool_name: str,
    capability: str,
    tool_args: Mapping[str, Any],
) -> str:
    material = {
        "tool_name": tool_name,
        "capability": capability,
        "tool_args": dict(tool_args),
    }
    canonical = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resource_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        mapping = _as_mapping(item)
        if mapping:
            result.append(mapping)
    return result


class ExecutionEvidence(BaseModel):
    """Observed evidence for one Runtime tool invocation.

    ``invocation_id`` identifies one attempt, while ``operation_id`` is the
    optional logical operation identity that can remain stable across
    attempts.  ``runtime_idempotency_key`` and
    ``external_idempotency_key`` intentionally remain separate fields.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    # identity
    execution_id: str | None = None
    step_id: str | None = None
    invocation_id: str | None = None
    tool_call_id: str | None = None
    operation_id: str | None = None

    # request evidence
    request_hash: str | None = None
    request_time: str | None = None
    request_sent: bool | None = None

    # side-effect evidence
    side_effect_state: SideEffectState = SideEffectState.UNKNOWN

    # external evidence
    receipt_id: str | None = None
    external_operation_id: str | None = None
    resource_refs: list[dict[str, Any]] = Field(default_factory=list)

    # idempotency evidence
    runtime_idempotency_key: str | None = None
    external_idempotency_key: str | None = None

    # error/response evidence
    error_code: str | None = None
    raw_error_type: str | None = None
    status_code: int | None = None
    phase: str | None = None
    trace_id: str | None = None

    @classmethod
    def from_context(
        cls,
        context: Any,
        *,
        request_time: str | None = None,
    ) -> ExecutionEvidence:
        """Create the pre-handler evidence from a ToolInvocationContext."""

        trace_context = getattr(context, "trace_context", None)
        tool_name = str(getattr(context, "tool_name", "") or "")
        capability = str(getattr(context, "capability", "") or "")
        tool_args = getattr(context, "tool_args", {}) or {}
        if not isinstance(tool_args, Mapping):
            tool_args = {}
        return cls(
            execution_id=_string_or_none(
                getattr(context, "execution_id", None)
                or getattr(trace_context, "execution_id", None)
            ),
            step_id=_string_or_none(
                getattr(context, "step_id", None)
                or getattr(trace_context, "step_id", None)
            ),
            invocation_id=_string_or_none(
                getattr(context, "invocation_id", None)
                or getattr(trace_context, "invocation_id", None)
            ),
            request_hash=_request_hash(
                tool_name=tool_name,
                capability=capability,
                tool_args=tool_args,
            ),
            request_time=request_time or datetime.now(UTC).isoformat(),
            request_sent=None,
            side_effect_state=SideEffectState.UNKNOWN,
            runtime_idempotency_key=_string_or_none(
                getattr(context, "idempotency_key", None)
            ),
            operation_id=_string_or_none(getattr(trace_context, "operation_id", None)),
            trace_id=_string_or_none(getattr(trace_context, "trace_id", None)),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None = None,
        *,
        base: ExecutionEvidence | None = None,
        request_sent: bool | None | object = _MISSING,
        side_effect_state: SideEffectState | str | object = _MISSING,
        error_code: str | None = None,
        raw_error_type: str | None = None,
        phase: str | None = None,
        tool_call_id: str | None = None,
        operation_id: str | None = None,
        external_operation_id: str | None = None,
    ) -> ExecutionEvidence:
        """Merge raw tool evidence without collapsing unknown delivery.

        The nested ``evidence`` mapping has precedence over legacy top-level
        fields.  Legacy ``state`` remains readable so existing MCP results can
        be upgraded at the Runtime boundary without changing their handlers.
        """

        raw = _as_mapping(payload)
        nested = _as_mapping(raw.get("evidence"))
        state = _as_mapping(nested.get("state")) or _as_mapping(raw.get("state"))
        base_data = base.model_dump(mode="python") if base is not None else {}

        raw_sent = _pick(
            nested,
            raw,
            keys=("request_sent",),
            default=_MISSING,
        )
        if raw_sent is _MISSING:
            raw_sent = request_sent
        if raw_sent is _MISSING:
            raw_sent = base_data.get("request_sent")
        resolved_sent = _bool_or_none(raw_sent)

        raw_side_effect = _pick(
            nested,
            raw,
            state,
            keys=("side_effect_state",),
            default=_MISSING,
        )
        if raw_side_effect is _MISSING:
            raw_side_effect = side_effect_state
        if raw_side_effect is _MISSING:
            started = _pick(
                nested,
                raw,
                state,
                keys=("side_effect_started",),
                default=_MISSING,
            )
            if started is True:
                raw_side_effect = SideEffectState.POSSIBLE
            elif started is False:
                raw_side_effect = SideEffectState.NOT_STARTED
            elif resolved_sent is True:
                raw_side_effect = SideEffectState.POSSIBLE
            elif resolved_sent is False:
                raw_side_effect = SideEffectState.NOT_STARTED
            else:
                raw_side_effect = base_data.get(
                    "side_effect_state", SideEffectState.UNKNOWN
                )
        try:
            resolved_side_effect = SideEffectState(str(raw_side_effect).upper())
        except ValueError:
            resolved_side_effect = SideEffectState.UNKNOWN

        raw_refs = _pick(
            nested,
            raw,
            keys=("resource_refs",),
            default=base_data.get("resource_refs", []),
        )
        raw_status = _pick(
            nested,
            raw,
            state,
            keys=("status_code", "response_status"),
            default=base_data.get("status_code"),
        )
        raw_external_key = _pick(
            nested,
            raw,
            state,
            keys=("external_idempotency_key", "idempotency_key"),
            default=base_data.get("external_idempotency_key"),
        )

        raw_error = _pick(
            nested,
            raw,
            keys=("error_code", "code"),
            default=error_code if error_code is not None else base_data.get("error_code"),
        )
        raw_type = _pick(
            nested,
            raw,
            state,
            keys=("raw_error_type", "exception_type"),
            default=raw_error_type
            if raw_error_type is not None
            else base_data.get("raw_error_type"),
        )
        raw_phase = _pick(
            nested,
            raw,
            state,
            keys=("phase",),
            default=phase if phase is not None else base_data.get("phase"),
        )
        raw_trace = _pick(
            nested,
            raw,
            state,
            keys=("trace_id",),
            default=base_data.get("trace_id"),
        )

        return cls(
            execution_id=_string_or_none(
                _pick(nested, raw, keys=("execution_id",),
                      default=base_data.get("execution_id"))
            ),
            step_id=_string_or_none(
                _pick(nested, raw, keys=("step_id",),
                      default=base_data.get("step_id"))
            ),
            invocation_id=_string_or_none(
                _pick(nested, raw, keys=("invocation_id",),
                      default=base_data.get("invocation_id"))
            ),
            tool_call_id=_string_or_none(
                _pick(
                    nested,
                    raw,
                    keys=("tool_call_id",),
                    default=tool_call_id
                    if tool_call_id is not None
                    else base_data.get("tool_call_id"),
                )
            ),
            operation_id=_string_or_none(
                _pick(
                    nested,
                    raw,
                    state,
                    keys=("operation_id",),
                    default=operation_id
                    if operation_id is not None
                    else base_data.get("operation_id"),
                )
            ),
            request_hash=_string_or_none(
                _pick(nested, raw, keys=("request_hash",),
                      default=base_data.get("request_hash"))
            ),
            request_time=_string_or_none(
                _pick(nested, raw, keys=("request_time",),
                      default=base_data.get("request_time"))
            ),
            request_sent=resolved_sent,
            side_effect_state=resolved_side_effect,
            receipt_id=_string_or_none(
                _pick(nested, raw, state, keys=("receipt_id", "receipt"),
                      default=base_data.get("receipt_id"))
            ),
            external_operation_id=_string_or_none(
                _pick(
                    nested,
                    raw,
                    state,
                    keys=("external_operation_id",),
                    default=external_operation_id
                    if external_operation_id is not None
                    else base_data.get("external_operation_id"),
                )
            ),
            resource_refs=_resource_refs(raw_refs),
            runtime_idempotency_key=_string_or_none(
                _pick(
                    nested,
                    raw,
                    keys=("runtime_idempotency_key",),
                    default=base_data.get("runtime_idempotency_key"),
                )
            ),
            external_idempotency_key=_string_or_none(raw_external_key),
            error_code=_string_or_none(raw_error),
            raw_error_type=_string_or_none(raw_type),
            status_code=_int_or_none(raw_status),
            phase=_string_or_none(raw_phase),
            trace_id=_string_or_none(raw_trace),
        )


__all__ = ["ExecutionEvidence"]
