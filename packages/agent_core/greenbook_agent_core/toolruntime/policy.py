"""Deterministic policy gate between ToolSelector and ToolRuntime."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata
from pydantic import BaseModel, ConfigDict


class ToolExecutionMode(StrEnum):
    SYNC = "SYNC"
    QUEUE = "QUEUE"
    WAITING_HUMAN = "WAITING_HUMAN"
    DENY = "DENY"


class ToolPolicyDecision(BaseModel):
    """The code-owned result of evaluating one selected tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    mode: ToolExecutionMode
    tool_name: str
    reason: str = ""
    requires_approval: bool = False
    required_scopes: tuple[str, ...] = ()
    queue_required: bool = False


class ToolPolicyDeniedError(PermissionError):
    """Raised only when a caller tries to execute a denied selection."""

    def __init__(self, decision: ToolPolicyDecision) -> None:
        super().__init__(decision.reason or f"Tool '{decision.tool_name}' is denied.")
        self.decision = decision


ToolPolicyDenied = ToolPolicyDeniedError


class ToolPolicyGate:
    """Single policy source for permission, approval, and execution mode."""

    def __init__(
        self,
        *,
        permission_checker: Callable[..., bool] | None = None,
    ) -> None:
        self._permission_checker = permission_checker

    def evaluate(
        self,
        metadata: ToolMetadata,
        *,
        scopes: Iterable[str] = (),
        approval_granted: bool = False,
        requested_mode: str | ToolExecutionMode = "AUTO",
        multi_step: bool = False,
        long_running: bool = False,
        max_cost: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ToolPolicyDecision:
        policy = metadata.policy
        required = tuple(policy.permission.required_scopes)
        supplied = set(scopes)
        if self._permission_checker is not None:
            permitted = bool(
                self._permission_checker(
                    metadata,
                    scopes=supplied,
                    context=dict(context or {}),
                )
            )
        else:
            permitted = set(required) <= supplied
        if not permitted:
            return ToolPolicyDecision(
                allowed=False,
                mode=ToolExecutionMode.DENY,
                tool_name=metadata.name,
                reason="Tool permission policy denied the requested scopes.",
                required_scopes=required,
            )

        requires_approval = policy.requires_approval
        if requires_approval and not approval_granted:
            return ToolPolicyDecision(
                allowed=False,
                mode=ToolExecutionMode.WAITING_HUMAN,
                tool_name=metadata.name,
                reason="Human approval is required before this tool can run.",
                requires_approval=True,
                required_scopes=required,
            )
        if max_cost is not None and policy.cost > max_cost:
            return ToolPolicyDecision(
                allowed=False,
                mode=ToolExecutionMode.DENY,
                tool_name=metadata.name,
                reason="Tool cost exceeds the configured policy budget.",
                required_scopes=required,
            )

        requested = str(requested_mode).upper()
        if requested not in {"AUTO", "SYNC", "QUEUE"}:
            return ToolPolicyDecision(
                allowed=False,
                mode=ToolExecutionMode.DENY,
                tool_name=metadata.name,
                reason=f"Unsupported execution mode '{requested_mode}'.",
                required_scopes=required,
            )

        retry_policy = policy.retry_policy
        queue_required = bool(
            multi_step
            or long_running
            or requested == ToolExecutionMode.QUEUE.value
            or policy.side_effect.has_side_effect
            or policy.side_effect.destructive
            or retry_policy.max_attempts > 1
            or policy.timeout_seconds > 120.0
        )
        mode = ToolExecutionMode.QUEUE if queue_required else ToolExecutionMode.SYNC
        return ToolPolicyDecision(
            allowed=True,
            mode=mode,
            tool_name=metadata.name,
            reason="Tool metadata policy permits the requested operation.",
            requires_approval=requires_approval,
            required_scopes=required,
            queue_required=queue_required,
        )

    def enforce(self, metadata: ToolMetadata, **kwargs: Any) -> ToolPolicyDecision:
        decision = self.evaluate(metadata, **kwargs)
        if not decision.allowed and decision.mode == ToolExecutionMode.DENY:
            raise ToolPolicyDenied(decision)
        return decision


__all__ = [
    "ToolExecutionMode",
    "ToolPolicyDecision",
    "ToolPolicyDenied",
    "ToolPolicyDeniedError",
    "ToolPolicyGate",
]
