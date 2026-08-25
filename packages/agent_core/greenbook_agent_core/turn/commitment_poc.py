"""Small, isolated Commitment/WorkItem projection for the B control-plane POC.

This module deliberately does not introduce a second interpreter, planner,
runtime, or persistence table.  It projects the already validated
``Command``/``ResolvedSemanticState`` facts into the minimal business
commitment shape described by the architecture closeout.  The existing
Objective, ActionLoop, Durable Runtime, ResourceBinding and Java state remain
the production owners until this projection earns a migration decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DesiredOutcome(StrEnum):
    SEARCH_RESULT = "SEARCH_RESULT"
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    SCHEDULED = "SCHEDULED"
    REVISED = "REVISED"
    SCHEDULE_UPDATED = "SCHEDULE_UPDATED"
    SCHEDULE_CANCELLED = "SCHEDULE_CANCELLED"
    DELETED = "DELETED"


class WorkItemStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class CommitmentStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    FROZEN = "FROZEN"
    SUPERSEDED = "SUPERSEDED"


class HITLType(StrEnum):
    CLARIFICATION = "CLARIFICATION"
    SEMANTIC_CONFIRMATION = "SEMANTIC_CONFIRMATION"
    RISK_APPROVAL = "RISK_APPROVAL"
    ASYNC_PENDING = "ASYNC_PENDING"
    RESULT_UNKNOWN_RECONCILIATION = "RESULT_UNKNOWN_RECONCILIATION"


class CommitmentValidationError(ValueError):
    """Raised when a draft cannot safely become a frozen commitment."""


class WorkItem(BaseModel):
    """Minimal final-business-outcome record.

    ``execution_requirements`` is intentionally restricted to facts that can
    change execution admission or completion.  Content style belongs to the
    content Tool arguments, never to this runtime state.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    work_item_id: str = Field(default_factory=lambda: str(uuid4()))
    commitment_version: int = Field(default=1, ge=1)
    supersedes: str | None = None
    parent_work_item_id: str | None = None
    source_span: str = ""
    subject: str = ""
    desired_outcome: DesiredOutcome
    target_reference: dict[str, Any] = Field(default_factory=dict)
    resolved_target_ref: dict[str, Any] = Field(default_factory=dict)
    temporal_expression: str = ""
    canonical_run_at: str | None = None
    execution_requirements: dict[str, bool] = Field(default_factory=dict)
    resource_refs: list[str] = Field(default_factory=list)
    status: WorkItemStatus = WorkItemStatus.PENDING

    @model_validator(mode="after")
    def _validate_runtime_fields(self) -> WorkItem:
        unsupported = set(self.execution_requirements) - {"evidence_required"}
        if unsupported:
            raise ValueError(
                "execution_requirements only supports evidence_required; "
                f"got {sorted(unsupported)}"
            )
        return self


class CommitmentBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    commitment_id: str = Field(default_factory=lambda: str(uuid4()))
    commitment_version: int = Field(default=1, ge=1)
    supersedes_version: int | None = Field(default=None, ge=1)
    source_message_id: str = ""
    work_items: list[WorkItem] = Field(default_factory=list)


class CommitmentDraft(CommitmentBase):
    status: Literal[CommitmentStatus.DRAFT] = CommitmentStatus.DRAFT


class FrozenCommitment(CommitmentBase):
    """Immutable semantic commitment consumed by a B controller.

    Persistence is intentionally not added here.  The POC adapter can be
    stored in the existing Task/Objective envelope after migration evidence is
    sufficient; until then this type is test/experiment-only.
    """

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, frozen=True
    )
    status: Literal[CommitmentStatus.FROZEN] = CommitmentStatus.FROZEN


class SupersededCommitment(CommitmentBase):
    status: Literal[CommitmentStatus.SUPERSEDED] = CommitmentStatus.SUPERSEDED


class SupersedeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    superseded: SupersededCommitment
    replacement: CommitmentDraft


_SCHEDULED_OUTCOMES = {
    DesiredOutcome.SCHEDULED,
    DesiredOutcome.SCHEDULE_UPDATED,
}
_TARGETED_OUTCOMES = {
    DesiredOutcome.REVISED,
    DesiredOutcome.SCHEDULE_UPDATED,
    DesiredOutcome.SCHEDULE_CANCELLED,
    DesiredOutcome.DELETED,
}
_MUTATION_OUTCOMES = {
    DesiredOutcome.DRAFT,
    DesiredOutcome.PUBLISHED,
    DesiredOutcome.SCHEDULED,
    DesiredOutcome.REVISED,
    DesiredOutcome.SCHEDULE_UPDATED,
    DesiredOutcome.SCHEDULE_CANCELLED,
    DesiredOutcome.DELETED,
}


