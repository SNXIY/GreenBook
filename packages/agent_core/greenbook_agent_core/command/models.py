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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    # A user-triggered retry is grounded against one persisted historical
    # Objective.  This is intentionally narrower than a generic status or
    # recency reference; TargetResolver applies the FAILED/UNKNOWN/terminal
    # safety rules.
    FAILED = "FAILED"


class TaskDeltaOperation(StrEnum):
    """Desired-state mutation a user sentence asks for (not an AgentAction)."""

    CREATE_TASK = "CREATE_TASK"
    ADD_GOAL = "ADD_GOAL"
    UPDATE_GOAL = "UPDATE_GOAL"
    CANCEL_GOAL = "CANCEL_GOAL"
    CANCEL_TASK = "CANCEL_TASK"
    CONTINUE_TASK = "CONTINUE_TASK"
    NO_CHANGE = "NO_CHANGE"
    ASK_USER = "ASK_USER"


class TaskDelta(BaseModel):
    """One desired-state mutation for an existing conversation task.

    ``operation`` is a state mutation, never a tool capability. Tool-level
    work (CANCEL_SCHEDULE, GENERATE_DRAFT) is decided later by AgentLoop from
    the desired/actual gap. ``target_reference`` grounds which Task/Goal the
    sentence changes; Python validates ownership and legality before apply.
    """

    model_config = ConfigDict(extra="forbid")

    operation: TaskDeltaOperation
    change_id: str = ""
    target_reference: dict[str, Any] = Field(default_factory=dict)
    desired_changes: dict[str, Any] = Field(default_factory=dict)
    dependency_reference: list[dict[str, Any]] = Field(default_factory=list)
    source_reference: dict[str, Any] = Field(default_factory=dict)
    needs_target_resolution: bool = False

    @field_validator("target_reference", mode="before")
    @classmethod
    def _normalize_target_reference(cls, value: Any) -> dict[str, Any]:
        """Treat null/blank reference values as missing evidence."""

        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        return {
            key: item
            for key, item in value.items()
            if not (isinstance(item, str) and not item.strip())
        }

    @model_validator(mode="after")
    def _require_goal_reference(self) -> TaskDelta:
        """Preserve explicit unresolved flags without blocking contextual grounding."""

        if self.operation in {
            TaskDeltaOperation.UPDATE_GOAL,
            TaskDeltaOperation.CANCEL_GOAL,
        }:
            reference = self.target_reference
            has_goal_reference = bool(
                str(reference.get("goal_id") or "").strip()
                or str(reference.get("label") or "").strip()
                or str(reference.get("description") or "").strip()
                or (
                    str(reference.get("id") or "").strip()
                    and str(reference.get("kind") or "").upper() == "GOAL"
                )
            )
            if not has_goal_reference:
                return self
        elif self.operation not in {
            TaskDeltaOperation.CREATE_TASK,
            TaskDeltaOperation.NO_CHANGE,
            TaskDeltaOperation.ASK_USER,
        }:
            reference = self.target_reference
            has_task_reference = bool(
                str(reference.get("task_id") or "").strip()
                or str(reference.get("id") or "").strip()
                or str(reference.get("label") or "").strip()
                or str(reference.get("description") or "").strip()
                or str(reference.get("reference_type") or "").upper() == "ACTIVE"
            )
            if not has_task_reference:
                return self
        return self


