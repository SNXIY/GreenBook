"""Canonical command contracts for the GreenBook Agent Runtime.

The command boundary is intentionally independent from tools, Tasks, and the
execution engine.  An LLM produces :class:`StructuredCommandOutput`; the
runtime then validates it and turns it into a durable :class:`Command`.
"""

from __future__ import annotations

import builtins
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CommandType(StrEnum):
    """Top-level user operation understood by the Agent Runtime."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    QUERY = "QUERY"
    CONTROL = "CONTROL"


class TargetKind(StrEnum):
    TASK = "TASK"
    DRAFT = "DRAFT"
    SCHEDULE = "SCHEDULE"
    POST = "POST"
    EXECUTION = "EXECUTION"
    APPROVAL = "APPROVAL"


class TargetReferenceType(StrEnum):
    """Semantic reference selected by the model, not inferred by Python."""

    NONE = "NONE"
    ACTIVE = "ACTIVE"
    IDENTIFIER = "IDENTIFIER"
    ORDINAL = "ORDINAL"
    PROPERTY = "PROPERTY"
    TEMPORAL = "TEMPORAL"


class CommandTarget(BaseModel):
    """Structured target reference emitted by the model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: TargetKind = TargetKind.TASK
    id: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    resource_id: str | None = Field(default=None, min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    reference_type: TargetReferenceType = TargetReferenceType.NONE
    ordinal: int | None = Field(default=None, ge=1)
    property: str | None = Field(default=None, min_length=1)
    value: str | None = Field(default=None, min_length=1)
    after: str | None = None
    before: str | None = None

    @builtins.property
    def explicit_id(self) -> str | None:
        return self.id or self.resource_id or (
            self.task_id if self.kind == TargetKind.TASK else None
        )


class StructuredCommandOutput(BaseModel):
    """Semantic understanding emitted by the Command Runtime model call.

    ``command`` is an execution-independent operation envelope, not a
    traditional intent classifier. ``objective`` and ``parameters`` remain
    accepted as compatibility fields; ``goal`` and the semantic fields are
    canonical for new prompts.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: CommandType
    goal: str = Field(default="", max_length=12000)
    objective: str = Field(default="", max_length=12000)
    target: CommandTarget | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    semantic_operation: str = ""
    scope: str = ""
    risk: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity: str = ""
    needs_clarification: bool = False
    required_capabilities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Command(BaseModel):
    """Validated command object handed to adapters and execution planning."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: CommandType
    goal: str = ""
    objective: str = ""
    target: CommandTarget | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    semantic_operation: str = ""
    scope: str = ""
    risk: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity: str = ""
    needs_clarification: bool = False
    required_capabilities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = "LLM_STRUCTURED_OUTPUT"
    raw_input: str = ""
    target_resolution: str | None = None
    resolved_target: dict[str, Any] | None = None

    @property
    def command(self) -> CommandType:
        """Compatibility spelling used by older Runtime callers."""

        return self.type

    @property
    def operation(self) -> CommandType:
        return self.type

    @property
    def requested_goal(self) -> str:
        """Return the semantic outcome without exposing command taxonomy."""

        return self.goal or self.objective

    @property
    def requires_target(self) -> bool:
        return self.type in {
            CommandType.MODIFY,
            CommandType.CANCEL,
            CommandType.CONTROL,
        }

    @property
    def target_exists(self) -> bool:
        return bool(
            self.target
            and (
                self.target.explicit_id
                or self.resolved_target
            )
        )

    @property
    def is_broad_destructive(self) -> bool:
        """Return whether the structured request has unbounded destructive scope."""

        values: list[str] = [self.semantic_operation, self.scope, self.risk]
        for container in (self.parameters, self.entities, self.constraints):
            for key in ("operation", "semantic_operation", "scope", "risk", "action"):
                value = container.get(key)
                if value is not None:
                    values.append(str(value))
        normalized = " ".join(values).upper().replace("-", "_")
        broad = any(
            marker in normalized
            for marker in (
                "BROAD_DESTRUCTIVE",
                "UNBOUNDED_DESTRUCTIVE",
                "UNBOUNDED_SCOPE",
                "ALL_OWNED",
                "ALL_POSTS",
                "ALL_ARTICLES",
            )
        )
        destructive = any(
            marker in normalized
            for marker in ("DELETE", "REMOVE", "PURGE", "DESTROY")
        )
        return broad and (
            destructive or self.type in {CommandType.MODIFY, CommandType.CANCEL}
        )


class CommandContext(BaseModel):
    """Bounded context supplied to the model and the resolver facade."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    conversation_id: str = ""
    timezone: str = "Asia/Shanghai"
    active_target: CommandTarget | None = None
    active_tasks: list[dict[str, Any]] = Field(default_factory=list)
    unfinished_goals: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_any(cls, value: Any | None) -> CommandContext:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            payload = dict(value)
            return cls(
                conversation_id=str(payload.get("conversation_id", "")),
                timezone=str(payload.get("timezone", "Asia/Shanghai")),
                active_target=payload.get("active_target"),
                active_tasks=list(payload.get("active_tasks") or []),
                unfinished_goals=list(payload.get("unfinished_goals") or []),
                targets=list(
                    payload.get("targets")
                    or payload.get("target_candidates")
                    or []
                ),
                history=list(
                    payload.get("history")
                    or payload.get("recent_messages")
                    or []
                ),
                summary=payload.get("summary") or payload.get("conversation_summary"),
                metadata=payload,
            )

        session = getattr(value, "session", value)
        payload_builder = getattr(value, "decision_payload", None)
        payload = payload_builder() if callable(payload_builder) else {}
        if not isinstance(payload, dict):
            payload = {}
        resources = list(
            getattr(value, "available_resources", None)
            or payload.get("available_resources", [])
            or []
        )
        resources.extend(
            {**item, "kind": "TASK"}
            for item in (
                getattr(value, "active_tasks", None)
                or payload.get("active_tasks", [])
                or []
            )
            if isinstance(item, dict)
        )
        resources.extend(
            {**item, "kind": "EXECUTION"}
            for item in (
                getattr(value, "executions", None)
                or payload.get("executions", [])
                or []
            )
            if isinstance(item, dict)
        )
        resources.extend(
            item for item in (
                getattr(value, "artifacts", None)
                or payload.get("artifacts", [])
                or []
            ) if isinstance(item, dict)
        )
        active_tasks = list(
            getattr(value, "active_tasks", None)
            or payload.get("active_tasks", [])
            or []
        )
        unfinished_goals = list(
            getattr(value, "unfinished_goals", None)
            or payload.get("unfinished_goals", [])
            or []
        )
        active_target = _active_target_from_session(session)
        return cls(
            conversation_id=str(
                getattr(session, "conversation_id", "")
                or payload.get("conversation_id", "")
            ),
            timezone=str(
                getattr(session, "timezone", "Asia/Shanghai")
                or payload.get("timezone", "Asia/Shanghai")
            ),
            active_target=active_target,
            active_tasks=active_tasks,
            unfinished_goals=unfinished_goals,
            targets=resources,
            history=list(
                getattr(value, "recent_messages", None)
                or payload.get("recent_messages", [])
                or payload.get("history", [])
                or []
            ),
            summary=(
                getattr(value, "summary", None)
                or payload.get("summary")
                or payload.get("conversation_summary")
            ),
            metadata=payload,
        )


def _active_target_from_session(session: Any) -> CommandTarget | None:
    """Project explicit session bindings without guessing from user text."""

    if session is None:
        return None
    bindings = (
        (TargetKind.DRAFT, getattr(session, "active_draft_id", None)),
        (TargetKind.SCHEDULE, getattr(session, "active_schedule_id", None)),
        (TargetKind.POST, getattr(session, "active_post_id", None)),
        (TargetKind.TASK, getattr(session, "active_task_id", None)),
        (TargetKind.EXECUTION, getattr(session, "active_execution_id", None)),
    )
    for kind, identifier in bindings:
        if identifier:
            return CommandTarget(kind=kind, id=str(identifier))
    return None


__all__ = [
    "Command",
    "CommandContext",
    "CommandTarget",
    "CommandType",
    "StructuredCommandOutput",
    "TargetKind",
    "TargetReferenceType",
]
