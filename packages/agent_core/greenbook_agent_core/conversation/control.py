"""Explicit control commands for already-created Runtime resources.

Natural-language understanding belongs to ``command.Command``.  This module
contains only typed control payloads used by explicit API/control surfaces;
it is deliberately not an Intent or user-message interpreter.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from greenbook_agent_core.command.models import (
    Command,
    CommandTarget,
    TargetKind,
    TargetReferenceType,
)


class ExecutionControlType(StrEnum):
    PAUSE_EXECUTION = "PAUSE_EXECUTION"
    RESUME_EXECUTION = "RESUME_EXECUTION"
    RETRY_EXECUTION = "RETRY_EXECUTION"
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ExecutionControlOperation(StrEnum):
    PAUSE_EXECUTION = "PAUSE_EXECUTION"
    RESUME_EXECUTION = "RESUME_EXECUTION"
    RETRY_FAILED_STEP = "RETRY_FAILED_STEP"
    APPROVE_REQUEST = "APPROVE_REQUEST"
    REJECT_REQUEST = "REJECT_REQUEST"


class ExecutionControlTarget(BaseModel):
    """Typed target accepted by explicit control endpoints.

    ``type`` and the resource-specific ID aliases are accepted because these
    are wire-format fields, not a second semantic model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: TargetKind = Field(
        default=TargetKind.EXECUTION,
        validation_alias=AliasChoices("kind", "type"),
    )
    id: str | None = Field(default=None, validation_alias=AliasChoices("id", "target_id"))
    task_id: str | None = None
    resource_id: str | None = None
    artifact_id: str | None = None
    execution_id: str | None = None
    reference: str | None = None

    def to_command_target(self) -> CommandTarget:
        identifier = (
            self.id
            or self.execution_id
            or self.resource_id
            or self.artifact_id
            or (self.task_id if self.kind == TargetKind.TASK else None)
        )
        return CommandTarget(
            kind=self.kind,
            id=identifier,
            task_id=self.task_id,
            resource_id=self.resource_id,
            reference=self.reference,
            reference_type=(
                TargetReferenceType.IDENTIFIER
                if identifier and not self.reference
                else TargetReferenceType.NONE
            ),
        )


class ExecutionControlCommand(BaseModel):
    """Control an existing execution or approval request."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    command: ExecutionControlType
    operation: ExecutionControlOperation
    target: ExecutionControlTarget
    patch: dict[str, Any] = Field(default_factory=dict)
    source: str = "EXPLICIT_CONTROL_API"

    @property
    def is_approval(self) -> bool:
        return self.command in {
            ExecutionControlType.APPROVE,
            ExecutionControlType.REJECT,
        }

    @property
    def is_execution_control(self) -> bool:
        return self.command in {
            ExecutionControlType.PAUSE_EXECUTION,
            ExecutionControlType.RESUME_EXECUTION,
            ExecutionControlType.RETRY_EXECUTION,
        }

    def as_command(self) -> Command:
        return Command(
            command_id=self.command_id,
            type="CONTROL",
            objective=self.operation.value,
            target=self.target.to_command_target(),
            parameters=dict(self.patch),
            confidence=1.0,
            source=self.source,
        )


__all__ = [
    "ExecutionControlCommand",
    "ExecutionControlOperation",
    "ExecutionControlTarget",
    "ExecutionControlType",
]