class CommandItem(BaseModel):
    """One independent final business deliverable.

    A CommandItem is a business target, not a capability.  It carries the
    natural-language temporal expression (``temporal_text``) so a later step can
    resolve it through TemporalResolver to a canonical absolute run_at.  The
    model/Interpreter only assigns WHICH business item a time belongs to; it
    never produces the authoritative UTC instant.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", description="Title of this one final business deliverable")
    topic: str = Field(default="", description="Topic owned by this deliverable")
    # Stable semantic label supplied by the structured interpreter (for
    # example A/B/C). It is not a runtime/objective id; it only lets a later
    # item name an explicit predecessor without inferring from raw text.
    item_key: str = Field(default="", description="Structured label for this deliverable, if supplied")
    requirements: list[str] = Field(
        default_factory=list,
        description="Requirements for this deliverable; do not split execution steps into items",
    )
    operation: str = "CREATE"
    capabilities: list[str] = Field(default_factory=list)
    temporal_text: str = Field(default="", description="This item's natural-language publication time")
    # References to other structured items, not tool steps. Resolution to
    # persisted Objective ids happens after all items are materialized.
    dependencies: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class ResolvedSemanticItem(BaseModel):
    """Canonical facts for one independent business deliverable."""

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    topic: str = ""
    item_key: str = ""
    requirements: list[str] = Field(default_factory=list)
    # The model may provide an operation/capability hint for compatibility;
    # the semantic compilation boundary derives the canonical values.
    operation: str = Field(default="CREATE", description="Open action-family evidence")
    capabilities: list[str] = Field(
        default_factory=list,
        description="Open capability evidence; canonical capabilities are derived later",
    )
    publication_intent: str = ""
    temporal_text: str = ""
    temporal_kind: str = "NONE"
    run_at: str | None = None
    temporal_resolved: bool = False
    dependencies: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    target_reference: dict[str, Any] = Field(default_factory=dict)


class ResolvedSemanticState(BaseModel):
    """Single resolved semantic fact set shared by both execution paths.

    This model contains facts only.  It deliberately has no plan, queue,
    worker, or execution fields.
    """

    model_config = ConfigDict(extra="forbid")

    source_command_id: str = ""
    operation: str = ""
    semantic_operation: str = ""
    capabilities: list[str] = Field(default_factory=list)
    publication_intent: str = ""
    target_type: str = ""
    target_reference: dict[str, Any] = Field(default_factory=dict)
    resolved_target: dict[str, Any] = Field(default_factory=dict)
    # Preserve the resolver's bounded candidate set for ambiguity/clarification
    # consumers.  This is evidence, not an alternate target selection path.
    target_candidates: list[dict[str, Any]] = Field(default_factory=list)
    # A bounded knowledge/query fact preserved from structured interpretation.
    # It is intentionally separate from Command.goal, which may be a
    # compatibility display string and must never be used as an implicit raw
    # user-text fallback by an execution adapter.
    question: str = Field(default="", max_length=1000)
    temporal_kind: str = "NONE"
    run_at: str | None = None
    temporal_resolved: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_reason: str = ""
    risk: str = ""
    requires_approval: bool = False
    items: list[ResolvedSemanticItem] = Field(default_factory=list)
    objectives: list[dict[str, Any]] = Field(default_factory=list)


class DeliverableSegment(BaseModel):
    """One independently createable business deliverable before item mapping."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(default="", description="Self-contained deliverable text")
    item_key: str = ""
    operation_hint: str = Field(
        default="CREATE",
        description="High-level operation hint: CREATE, MODIFY, QUERY, or CANCEL",
    )
    entity_type: str = ""
    topic: str = ""
    title: str = ""
    requirements: list[str] = Field(default_factory=list)
    temporal_text: str = ""
    dependencies: list[str] = Field(default_factory=list)
    # Per-deliverable semantic evidence.  This is intentionally the same
    # bounded constraint container used by CommandItem; segmentation may carry
    # publication intent without inventing a concrete timestamp.
    constraints: dict[str, Any] = Field(default_factory=dict)
    target_reference: dict[str, Any] = Field(default_factory=dict)


class DeliverableSegmentation(BaseModel):
    """Minimal WHAT-only segmentation result used by CommandInterpreter."""

    model_config = ConfigDict(extra="forbid")

    deliverables: list[DeliverableSegment] = Field(default_factory=list)


class InputSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span_id: int
    text: str


class SpanAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span_id: int
    group_id: str


class SpanGrouping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignments: list[SpanAssignment] = Field(default_factory=list)