def project_command(command: Any, *, target_resolution: Any | None = None) -> CommitmentDraft:
    """Project existing resolved Command facts into a minimal B draft.

    The function accepts only structured command facts.  It never reads raw
    user text and never invokes an LLM, TargetResolver, or TemporalResolver;
    those remain the owners that produce the inputs.
    """

    semantic = getattr(command, "resolved_semantics", None)
    source_items = list(getattr(semantic, "items", None) or getattr(command, "items", None) or ())
    if not source_items:
        source_items = [None]

    resolved_target = _resolved_target(command, target_resolution)
    items: list[WorkItem] = []
    for index, source in enumerate(source_items):
        item_constraints = dict(getattr(source, "constraints", None) or {}) if source is not None else {}
        capabilities = list(
            getattr(source, "capabilities", None)
            or getattr(semantic, "capabilities", None)
            or getattr(command, "required_capabilities", None)
            or ()
        )
        operation = str(
            getattr(source, "operation", None)
            or getattr(semantic, "semantic_operation", None)
            or getattr(command, "semantic_operation", None)
            or getattr(command, "type", "CREATE")
        )
        publication = str(
            getattr(source, "publication_intent", None)
            or getattr(semantic, "publication_intent", None)
            or item_constraints.get("publication_intent")
            or item_constraints.get("publication_mode")
            or ""
        )
        outcome = _desired_outcome(operation, publication, capabilities)
        subject = str(
            getattr(source, "title", None)
            or getattr(source, "topic", None)
            or getattr(command, "goal", None)
            or getattr(command, "objective", None)
            or ""
        )
        temporal_expression = str(
            getattr(source, "temporal_text", None)
            or item_constraints.get("temporal_text")
            or ""
        )
        canonical_run_at = (
            getattr(source, "run_at", None)
            or item_constraints.get("run_at")
            or (
                getattr(semantic, "run_at", None)
                if len(source_items) == 1
                else None
            )
        )
        target_reference = dict(
            getattr(source, "target_reference", None)
            or getattr(command, "resolved_target", None)
            or {}
        )
        item_target = dict(resolved_target or {})
        if not item_target and target_reference.get("resource_id"):
            item_target = dict(target_reference)
        requirements = _execution_requirements(
            getattr(source, "requirements", None) or ()
        )
        items.append(
            WorkItem(
                commitment_version=1,
                source_span=f"item:{index}",
                subject=subject,
                desired_outcome=outcome,
                target_reference=target_reference,
                resolved_target_ref=item_target,
                temporal_expression=temporal_expression,
                canonical_run_at=str(canonical_run_at) if canonical_run_at else None,
                execution_requirements=requirements,
            )
        )

    return CommitmentDraft(
        source_message_id=str(
            getattr(command, "command_id", None)
            or getattr(command, "message_id", None)
            or ""
        ),
        work_items=items,
    )


def semantic_confirmation_required(commitment: CommitmentBase, *, risk: str = "") -> bool:
    """Return the deterministic confirmation trigger for a draft/frozen view."""

    items = commitment.work_items
    if len(items) >= 2:
        return True
    outcomes = {item.desired_outcome for item in items}
    if len(outcomes) > 1 and outcomes & _MUTATION_OUTCOMES:
        return True
    if any(item.desired_outcome in _TARGETED_OUTCOMES for item in items) and len(items) > 1:
        return True
    if any(item.desired_outcome == DesiredOutcome.SEARCH_RESULT for item in items) and any(
        item.desired_outcome in _MUTATION_OUTCOMES
        for item in items
    ):
        return True
    return str(risk or "").upper() in {"HIGH", "IMPORTANT"} and any(
        item.desired_outcome == DesiredOutcome.PUBLISHED for item in items
    )


def clarification_required(commitment: CommitmentBase) -> bool:
    """Check only blocking target/time facts, not user-facing wording."""

    return any(
        (
            item.desired_outcome in _SCHEDULED_OUTCOMES
            and not item.canonical_run_at
        )
        or (
            item.desired_outcome in _TARGETED_OUTCOMES
            and not item.resolved_target_ref
        )
        for item in commitment.work_items
    )


def risk_approval_required(commitment: CommitmentBase) -> bool:
    return any(
        item.desired_outcome in {DesiredOutcome.DELETED, DesiredOutcome.PUBLISHED}
        for item in commitment.work_items
    )


def hitl_type(commitment: CommitmentBase, *, risk: str = "") -> HITLType | None:
    if clarification_required(commitment):
        return HITLType.CLARIFICATION
    if semantic_confirmation_required(commitment, risk=risk):
        return HITLType.SEMANTIC_CONFIRMATION
    if risk_approval_required(commitment):
        return HITLType.RISK_APPROVAL
    return None


