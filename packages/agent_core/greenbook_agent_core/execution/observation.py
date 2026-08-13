"""Evidence derived from one concrete tool result.

The AgentLoop and the durable Worker must describe the same observation.  This
module is intentionally a small, side-effect-free projection helper; it does
not choose a fallback or mutate a plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .failure_decision import FailureClassifier


def observation_evidence(
    result: Mapping[str, Any],
    *,
    available_tools: Sequence[Any] = (),
    failed_tool: str = "",
) -> dict[str, Any]:
    """Project a tool result into planner-facing, evidence-bounded facts."""

    ok = result.get("ok", result.get("success"))
    data = result.get("data")
    resource_count = resource_count_for(data)
    if ok is False:
        result_status = "FAILED"
    elif ok is True:
        result_status = (
            "EMPTY"
            if resource_count == 0 and is_collection_result(data)
            else "SUCCESS"
        )
    else:
        result_status = ""

    failure_kind = ""
    if result_status == "FAILED":
        failure_kind = FailureClassifier.category_for_code(
            str(result.get("error_code") or result.get("code") or "UNKNOWN")
        ).value

    missing = str(result.get("missing_required_reference") or "")
    state = result.get("state")
    if not missing and isinstance(state, Mapping):
        missing = str(state.get("missing_required_reference") or "")

    return {
        "result_status": result_status,
        "resource_count": resource_count,
        "missing_required_reference": missing,
        "available_fallback_capabilities": available_read_fallbacks(
            available_tools,
            failed_tool=failed_tool or str(result.get("tool_name") or ""),
        ),
        "failure_kind": failure_kind,
    }


def is_collection_result(data: Any) -> bool:
    if isinstance(data, list):
        return True
    if not isinstance(data, Mapping):
        return False
    return any(key in data for key in ("items", "posts", "results", "resource_refs"))


def resource_count_for(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, Mapping):
        return 0
    for key in ("items", "posts", "results", "resource_refs"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def available_read_fallbacks(
    tools: Sequence[Any],
    *,
    failed_tool: str,
) -> list[str]:
    """List only read-only candidates; selection remains Planner-owned."""

    names: list[str] = []
    for metadata in tools:
        name = str(getattr(metadata, "name", "") or "")
        if not name or name == failed_tool:
            continue
        policy = getattr(metadata, "policy", None)
        if policy is None:
            continue
        if (
            bool(getattr(policy, "requires_approval", False))
            or bool(getattr(getattr(policy, "side_effect", None), "has_side_effect", False))
            or bool(getattr(getattr(policy, "side_effect", None), "destructive", False))
        ):
            continue
        names.append(name)
    return names


__all__ = [
    "available_read_fallbacks",
    "is_collection_result",
    "observation_evidence",
    "resource_count_for",
]