class CommandTarget(BaseModel):
    """Structured target reference emitted by the model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: TargetKind = TargetKind.TASK
    id: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    resource_id: str | None = Field(default=None, min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    # Some structured-output providers use ``label`` for the same bounded
    # human-readable identity that TaskDelta.target_reference already accepts.
    # It is evidence only; resolver selection remains deterministic and
    # identity/status scoped.
    label: str | None = Field(default=None, min_length=1)
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
    # Phase 3.5 adaptive control plane: the first semantic action (a canonical
    # capability name) and the request complexity. SIMPLE requests skip
    # GoalDecomposer and the first AgentLoop reason; COMPLEX keeps the full
    # decomposition path. Both are bootstrap hints only and are validated
    # against the catalog and Command before any tool is selected.
    first_action: str = Field(
        default="",
        description="Compatibility hint only; the runtime derives the first action",
    )
    request_complexity: str = Field(
        default="SIMPLE",
        description="Segmentation bootstrap hint; final complexity is derived",
    )
    task_changes: list[TaskDelta] = Field(default_factory=list)
    target: CommandTarget | None = None
    # Query text for a knowledge/read outcome. This is semantic evidence, not
    # an execution plan or a copy of the unstructured request envelope.
    question: str = Field(default="", max_length=1000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    semantic_operation: str = Field(
        default="",
        description="Open action-family evidence; final operation is derived",
    )
    scope: str = ""
    risk: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity: str = ""
    needs_clarification: bool = Field(
        default=False,
        description="Clarification evidence only; final truth is deterministic",
    )
    required_capabilities: list[str] = Field(
        default_factory=list,
        description="Compatibility capability evidence; canonical capabilities are derived",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Thin per-business-target items (each with its own temporal_text).
    items: list[CommandItem] = Field(default_factory=list)


class Command(BaseModel):
    """Validated command object handed to adapters and execution planning."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: CommandType
    goal: str = ""
    objective: str = ""
    first_action: str = ""
    request_complexity: str = "SIMPLE"
    task_changes: list[TaskDelta] = Field(default_factory=list)
    target: CommandTarget | None = None
    # Bounded structured query fact used by canonical read capabilities.
    question: str = Field(default="", max_length=1000)
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
    # Thin per-business-target items (each with its own temporal_text).  When
    # present, Objectives are created one-per-item (not per capability) so a
    # single turn can schedule multiple targets at different times.
    items: list[CommandItem] = Field(default_factory=list)
    target_resolution: str | None = None
    resolved_target: dict[str, Any] | None = None
    target_candidates: list[dict[str, Any]] = Field(default_factory=list)
    resolved_semantics: ResolvedSemanticState | None = None

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
    # Semantic evidence is a separate contract from canonical resolver
    # metadata.  The provider projection strips identities recursively.
    reference_evidence: list[dict[str, Any]] = Field(default_factory=list)
    recent_verified_outcomes: list[dict[str, Any]] = Field(default_factory=list)
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
                reference_evidence=list(payload.get("reference_evidence") or []),
                recent_verified_outcomes=list(payload.get("recent_verified_outcomes") or []),
                metadata=payload,
            )

        session = getattr(value, "session", value)
        payload_builder = getattr(value, "decision_payload", None)
        payload = payload_builder() if callable(payload_builder) else {}
        if not isinstance(payload, dict):
            payload = {}
        # Prefer the assembled target_candidates (tasks projected with label,
        # run_at, created_at, task_id) so cross-turn references resolve against
        # the real owning Task; the raw resource/artifact views lack that shape.
        target_candidates = payload.get("target_candidates")
        if target_candidates:
            resources = list(target_candidates) if isinstance(target_candidates, list) else []
        else:
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
            reference_evidence=list(
                getattr(value, "reference_evidence", None)
                or payload.get("reference_evidence", [])
                or []
            ),
            recent_verified_outcomes=list(
                getattr(value, "recent_verified_outcomes", None)
                or payload.get("recent_verified_outcomes", [])
                or []
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
    "DeliverableSegment",
    "DeliverableSegmentation",
    "InputSpan",
    "SpanAssignment",
    "SpanGrouping",
    "StructuredCommandOutput",
    "TargetKind",
    "TargetReferenceType",
    "TaskDelta",
    "TaskDeltaOperation",
]