def validate_draft(commitment: CommitmentDraft) -> None:
    errors: list[str] = []
    if not commitment.work_items:
        errors.append("commitment has no work items")
    if clarification_required(commitment):
        errors.extend(
            f"{item.work_item_id}: unresolved target/time"
            for item in commitment.work_items
            if (
                item.desired_outcome in _SCHEDULED_OUTCOMES
                and not item.canonical_run_at
            )
            or (
                item.desired_outcome in _TARGETED_OUTCOMES
                and not item.resolved_target_ref
            )
        )
    if errors:
        raise CommitmentValidationError("; ".join(errors))


def freeze(commitment: CommitmentDraft) -> FrozenCommitment:
    """Validate and freeze a draft; frozen facts cannot be silently changed."""

    validate_draft(commitment)
    payload = commitment.model_dump(mode="python")
    payload["status"] = CommitmentStatus.FROZEN
    return FrozenCommitment.model_validate(payload)


def supersede(
    frozen: FrozenCommitment,
    work_items: Sequence[WorkItem],
) -> SupersedeResult:
    """Create v(n+1) and mark v(n) superseded without in-place mutation."""

    old_payload = frozen.model_dump(mode="python")
    old_payload["status"] = CommitmentStatus.SUPERSEDED
    superseded_record = SupersededCommitment.model_validate(old_payload)
    next_version = frozen.commitment_version + 1
    replacement_items = [
        item.model_copy(update={"commitment_version": next_version})
        for item in work_items
    ]
    replacement = CommitmentDraft(
        commitment_id=frozen.commitment_id,
        commitment_version=next_version,
        supersedes_version=frozen.commitment_version,
        source_message_id=frozen.source_message_id,
        work_items=replacement_items,
    )
    return SupersedeResult(superseded=superseded_record, replacement=replacement)


def revalidate_draft(
    draft: CommitmentDraft,
    *,
    resolve_target: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
    resolve_time: Callable[[str], str | None] | None = None,
) -> CommitmentDraft:
    """Re-run backend target/time validation for a frontend-edited draft."""

    payload = draft.model_dump(mode="python")
    updated_items: list[dict[str, Any]] = []
    for item in draft.work_items:
        value = item.model_dump(mode="python")
        if resolve_target is not None and item.target_reference:
            resolved = resolve_target(item.target_reference)
            value["resolved_target_ref"] = dict(resolved or {})
        if resolve_time is not None and item.temporal_expression:
            value["canonical_run_at"] = resolve_time(item.temporal_expression)
        updated_items.append(value)
    payload["work_items"] = updated_items
    result = CommitmentDraft.model_validate(payload)
    validate_draft(result)
    return result


def objective_from_work_item(work_item: WorkItem, task_id: str) -> Any:
    """Adapt one POC WorkItem to the existing Objective persistence shape.

    This is deliberately an adapter, not a second stored model.  The
    execution capabilities below are derived only because the current
    Objective/ActionLoop contract still requires them; the Commitment itself
    stores only the desired business outcome.
    """

    from ..task.models import Objective

    capability_map = {
        DesiredOutcome.SEARCH_RESULT: ["SEARCH_COMMUNITY"],
        DesiredOutcome.DRAFT: ["GENERATE_CONTENT"],
        DesiredOutcome.PUBLISHED: ["GENERATE_CONTENT", "PUBLISH_NOW"],
        DesiredOutcome.SCHEDULED: ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        DesiredOutcome.REVISED: ["UPDATE_DRAFT"],
        DesiredOutcome.SCHEDULE_UPDATED: ["UPDATE_SCHEDULE"],
        DesiredOutcome.SCHEDULE_CANCELLED: ["CANCEL_SCHEDULE"],
        DesiredOutcome.DELETED: ["DELETE_POST"],
    }
    resource_kind = {
        DesiredOutcome.SEARCH_RESULT: "SEARCH_RESULT",
        DesiredOutcome.DRAFT: "DRAFT",
        DesiredOutcome.PUBLISHED: "POST",
        DesiredOutcome.SCHEDULED: "SCHEDULE",
        DesiredOutcome.REVISED: "DRAFT",
        DesiredOutcome.SCHEDULE_UPDATED: "SCHEDULE",
        DesiredOutcome.SCHEDULE_CANCELLED: "SCHEDULE",
        DesiredOutcome.DELETED: "POST",
    }[work_item.desired_outcome]
    constraints: dict[str, Any] = {}
    if work_item.canonical_run_at:
        constraints["run_at"] = work_item.canonical_run_at
    if work_item.resolved_target_ref:
        constraints["target_state"] = "RESOLVED"
    related = list(work_item.resource_refs)
    target_id = str(
        work_item.resolved_target_ref.get("resource_id")
        or work_item.resolved_target_ref.get("id")
        or ""
    )
    if target_id and target_id not in related:
        related.append(target_id)
    return Objective(
        objective_id=work_item.work_item_id,
        task_id=task_id,
        description=work_item.subject,
        intent=work_item.desired_outcome.value,
        expected_resource_kind=resource_kind,
        required_capabilities=capability_map[work_item.desired_outcome],
        constraints=constraints,
        related_resource_ids=related,
    )


