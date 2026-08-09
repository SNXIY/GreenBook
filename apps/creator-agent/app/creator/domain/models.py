from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.errors import CreatorInvalidTransitionError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CreatorTaskKind(str, Enum):
    CREATE_CONTENT = "CREATE_CONTENT"
    ANALYZE_CONTENT = "ANALYZE_CONTENT"
    BUILD_STRATEGY = "BUILD_STRATEGY"
    IMPROVE_DRAFT = "IMPROVE_DRAFT"
    RESEARCH_TOPIC = "RESEARCH_TOPIC"


class CreatorTaskStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CreatorRunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RuntimeOutcomeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    WAITING_HUMAN = "WAITING_HUMAN"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FAILED = "FAILED"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    DEAD = "DEAD"


class CreatorDecisionKind(str, Enum):
    TOPIC_SELECTION = "TOPIC_SELECTION"
    OUTLINE_APPROVAL = "OUTLINE_APPROVAL"
    DRAFT_REVIEW = "DRAFT_REVIEW"


class CreatorDecisionAction(str, Enum):
    SELECT = "SELECT"
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    EDIT = "EDIT"


class CreatorDecisionStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    APPLIED = "APPLIED"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatorGoal(FrozenModel):
    text: str = Field(min_length=1, max_length=20_000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_scope: dict[str, Any] = Field(default_factory=dict)


class CreateCreatorTaskCommand(FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    kind: CreatorTaskKind
    goal: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, max_length=128)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_scope: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    trace_id: str | None = Field(default=None, min_length=1, max_length=64)

    def creator_goal(self) -> CreatorGoal:
        return CreatorGoal(
            text=self.goal.strip(),
            constraints=self.constraints,
            source_scope=self.source_scope,
        )


class CancelCreatorTaskCommand(FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)


class RetryCreatorTaskCommand(FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class SubmitCreatorDecisionCommand(FrozenModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    decision_id: str = Field(min_length=1, max_length=128)
    action: CreatorDecisionAction
    actor_id: str = Field(min_length=1, max_length=128)
    selected_option_id: str | None = Field(default=None, max_length=128)
    feedback: str | None = Field(default=None, max_length=4_000)
    edited_payload: dict[str, Any] | None = None
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_action(self) -> "SubmitCreatorDecisionCommand":
        _validate_decision_values(
            self.action,
            selected_option_id=self.selected_option_id,
            feedback=self.feedback,
            edited_payload=self.edited_payload,
        )
        return self


_TASK_TRANSITIONS: dict[CreatorTaskStatus, frozenset[CreatorTaskStatus]] = {
    CreatorTaskStatus.CREATED: frozenset(
        {CreatorTaskStatus.QUEUED, CreatorTaskStatus.CANCELLED}
    ),
    CreatorTaskStatus.QUEUED: frozenset(
        {CreatorTaskStatus.RUNNING, CreatorTaskStatus.CANCELLED}
    ),
    CreatorTaskStatus.RUNNING: frozenset(
        {
            CreatorTaskStatus.WAITING_HUMAN,
            CreatorTaskStatus.RETRYING,
            CreatorTaskStatus.COMPLETED,
            CreatorTaskStatus.FAILED,
            CreatorTaskStatus.CANCELLED,
        }
    ),
    CreatorTaskStatus.WAITING_HUMAN: frozenset(
        {CreatorTaskStatus.RUNNING, CreatorTaskStatus.CANCELLED}
    ),
    CreatorTaskStatus.RETRYING: frozenset(
        {
            CreatorTaskStatus.RUNNING,
            CreatorTaskStatus.FAILED,
            CreatorTaskStatus.CANCELLED,
        }
    ),
    CreatorTaskStatus.FAILED: frozenset({CreatorTaskStatus.QUEUED}),
    CreatorTaskStatus.COMPLETED: frozenset(),
    CreatorTaskStatus.CANCELLED: frozenset(),
}

_RUN_TRANSITIONS: dict[CreatorRunStatus, frozenset[CreatorRunStatus]] = {
    CreatorRunStatus.QUEUED: frozenset(
        {CreatorRunStatus.RUNNING, CreatorRunStatus.CANCELLED}
    ),
    CreatorRunStatus.RUNNING: frozenset(
        {
            CreatorRunStatus.WAITING_HUMAN,
            CreatorRunStatus.RETRYING,
            CreatorRunStatus.COMPLETED,
            CreatorRunStatus.FAILED,
            CreatorRunStatus.CANCELLED,
        }
    ),
    CreatorRunStatus.WAITING_HUMAN: frozenset(
        {CreatorRunStatus.RUNNING, CreatorRunStatus.CANCELLED}
    ),
    CreatorRunStatus.RETRYING: frozenset(
        {CreatorRunStatus.RUNNING, CreatorRunStatus.FAILED, CreatorRunStatus.CANCELLED}
    ),
    CreatorRunStatus.COMPLETED: frozenset(),
    CreatorRunStatus.FAILED: frozenset(),
    CreatorRunStatus.CANCELLED: frozenset(),
}


class CreatorTask(FrozenModel):
    id: str
    tenant_id: str
    creator_id: str
    session_id: str | None = None
    kind: CreatorTaskKind
    goal: CreatorGoal
    status: CreatorTaskStatus
    version: int = Field(ge=1)
    active_run_id: str
    final_artifact_id: str | None = Field(default=None, max_length=128)
    pending_decision_id: str | None = Field(default=None, max_length=128)
    trace_id: str
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    def transition(
        self,
        target: CreatorTaskStatus,
        *,
        now: datetime,
        final_artifact_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        active_run_id: str | None = None,
        pending_decision_id: str | None = None,
    ) -> "CreatorTask":
        if target == self.status:
            return self
        if target not in _TASK_TRANSITIONS[self.status]:
            raise CreatorInvalidTransitionError(
                f"Task {self.id} cannot transition from {self.status.value} to {target.value}",
                details={
                    "task_id": self.id,
                    "from": self.status.value,
                    "to": target.value,
                },
            )
        return self.model_copy(
            update={
                "status": target,
                "version": self.version + 1,
                "active_run_id": active_run_id or self.active_run_id,
                "final_artifact_id": final_artifact_id,
                "pending_decision_id": pending_decision_id,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": now,
            }
        )

    def mark_cancel_requested(self, *, now: datetime) -> "CreatorTask":
        if self.cancel_requested:
            return self
        return self.model_copy(
            update={
                "cancel_requested": True,
                "version": self.version + 1,
                "updated_at": now,
            }
        )


class CreatorRun(FrozenModel):
    id: str
    task_id: str
    thread_id: str
    attempt: int = Field(ge=1)
    execution_attempts: int = Field(default=0, ge=0)
    status: CreatorRunStatus
    version: int = Field(ge=1)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    checkpoint_id: str | None = None
    pending_decision_id: str | None = Field(default=None, max_length=128)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    def lease_is_active(self, now: datetime) -> bool:
        return (
            self.lease_owner is not None
            and self.lease_expires_at is not None
            and self.lease_expires_at > now
        )

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> "CreatorRun":
        if self.status not in {
            CreatorRunStatus.QUEUED,
            CreatorRunStatus.RETRYING,
            CreatorRunStatus.RUNNING,
            CreatorRunStatus.WAITING_HUMAN,
        }:
            raise CreatorInvalidTransitionError(
                f"Run {self.id} cannot be claimed from {self.status.value}",
                details={"run_id": self.id, "status": self.status.value},
            )
        return self.model_copy(
            update={
                "status": CreatorRunStatus.RUNNING,
                "execution_attempts": self.execution_attempts + 1,
                "version": self.version + 1,
                "lease_owner": worker_id,
                "lease_expires_at": lease_expires_at,
                "error_code": None,
                "error_message": None,
                "retryable": False,
                "pending_decision_id": None,
                "started_at": self.started_at or now,
                "ended_at": None,
                "updated_at": now,
            }
        )

    def renew_lease(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> "CreatorRun":
        if self.status != CreatorRunStatus.RUNNING or self.lease_owner != worker_id:
            raise CreatorInvalidTransitionError(
                f"Worker {worker_id} does not own running lease for {self.id}",
                details={"run_id": self.id, "worker_id": worker_id},
            )
        return self.model_copy(
            update={
                "version": self.version + 1,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            }
        )

    def transition(
        self,
        target: CreatorRunStatus,
        *,
        now: datetime,
        checkpoint_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        pending_decision_id: str | None = None,
    ) -> "CreatorRun":
        if target == self.status:
            return self
        if target not in _RUN_TRANSITIONS[self.status]:
            raise CreatorInvalidTransitionError(
                f"Run {self.id} cannot transition from {self.status.value} to {target.value}",
                details={
                    "run_id": self.id,
                    "from": self.status.value,
                    "to": target.value,
                },
            )
        ended_at = (
            now
            if target
            in {
                CreatorRunStatus.COMPLETED,
                CreatorRunStatus.FAILED,
                CreatorRunStatus.CANCELLED,
            }
            else None
        )
        return self.model_copy(
            update={
                "status": target,
                "version": self.version + 1,
                "lease_owner": None,
                "lease_expires_at": None,
                "checkpoint_id": checkpoint_id or self.checkpoint_id,
                "error_code": error_code,
                "error_message": error_message,
                "retryable": retryable,
                "pending_decision_id": pending_decision_id,
                "ended_at": ended_at,
                "updated_at": now,
            }
        )


class CreatorRunEvent(FrozenModel):
    id: str
    task_id: str
    run_id: str
    sequence: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    created_at: datetime


class CreatorOutboxMessage(FrozenModel):
    id: str
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=64)
    topic: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class CreatorIdempotencyRecord(FrozenModel):
    id: str
    scope: str
    key_hash: str
    request_hash: str
    response: dict[str, Any]
    task_id: str
    created_at: datetime
    expires_at: datetime


class RuntimeDecisionRequest(FrozenModel):
    decision_id: str = Field(min_length=1, max_length=128)
    interrupt_id: str = Field(min_length=1, max_length=128)
    kind: CreatorDecisionKind
    prompt: str = Field(min_length=1, max_length=2_000)
    source_artifact_id: str = Field(min_length=1, max_length=128)
    allowed_actions: tuple[CreatorDecisionAction, ...] = Field(min_length=1)
    allowed_option_ids: tuple[str, ...] = ()


class RuntimeHumanDecision(FrozenModel):
    decision_id: str = Field(min_length=1, max_length=128)
    interrupt_id: str = Field(min_length=1, max_length=128)
    kind: CreatorDecisionKind
    action: CreatorDecisionAction
    actor_id: str = Field(min_length=1, max_length=128)
    selected_option_id: str | None = Field(default=None, max_length=128)
    feedback: str | None = Field(default=None, max_length=4_000)
    edited_payload: dict[str, Any] | None = None
    submitted_at: datetime

    @model_validator(mode="after")
    def validate_action(self) -> "RuntimeHumanDecision":
        _validate_decision_values(
            self.action,
            selected_option_id=self.selected_option_id,
            feedback=self.feedback,
            edited_payload=self.edited_payload,
        )
        return self


class CreatorHumanDecision(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    interrupt_id: str = Field(min_length=1, max_length=128)
    kind: CreatorDecisionKind
    prompt: str = Field(min_length=1, max_length=2_000)
    source_artifact_id: str = Field(min_length=1, max_length=128)
    allowed_actions: tuple[CreatorDecisionAction, ...] = Field(min_length=1)
    allowed_option_ids: tuple[str, ...] = ()
    status: CreatorDecisionStatus
    version: int = Field(ge=1)
    submission_hash: str | None = Field(default=None, min_length=64, max_length=64)
    idempotency_key_hash: str | None = Field(default=None, min_length=64, max_length=64)
    action: CreatorDecisionAction | None = None
    actor_id: str | None = Field(default=None, max_length=128)
    selected_option_id: str | None = Field(default=None, max_length=128)
    feedback: str | None = Field(default=None, max_length=4_000)
    edited_payload: dict[str, Any] | None = None
    created_at: datetime
    submitted_at: datetime | None = None
    applied_at: datetime | None = None

    def submit(
        self,
        decision: RuntimeHumanDecision,
        *,
        submission_hash: str,
        idempotency_key_hash: str,
    ) -> "CreatorHumanDecision":
        if self.status != CreatorDecisionStatus.PENDING:
            raise CreatorInvalidTransitionError(
                f"Decision {self.id} cannot be submitted from {self.status.value}",
                details={"decision_id": self.id, "status": self.status.value},
            )
        if (
            decision.decision_id != self.id
            or decision.interrupt_id != self.interrupt_id
        ):
            raise CreatorInvalidTransitionError(
                f"Decision {self.id} does not match its checkpoint interrupt",
                details={"decision_id": self.id},
            )
        if decision.kind != self.kind or decision.action not in self.allowed_actions:
            raise CreatorInvalidTransitionError(
                f"Decision action {decision.action.value} is not allowed",
                details={"decision_id": self.id, "kind": self.kind.value},
            )
        if (
            decision.selected_option_id is not None
            and decision.selected_option_id not in self.allowed_option_ids
        ):
            raise CreatorInvalidTransitionError(
                f"Decision option {decision.selected_option_id} is not allowed",
                details={"decision_id": self.id},
            )
        if decision.action == CreatorDecisionAction.EDIT and (
            self.kind == CreatorDecisionKind.TOPIC_SELECTION
            and not decision.selected_option_id
        ):
            raise CreatorInvalidTransitionError(
                "EDIT for topic selection requires selected_option_id",
                details={"decision_id": self.id},
            )
        return self.model_copy(
            update={
                "status": CreatorDecisionStatus.SUBMITTED,
                "version": self.version + 1,
                "submission_hash": submission_hash,
                "idempotency_key_hash": idempotency_key_hash,
                "action": decision.action,
                "actor_id": decision.actor_id,
                "selected_option_id": decision.selected_option_id,
                "feedback": decision.feedback,
                "edited_payload": decision.edited_payload,
                "submitted_at": decision.submitted_at,
            }
        )

    def mark_applied(self, *, now: datetime) -> "CreatorHumanDecision":
        if self.status != CreatorDecisionStatus.SUBMITTED:
            raise CreatorInvalidTransitionError(
                f"Decision {self.id} cannot be applied from {self.status.value}",
                details={"decision_id": self.id, "status": self.status.value},
            )
        return self.model_copy(
            update={
                "status": CreatorDecisionStatus.APPLIED,
                "version": self.version + 1,
                "applied_at": now,
            }
        )


class RuntimeErrorInfo(FrozenModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4_000)
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvent(FrozenModel):
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeStartRequest(FrozenModel):
    task_id: str
    run_id: str
    thread_id: str
    tenant_id: str
    creator_id: str
    session_id: str | None = None
    kind: CreatorTaskKind
    goal: CreatorGoal
    trace_id: str
    execution_attempt: int = Field(ge=1)


class RuntimeResumeRequest(FrozenModel):
    task_id: str
    run_id: str
    thread_id: str
    tenant_id: str
    creator_id: str
    session_id: str | None = None
    kind: CreatorTaskKind
    goal: CreatorGoal
    trace_id: str
    execution_attempt: int = Field(ge=1)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    decision: RuntimeHumanDecision


class RuntimeOutcome(FrozenModel):
    status: RuntimeOutcomeStatus
    checkpoint_id: str | None = Field(default=None, max_length=128)
    final_artifact_id: str | None = Field(default=None, max_length=128)
    decision_request: RuntimeDecisionRequest | None = None
    applied_decision_id: str | None = Field(default=None, max_length=128)
    error: RuntimeErrorInfo | None = None
    events: tuple[RuntimeEvent, ...] = ()
    state_summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "RuntimeOutcome":
        if self.status == RuntimeOutcomeStatus.COMPLETED and not self.final_artifact_id:
            raise ValueError("COMPLETED runtime outcome requires final_artifact_id")
        if self.status == RuntimeOutcomeStatus.WAITING_HUMAN:
            if not self.checkpoint_id:
                raise ValueError("WAITING_HUMAN runtime outcome requires checkpoint_id")
            if self.decision_request is None:
                raise ValueError(
                    "WAITING_HUMAN runtime outcome requires decision_request"
                )
        elif self.decision_request is not None:
            raise ValueError(
                f"{self.status.value} runtime outcome cannot include decision_request"
            )
        if (
            self.status
            in {
                RuntimeOutcomeStatus.RETRYABLE_ERROR,
                RuntimeOutcomeStatus.FAILED,
            }
            and self.error is None
        ):
            raise ValueError(f"{self.status.value} runtime outcome requires error")
        if (
            self.status
            not in {
                RuntimeOutcomeStatus.RETRYABLE_ERROR,
                RuntimeOutcomeStatus.FAILED,
            }
            and self.error is not None
        ):
            raise ValueError(
                f"{self.status.value} runtime outcome cannot include error"
            )
        return self


class CreatorTaskResult(FrozenModel):
    task_id: str
    run_id: str
    status: CreatorTaskStatus
    version: int
    trace_id: str
    pending_decision_id: str | None = None
    replayed: bool = False


class CreatorRunExecutionResult(FrozenModel):
    task_id: str
    run_id: str
    task_status: CreatorTaskStatus
    run_status: CreatorRunStatus
    task_version: int
    invoked: bool
    final_artifact_id: str | None = None
    pending_decision_id: str | None = None
    error_code: str | None = None


class CreatorDecisionResult(FrozenModel):
    task_id: str
    run_id: str
    task_status: CreatorTaskStatus
    run_status: CreatorRunStatus
    task_version: int
    final_artifact_id: str | None = None
    pending_decision_id: str | None = None
    applied_decision_id: str | None = None
    replayed: bool = False


def _validate_decision_values(
    action: CreatorDecisionAction,
    *,
    selected_option_id: str | None,
    feedback: str | None,
    edited_payload: dict[str, Any] | None = None,
) -> None:
    if action == CreatorDecisionAction.SELECT and not selected_option_id:
        raise ValueError("SELECT requires selected_option_id")
    if action == CreatorDecisionAction.APPROVE and selected_option_id is not None:
        raise ValueError("APPROVE cannot include selected_option_id")
    if action == CreatorDecisionAction.REQUEST_CHANGES and not (
        feedback and feedback.strip()
    ):
        raise ValueError("REQUEST_CHANGES requires feedback")
    if action == CreatorDecisionAction.EDIT:
        if not isinstance(edited_payload, dict) or not edited_payload:
            raise ValueError("EDIT requires edited_payload")
        if selected_option_id is None and not (
            "outline" in edited_payload
            or "annotations" in edited_payload
            or "document" in edited_payload
        ):
            raise ValueError(
                "EDIT requires selected_option_id for topics, outline payload, "
                "or draft annotations/document"
            )
    elif edited_payload is not None:
        raise ValueError("edited_payload is only valid for EDIT")