def render_confirmation(commitment: CommitmentBase) -> str:
    """Deterministically render the structured commitment for the frontend."""

    lines = ["我理解你的安排如下："]
    for index, item in enumerate(commitment.work_items, start=1):
        subject = item.subject or "未命名目标"
        outcome = _OUTCOME_LABEL[item.desired_outcome]
        if item.desired_outcome in _SCHEDULED_OUTCOMES:
            outcome = f"{outcome}（{item.canonical_run_at or '时间待确认'}）"
        lines.append(f"{index}.《{subject}》{outcome}")
    return "\n".join(lines)


def _desired_outcome(operation: str, publication: str, capabilities: Sequence[str]) -> DesiredOutcome:
    values = {str(value).upper() for value in capabilities}
    operation = str(operation or "").upper()
    publication = str(publication or "").upper()
    if "DELETE_POST" in values or operation in {"DELETE", "DELETE_POST"}:
        return DesiredOutcome.DELETED
    if "CANCEL_SCHEDULE" in values or operation in {"CANCEL", "CANCEL_SCHEDULE"}:
        return DesiredOutcome.SCHEDULE_CANCELLED
    if "UPDATE_SCHEDULE" in values or operation == "UPDATE_SCHEDULE":
        return DesiredOutcome.SCHEDULE_UPDATED
    if "UPDATE_DRAFT" in values or "MANAGE_DRAFT" in values or operation in {"MODIFY", "REVISE", "UPDATE_DRAFT"}:
        return DesiredOutcome.REVISED
    if publication in {"SCHEDULED", "SCHEDULE", "SCHEDULED_PUBLISH", "FUTURE", "FUTURE_PUBLISH"} or "SCHEDULE_PUBLISH" in values:
        return DesiredOutcome.SCHEDULED
    if publication in {"IMMEDIATE", "PUBLISH_NOW", "NOW", "IMMEDIATE_PUBLISH"} or "PUBLISH_NOW" in values:
        return DesiredOutcome.PUBLISHED
    if "SEARCH_COMMUNITY" in values or "SEARCH_POSTS" in values or operation in {"SEARCH", "QUERY"}:
        return DesiredOutcome.SEARCH_RESULT
    return DesiredOutcome.DRAFT


def _resolved_target(command: Any, resolution: Any | None) -> dict[str, Any]:
    if resolution is not None and getattr(resolution, "target", None) is not None:
        target = resolution.target
        return {
            key: value
            for key, value in {
                "id": getattr(target, "id", None),
                "resource_id": getattr(target, "resource_id", None),
                "kind": getattr(getattr(target, "kind", None), "value", getattr(target, "kind", None)),
                "task_id": getattr(target, "task_id", None),
            }.items()
            if value not in (None, "")
        }
    return dict(getattr(command, "resolved_target", None) or {})


def _execution_requirements(requirements: Sequence[Any]) -> dict[str, bool]:
    return {
        "evidence_required": True
    } if any(str(value).lower() == "evidence_required" for value in requirements) else {}


_OUTCOME_LABEL = {
    DesiredOutcome.SEARCH_RESULT: "返回搜索结果",
    DesiredOutcome.DRAFT: "保存为草稿",
    DesiredOutcome.PUBLISHED: "创作后立即发布",
    DesiredOutcome.SCHEDULED: "创作后定时发布",
    DesiredOutcome.REVISED: "完成修改",
    DesiredOutcome.SCHEDULE_UPDATED: "更新发布时间",
    DesiredOutcome.SCHEDULE_CANCELLED: "取消定时发布",
    DesiredOutcome.DELETED: "删除",
}


__all__ = [
    "CommitmentBase",
    "CommitmentDraft",
    "CommitmentStatus",
    "CommitmentValidationError",
    "DesiredOutcome",
    "FrozenCommitment",
    "HITLType",
    "SupersedeResult",
    "SupersededCommitment",
    "WorkItem",
    "WorkItemStatus",
    "clarification_required",
    "freeze",
    "hitl_type",
    "project_command",
    "render_confirmation",
    "revalidate_draft",
    "risk_approval_required",
    "semantic_confirmation_required",
    "supersede",
    "validate_draft",
    "objective_from_work_item",
]
