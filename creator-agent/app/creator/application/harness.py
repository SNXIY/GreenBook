from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from app.creator.application.ports import (
    CreatorRuntimePort,
    CreatorTaskMemoryPort,
    CreatorUnitOfWork,
    CreatorUnitOfWorkFactory,
)
from app.creator.domain.errors import (
    CreatorCheckpointConflictError,
    CreatorDecisionConflictError,
    CreatorDecisionNotFoundError,
    CreatorIdempotencyConflictError,
    CreatorInvalidTransitionError,
    CreatorPersistenceConflictError,
    CreatorRunLeaseConflictError,
    CreatorRunNotFoundError,
    CreatorRuntimeContractError,
    CreatorRuntimeFatalError,
    CreatorRuntimeRetryableError,
    CreatorScopeViolationError,
    CreatorStaleWorkerResultError,
    CreatorTaskNotFoundError,
    CreatorTaskVersionConflictError,
)
from app.creator.domain.models import (
    CancelCreatorTaskCommand,
    CreateCreatorTaskCommand,
    CreatorDecisionResult,
    CreatorDecisionStatus,
    CreatorHumanDecision,
    CreatorIdempotencyRecord,
    CreatorOutboxMessage,
    CreatorRun,
    CreatorRunEvent,
    CreatorRunExecutionResult,
    CreatorRunStatus,
    CreatorTask,
    CreatorTaskResult,
    CreatorTaskStatus,
    OutboxStatus,
    RetryCreatorTaskCommand,
    RuntimeHumanDecision,
    RuntimeErrorInfo,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
    RuntimeResumeRequest,
    RuntimeStartRequest,
    SubmitCreatorDecisionCommand,
)


logger = logging.getLogger(__name__)


class CreatorHarnessSettings(Protocol):
    creator_runtime_max_attempts: int
    creator_run_lease_seconds: int
    creator_retry_delay_seconds: int
    creator_idempotency_ttl_seconds: int
    creator_max_runtime_events: int
    creator_max_event_payload_bytes: int


@dataclass(frozen=True)
class CreatorHarnessPolicy:
    max_runtime_attempts: int = 3
    run_lease_seconds: int = 120
    retry_delay_seconds: int = 15
    idempotency_ttl_seconds: int = 86_400
    max_runtime_events: int = 100
    max_event_payload_bytes: int = 65_536

    @classmethod
    def from_settings(cls, settings: CreatorHarnessSettings) -> "CreatorHarnessPolicy":
        return cls(
            max_runtime_attempts=settings.creator_runtime_max_attempts,
            run_lease_seconds=settings.creator_run_lease_seconds,
            retry_delay_seconds=settings.creator_retry_delay_seconds,
            idempotency_ttl_seconds=settings.creator_idempotency_ttl_seconds,
            max_runtime_events=settings.creator_max_runtime_events,
            max_event_payload_bytes=settings.creator_max_event_payload_bytes,
        )

    def __post_init__(self) -> None:
        for name in (
            "max_runtime_attempts",
            "run_lease_seconds",
            "idempotency_ttl_seconds",
            "max_runtime_events",
            "max_event_payload_bytes",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")


@dataclass(frozen=True)
class _ClaimedRun:
    task: CreatorTask
    run: CreatorRun
    recovered: bool


@dataclass(frozen=True)
class _ClaimedDecision:
    task: CreatorTask
    run: CreatorRun
    decision: CreatorHumanDecision
    runtime_decision: RuntimeHumanDecision
    request_hash: str
    scope: str
    key_hash: str


@dataclass(frozen=True)
class _DecisionResumeContext:
    decision_id: str
    request_hash: str
    scope: str
    key_hash: str
    persist_idempotency: bool = True


class CreatorAgentHarness:
    """Durable application boundary for creator task execution.

    The Harness owns task/run lifecycle, idempotency, leases, Outbox commands,
    runtime invocation, and outcome projection. It deliberately delegates
    content planning and Agent selection to the injected runtime.
    """

    def __init__(
        self,
        *,
        uow_factory: CreatorUnitOfWorkFactory,
        runtime: CreatorRuntimePort,
        policy: CreatorHarnessPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        id_generator: Callable[[], str] | None = None,
        task_memory: CreatorTaskMemoryPort | None = None,
    ):
        self._uow_factory = uow_factory
        self._runtime = runtime
        self._policy = policy or CreatorHarnessPolicy()
        self._clock = clock or _utc_now
        self._new_id = id_generator or (lambda: str(uuid.uuid4()))
        self._task_memory = task_memory

    async def create_task(self, command: CreateCreatorTaskCommand) -> CreatorTaskResult:
        request_hash = _create_request_hash(command)
        scope = _create_idempotency_scope(command)
        key_hash = _hash_text(command.idempotency_key)

        try:
            async with self._uow_factory() as uow:
                existing = await uow.idempotency.get(scope, key_hash)
                if existing is not None:
                    return _replay_idempotent(existing, request_hash)

                now = self._clock()
                task_id = self._new_id()
                run_id = self._new_id()
                thread_id = self._new_id()
                trace_id = command.trace_id or self._new_id()
                task = CreatorTask(
                    id=task_id,
                    tenant_id=command.tenant_id,
                    creator_id=command.creator_id,
                    session_id=command.session_id,
                    kind=command.kind,
                    goal=command.creator_goal(),
                    status=CreatorTaskStatus.QUEUED,
                    version=1,
                    active_run_id=run_id,
                    trace_id=trace_id,
                    created_at=now,
                    updated_at=now,
                )
                run = CreatorRun(
                    id=run_id,
                    task_id=task_id,
                    thread_id=thread_id,
                    attempt=1,
                    status=CreatorRunStatus.QUEUED,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                result = _task_result(task)

                await uow.tasks.add(task)
                await uow.flush()
                await uow.runs.add(run)
                await self._append_event(
                    uow,
                    task=task,
                    run=run,
                    event_type="task.created",
                    payload={"kind": task.kind.value, "status": task.status.value},
                    now=now,
                )
                await uow.outbox.add(
                    self._run_outbox(
                        run=run,
                        task=task,
                        topic="creator.run.start",
                        available_at=now,
                    )
                )
                await uow.idempotency.add(
                    CreatorIdempotencyRecord(
                        id=self._new_id(),
                        scope=scope,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        response=result.model_dump(mode="json"),
                        task_id=task.id,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self._policy.idempotency_ttl_seconds),
                    )
                )
                await uow.commit()
                await self._remember_task(task, run)
                return result
        except CreatorPersistenceConflictError:
            return await self._replay_after_create_race(scope, key_hash, request_hash)

    async def get_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> CreatorTaskResult:
        async with self._uow_factory() as uow:
            task = await self._require_task(uow, task_id)
            self._require_scope(task, tenant_id, creator_id)
            return _task_result(task)

    async def get_decision(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        decision_id: str,
    ) -> CreatorHumanDecision:
        async with self._uow_factory() as uow:
            task = await self._require_task(uow, task_id)
            self._require_scope(task, tenant_id, creator_id)
            decision = await uow.decisions.get(decision_id)
            if (
                decision is None
                or decision.task_id != task.id
                or decision.run_id != task.active_run_id
            ):
                raise CreatorDecisionNotFoundError(
                    f"Creator decision {decision_id} was not found",
                    details={"decision_id": decision_id, "task_id": task_id},
                )
            return decision

    async def start_run(
        self, run_id: str, *, worker_id: str
    ) -> CreatorRunExecutionResult:
        claimed = await self._claim_run(run_id, worker_id=worker_id)
        if isinstance(claimed, CreatorRunExecutionResult):
            return claimed

        request = RuntimeStartRequest(
            task_id=claimed.task.id,
            run_id=claimed.run.id,
            thread_id=claimed.run.thread_id,
            tenant_id=claimed.task.tenant_id,
            creator_id=claimed.task.creator_id,
            session_id=claimed.task.session_id,
            kind=claimed.task.kind,
            goal=claimed.task.goal,
            trace_id=claimed.task.trace_id,
            execution_attempt=claimed.run.execution_attempts,
        )
        outcome = await self._invoke_runtime(
            request,
            run_id=run_id,
            worker_id=worker_id,
        )
        return await self._apply_runtime_outcome(
            run_id=run_id,
            worker_id=worker_id,
            outcome=outcome,
        )

    async def recover_run(
        self, run_id: str, *, worker_id: str
    ) -> CreatorRunExecutionResult:
        """Reclaim an expired run lease and invoke the same stable thread."""

        return await self.start_run(run_id, worker_id=worker_id)

    async def submit_decision(
        self,
        command: SubmitCreatorDecisionCommand,
        *,
        worker_id: str,
    ) -> CreatorDecisionResult:
        claimed = await self._claim_decision(command, worker_id=worker_id)
        if isinstance(claimed, CreatorDecisionResult):
            return claimed

        request = RuntimeResumeRequest(
            task_id=claimed.task.id,
            run_id=claimed.run.id,
            thread_id=claimed.run.thread_id,
            tenant_id=claimed.task.tenant_id,
            creator_id=claimed.task.creator_id,
            session_id=claimed.task.session_id,
            kind=claimed.task.kind,
            goal=claimed.task.goal,
            trace_id=claimed.task.trace_id,
            execution_attempt=claimed.run.execution_attempts,
            checkpoint_id=claimed.decision.checkpoint_id,
            decision=claimed.runtime_decision,
        )
        outcome = await self._invoke_runtime_resume(
            request,
            run_id=claimed.run.id,
            worker_id=worker_id,
        )
        execution = await self._apply_runtime_outcome(
            run_id=claimed.run.id,
            worker_id=worker_id,
            outcome=outcome,
            resume_context=_DecisionResumeContext(
                decision_id=claimed.decision.id,
                request_hash=claimed.request_hash,
                scope=claimed.scope,
                key_hash=claimed.key_hash,
            ),
        )
        return CreatorDecisionResult(
            task_id=execution.task_id,
            run_id=execution.run_id,
            task_status=execution.task_status,
            run_status=execution.run_status,
            task_version=execution.task_version,
            final_artifact_id=execution.final_artifact_id,
            pending_decision_id=execution.pending_decision_id,
            applied_decision_id=outcome.applied_decision_id,
        )

    async def enqueue_decision(
        self,
        command: SubmitCreatorDecisionCommand,
    ) -> CreatorDecisionResult:
        """Persist a validated decision and its resume command atomically."""

        queued = await self._claim_decision(
            command,
            worker_id="outbox",
            enqueue_only=True,
        )
        if not isinstance(queued, CreatorDecisionResult):
            raise CreatorDecisionConflictError(
                f"Decision {command.decision_id} was not queued",
                details={"decision_id": command.decision_id},
            )
        return queued

    async def resume_decision(
        self,
        decision_id: str,
        *,
        worker_id: str,
    ) -> CreatorDecisionResult:
        """Resume one previously persisted SUBMITTED decision."""

        claimed = await self._claim_submitted_decision(
            decision_id,
            worker_id=worker_id,
        )
        if isinstance(claimed, CreatorDecisionResult):
            return claimed

        request = RuntimeResumeRequest(
            task_id=claimed.task.id,
            run_id=claimed.run.id,
            thread_id=claimed.run.thread_id,
            tenant_id=claimed.task.tenant_id,
            creator_id=claimed.task.creator_id,
            session_id=claimed.task.session_id,
            kind=claimed.task.kind,
            goal=claimed.task.goal,
            trace_id=claimed.task.trace_id,
            execution_attempt=claimed.run.execution_attempts,
            checkpoint_id=claimed.decision.checkpoint_id,
            decision=claimed.runtime_decision,
        )
        outcome = await self._invoke_runtime_resume(
            request,
            run_id=claimed.run.id,
            worker_id=worker_id,
        )
        execution = await self._apply_runtime_outcome(
            run_id=claimed.run.id,
            worker_id=worker_id,
            outcome=outcome,
            resume_context=_DecisionResumeContext(
                decision_id=claimed.decision.id,
                request_hash=claimed.request_hash,
                scope=claimed.scope,
                key_hash=claimed.key_hash,
                persist_idempotency=False,
            ),
        )
        return CreatorDecisionResult(
            task_id=execution.task_id,
            run_id=execution.run_id,
            task_status=execution.task_status,
            run_status=execution.run_status,
            task_version=execution.task_version,
            final_artifact_id=execution.final_artifact_id,
            pending_decision_id=execution.pending_decision_id,
            applied_decision_id=outcome.applied_decision_id,
        )

    async def renew_run_lease(self, run_id: str, *, worker_id: str) -> None:
        now = self._clock()
        async with self._uow_factory() as uow:
            run = await self._require_run(uow, run_id, for_update=True)
            if run.lease_owner != worker_id or not run.lease_is_active(now):
                raise CreatorRunLeaseConflictError(
                    f"Worker {worker_id} cannot renew lease for run {run_id}",
                    details={"run_id": run_id, "worker_id": worker_id},
                )
            renewed = run.renew_lease(
                worker_id=worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.run_lease_seconds),
            )
            await uow.runs.save(renewed, expected_version=run.version)
            await uow.commit()

    async def request_cancel(
        self, command: CancelCreatorTaskCommand
    ) -> CreatorTaskResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            task = await self._require_task(uow, command.task_id, for_update=True)
            self._require_scope(task, command.tenant_id, command.creator_id)
            if task.status == CreatorTaskStatus.CANCELLED:
                return _task_result(task)
            if task.version != command.expected_version:
                raise CreatorTaskVersionConflictError(
                    f"Task {task.id} changed from version {command.expected_version} to {task.version}",
                    details={
                        "task_id": task.id,
                        "expected_version": command.expected_version,
                        "actual_version": task.version,
                    },
                )
            if task.status in {CreatorTaskStatus.COMPLETED, CreatorTaskStatus.FAILED}:
                raise CreatorInvalidTransitionError(
                    f"Task {task.id} cannot be cancelled from {task.status.value}",
                    details={"task_id": task.id, "status": task.status.value},
                )

            run = await self._require_run(uow, task.active_run_id, for_update=True)
            if task.status == CreatorTaskStatus.RUNNING:
                updated_task = task.mark_cancel_requested(now=now)
                updated_run = run
                await uow.tasks.save(updated_task, expected_version=task.version)
                await uow.outbox.add(
                    self._run_outbox(
                        run=run,
                        task=updated_task,
                        topic="creator.run.cancel",
                        available_at=now,
                    )
                )
                await self._append_event(
                    uow,
                    task=updated_task,
                    run=run,
                    event_type="task.cancel_requested",
                    payload={"worker_id": run.lease_owner},
                    now=now,
                )
            else:
                updated_task = task.transition(CreatorTaskStatus.CANCELLED, now=now)
                updated_run = run.transition(CreatorRunStatus.CANCELLED, now=now)
                await uow.tasks.save(updated_task, expected_version=task.version)
                await uow.runs.save(updated_run, expected_version=run.version)
                await self._append_event(
                    uow,
                    task=updated_task,
                    run=updated_run,
                    event_type="task.cancelled",
                    payload={"status": updated_task.status.value},
                    now=now,
                )
            await uow.commit()
            await self._remember_task(updated_task, updated_run)
            return _task_result(updated_task)

    async def retry_task(self, command: RetryCreatorTaskCommand) -> CreatorTaskResult:
        request_hash = _retry_request_hash(command)
        scope = _retry_idempotency_scope(command)
        key_hash = _hash_text(command.idempotency_key)

        try:
            async with self._uow_factory() as uow:
                existing = await uow.idempotency.get(scope, key_hash)
                if existing is not None:
                    return _replay_idempotent(existing, request_hash)

                task = await self._require_task(uow, command.task_id, for_update=True)
                self._require_scope(task, command.tenant_id, command.creator_id)
                if task.version != command.expected_version:
                    raise CreatorTaskVersionConflictError(
                        f"Task {task.id} changed from version {command.expected_version} to {task.version}",
                        details={
                            "task_id": task.id,
                            "expected_version": command.expected_version,
                            "actual_version": task.version,
                        },
                    )
                if task.status != CreatorTaskStatus.FAILED:
                    raise CreatorInvalidTransitionError(
                        f"Task {task.id} cannot retry from {task.status.value}",
                        details={"task_id": task.id, "status": task.status.value},
                    )

                now = self._clock()
                run_id = self._new_id()
                updated_task = task.transition(
                    CreatorTaskStatus.QUEUED,
                    now=now,
                    active_run_id=run_id,
                ).model_copy(update={"cancel_requested": False})
                run = CreatorRun(
                    id=run_id,
                    task_id=task.id,
                    thread_id=self._new_id(),
                    attempt=(await uow.runs.max_attempt(task.id)) + 1,
                    status=CreatorRunStatus.QUEUED,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                result = _task_result(updated_task)

                await uow.tasks.save(updated_task, expected_version=task.version)
                await uow.runs.add(run)
                await self._append_event(
                    uow,
                    task=updated_task,
                    run=run,
                    event_type="run.queued",
                    payload={"attempt": run.attempt, "reason": "explicit_retry"},
                    now=now,
                )
                await uow.outbox.add(
                    self._run_outbox(
                        run=run,
                        task=updated_task,
                        topic="creator.run.start",
                        available_at=now,
                    )
                )
                await uow.idempotency.add(
                    CreatorIdempotencyRecord(
                        id=self._new_id(),
                        scope=scope,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        response=result.model_dump(mode="json"),
                        task_id=task.id,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self._policy.idempotency_ttl_seconds),
                    )
                )
                await uow.commit()
                await self._remember_task(updated_task, run)
                return result
        except CreatorPersistenceConflictError:
            return await self._replay_after_create_race(scope, key_hash, request_hash)

    async def _claim_run(
        self,
        run_id: str,
        *,
        worker_id: str,
    ) -> _ClaimedRun | CreatorRunExecutionResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            run = await self._require_run(uow, run_id, for_update=True)
            task = await self._require_task(uow, run.task_id, for_update=True)

            if task.active_run_id != run.id:
                return _execution_result(task, run, invoked=False)
            if run.status in {
                CreatorRunStatus.COMPLETED,
                CreatorRunStatus.FAILED,
                CreatorRunStatus.CANCELLED,
                CreatorRunStatus.WAITING_HUMAN,
            }:
                return _execution_result(task, run, invoked=False)
            recovered = run.status == CreatorRunStatus.RUNNING
            if recovered and run.lease_is_active(now):
                raise CreatorRunLeaseConflictError(
                    f"Run {run.id} is leased by {run.lease_owner}",
                    details={
                        "run_id": run.id,
                        "lease_owner": run.lease_owner,
                        "lease_expires_at": (
                            run.lease_expires_at.isoformat()
                            if run.lease_expires_at
                            else None
                        ),
                    },
                )

            if task.cancel_requested:
                cancelled_task = (
                    task
                    if task.status == CreatorTaskStatus.CANCELLED
                    else task.transition(CreatorTaskStatus.CANCELLED, now=now)
                )
                cancelled_run = run.transition(CreatorRunStatus.CANCELLED, now=now)
                if cancelled_task is not task:
                    await uow.tasks.save(cancelled_task, expected_version=task.version)
                await uow.runs.save(cancelled_run, expected_version=run.version)
                await self._append_event(
                    uow,
                    task=cancelled_task,
                    run=cancelled_run,
                    event_type="task.cancelled",
                    payload={"reason": "cancel_requested_before_runtime"},
                    now=now,
                )
                await uow.commit()
                await self._remember_task(cancelled_task, cancelled_run)
                return _execution_result(cancelled_task, cancelled_run, invoked=False)

            if task.status not in {
                CreatorTaskStatus.QUEUED,
                CreatorTaskStatus.RETRYING,
                CreatorTaskStatus.RUNNING,
            }:
                return _execution_result(task, run, invoked=False)

            claimed_run = run.claim(
                worker_id=worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.run_lease_seconds),
            )
            if task.status in {CreatorTaskStatus.QUEUED, CreatorTaskStatus.RETRYING}:
                claimed_task = task.transition(CreatorTaskStatus.RUNNING, now=now)
                await uow.tasks.save(claimed_task, expected_version=task.version)
            else:
                claimed_task = task
            await uow.runs.save(claimed_run, expected_version=run.version)
            await self._append_event(
                uow,
                task=claimed_task,
                run=claimed_run,
                event_type="run.recovered" if recovered else "run.started",
                payload={
                    "runtime": self._runtime.name,
                    "execution_attempt": claimed_run.execution_attempts,
                    "worker_id": worker_id,
                },
                now=now,
            )
            await uow.commit()
            await self._remember_task(claimed_task, claimed_run)
            return _ClaimedRun(task=claimed_task, run=claimed_run, recovered=recovered)

    async def _claim_decision(
        self,
        command: SubmitCreatorDecisionCommand,
        *,
        worker_id: str,
        enqueue_only: bool = False,
    ) -> _ClaimedDecision | CreatorDecisionResult:
        request_hash = _decision_request_hash(command)
        scope = _decision_idempotency_scope(command)
        key_hash = _hash_text(command.idempotency_key)
        now = self._clock()

        async with self._uow_factory() as uow:
            existing = await uow.idempotency.get(scope, key_hash)
            if existing is not None:
                return _replay_decision_idempotent(existing, request_hash)

            task = await self._require_task(uow, command.task_id, for_update=True)
            self._require_scope(task, command.tenant_id, command.creator_id)
            if command.actor_id != command.creator_id:
                raise CreatorScopeViolationError(
                    "Decision actor must be the task owner",
                    details={
                        "task_id": task.id,
                        "actor_id": command.actor_id,
                    },
                )
            run = await self._require_run(
                uow,
                task.active_run_id,
                for_update=True,
            )
            decision = await uow.decisions.get(
                command.decision_id,
                for_update=True,
            )
            if (
                decision is None
                or decision.task_id != task.id
                or decision.run_id != run.id
            ):
                raise CreatorDecisionNotFoundError(
                    f"Creator decision {command.decision_id} was not found",
                    details={
                        "decision_id": command.decision_id,
                        "task_id": task.id,
                    },
                )

            if decision.status == CreatorDecisionStatus.APPLIED:
                if (
                    decision.submission_hash != request_hash
                    or decision.idempotency_key_hash != key_hash
                ):
                    raise CreatorDecisionConflictError(
                        f"Decision {decision.id} was already applied differently",
                        details={"decision_id": decision.id},
                    )
                return _decision_result(
                    task,
                    run,
                    applied_decision_id=decision.id,
                    replayed=True,
                )

            if (
                enqueue_only
                and decision.status == CreatorDecisionStatus.SUBMITTED
                and decision.submission_hash == request_hash
                and decision.idempotency_key_hash == key_hash
            ):
                return _decision_result(
                    task,
                    run,
                    applied_decision_id=None,
                    replayed=True,
                )

            if decision.status == CreatorDecisionStatus.PENDING:
                if task.version != command.expected_version:
                    raise CreatorTaskVersionConflictError(
                        f"Task {task.id} changed from version "
                        f"{command.expected_version} to {task.version}",
                        details={
                            "task_id": task.id,
                            "expected_version": command.expected_version,
                            "actual_version": task.version,
                        },
                    )
                if (
                    task.status != CreatorTaskStatus.WAITING_HUMAN
                    or run.status != CreatorRunStatus.WAITING_HUMAN
                    or task.pending_decision_id != decision.id
                    or run.pending_decision_id != decision.id
                ):
                    raise CreatorDecisionConflictError(
                        f"Decision {decision.id} is not the active task decision",
                        details={
                            "decision_id": decision.id,
                            "task_status": task.status.value,
                            "run_status": run.status.value,
                        },
                    )
                if run.checkpoint_id != decision.checkpoint_id:
                    raise CreatorCheckpointConflictError(
                        "Run checkpoint does not match the decision",
                        details={"decision_id": decision.id},
                    )
                runtime_decision = RuntimeHumanDecision(
                    decision_id=decision.id,
                    interrupt_id=decision.interrupt_id,
                    kind=decision.kind,
                    action=command.action,
                    actor_id=command.actor_id,
                    selected_option_id=command.selected_option_id,
                    feedback=command.feedback,
                    edited_payload=command.edited_payload,
                    submitted_at=now,
                )
                submitted = decision.submit(
                    runtime_decision,
                    submission_hash=request_hash,
                    idempotency_key_hash=key_hash,
                )
                await uow.decisions.save(
                    submitted,
                    expected_version=decision.version,
                )
            else:
                if (
                    decision.submission_hash != request_hash
                    or decision.idempotency_key_hash != key_hash
                ):
                    raise CreatorDecisionConflictError(
                        f"Decision {decision.id} has another submission in progress",
                        details={"decision_id": decision.id},
                    )
                if run.status == CreatorRunStatus.RUNNING and run.lease_is_active(now):
                    raise CreatorRunLeaseConflictError(
                        f"Decision resume is leased by {run.lease_owner}",
                        details={
                            "decision_id": decision.id,
                            "lease_owner": run.lease_owner,
                        },
                    )
                if (
                    decision.action is None
                    or decision.actor_id is None
                    or decision.submitted_at is None
                ):
                    raise CreatorDecisionConflictError(
                        f"Decision {decision.id} has an incomplete submission",
                        details={"decision_id": decision.id},
                    )
                runtime_decision = RuntimeHumanDecision(
                    decision_id=decision.id,
                    interrupt_id=decision.interrupt_id,
                    kind=decision.kind,
                    action=decision.action,
                    actor_id=decision.actor_id,
                    selected_option_id=decision.selected_option_id,
                    feedback=decision.feedback,
                    edited_payload=decision.edited_payload,
                    submitted_at=decision.submitted_at,
                )
                submitted = decision

            if enqueue_only:
                queued_result = _decision_result(
                    task,
                    run,
                    applied_decision_id=None,
                )
                await uow.outbox.add(
                    self._run_outbox(
                        run=run,
                        task=task,
                        topic="creator.decision.resume",
                        available_at=now,
                        extra_payload={"decision_id": decision.id},
                    )
                )
                await self._append_event(
                    uow,
                    task=task,
                    run=run,
                    event_type="decision.submitted",
                    payload={
                        "decision_id": decision.id,
                        "kind": decision.kind.value,
                        "action": runtime_decision.action.value,
                        "actor_id": runtime_decision.actor_id,
                        "delivery": "outbox",
                    },
                    now=now,
                )
                await uow.idempotency.add(
                    CreatorIdempotencyRecord(
                        id=self._new_id(),
                        scope=scope,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        response=queued_result.model_dump(mode="json"),
                        task_id=task.id,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self._policy.idempotency_ttl_seconds),
                    )
                )
                await uow.commit()
                await self._remember_task(task, run)
                return queued_result

            if run.status not in {
                CreatorRunStatus.WAITING_HUMAN,
                CreatorRunStatus.RETRYING,
                CreatorRunStatus.RUNNING,
            }:
                raise CreatorDecisionConflictError(
                    f"Run {run.id} cannot resume from {run.status.value}",
                    details={"decision_id": decision.id, "run_id": run.id},
                )
            claimed_run = run.claim(
                worker_id=worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.run_lease_seconds),
            )
            if task.status in {
                CreatorTaskStatus.WAITING_HUMAN,
                CreatorTaskStatus.RETRYING,
            }:
                claimed_task = task.transition(
                    CreatorTaskStatus.RUNNING,
                    now=now,
                )
                await uow.tasks.save(claimed_task, expected_version=task.version)
            elif task.status == CreatorTaskStatus.RUNNING:
                claimed_task = task
            else:
                raise CreatorDecisionConflictError(
                    f"Task {task.id} cannot resume from {task.status.value}",
                    details={"decision_id": decision.id, "task_id": task.id},
                )
            await uow.runs.save(claimed_run, expected_version=run.version)
            await self._append_event(
                uow,
                task=claimed_task,
                run=claimed_run,
                event_type="decision.submitted",
                payload={
                    "decision_id": decision.id,
                    "kind": decision.kind.value,
                    "action": runtime_decision.action.value,
                    "actor_id": runtime_decision.actor_id,
                    "worker_id": worker_id,
                },
                now=now,
            )
            await uow.commit()
            await self._remember_task(claimed_task, claimed_run)
            return _ClaimedDecision(
                task=claimed_task,
                run=claimed_run,
                decision=submitted,
                runtime_decision=runtime_decision,
                request_hash=request_hash,
                scope=scope,
                key_hash=key_hash,
            )

    async def _claim_submitted_decision(
        self,
        decision_id: str,
        *,
        worker_id: str,
    ) -> _ClaimedDecision | CreatorDecisionResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            decision = await uow.decisions.get(decision_id, for_update=True)
            if decision is None:
                raise CreatorDecisionNotFoundError(
                    f"Creator decision {decision_id} was not found",
                    details={"decision_id": decision_id},
                )
            task = await self._require_task(uow, decision.task_id, for_update=True)
            run = await self._require_run(uow, decision.run_id, for_update=True)
            if task.active_run_id != run.id:
                raise CreatorDecisionConflictError(
                    f"Decision {decision.id} is not attached to the active run",
                    details={
                        "decision_id": decision.id,
                        "run_id": run.id,
                        "active_run_id": task.active_run_id,
                    },
                )
            if decision.status == CreatorDecisionStatus.APPLIED:
                return _decision_result(
                    task,
                    run,
                    applied_decision_id=decision.id,
                    replayed=True,
                )
            if decision.status != CreatorDecisionStatus.SUBMITTED:
                raise CreatorDecisionConflictError(
                    f"Decision {decision.id} is not submitted",
                    details={
                        "decision_id": decision.id,
                        "status": decision.status.value,
                    },
                )
            if (
                decision.submission_hash is None
                or decision.idempotency_key_hash is None
                or decision.action is None
                or decision.actor_id is None
                or decision.submitted_at is None
            ):
                raise CreatorDecisionConflictError(
                    f"Decision {decision.id} has an incomplete submission",
                    details={"decision_id": decision.id},
                )
            if run.status == CreatorRunStatus.RUNNING and run.lease_is_active(now):
                raise CreatorRunLeaseConflictError(
                    f"Decision resume is leased by {run.lease_owner}",
                    details={
                        "decision_id": decision.id,
                        "lease_owner": run.lease_owner,
                    },
                )
            if run.status not in {
                CreatorRunStatus.WAITING_HUMAN,
                CreatorRunStatus.RETRYING,
                CreatorRunStatus.RUNNING,
            }:
                raise CreatorDecisionConflictError(
                    f"Run {run.id} cannot resume from {run.status.value}",
                    details={"decision_id": decision.id, "run_id": run.id},
                )

            runtime_decision = RuntimeHumanDecision(
                decision_id=decision.id,
                interrupt_id=decision.interrupt_id,
                kind=decision.kind,
                action=decision.action,
                actor_id=decision.actor_id,
                selected_option_id=decision.selected_option_id,
                feedback=decision.feedback,
                edited_payload=decision.edited_payload,
                submitted_at=decision.submitted_at,
            )
            claimed_run = run.claim(
                worker_id=worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.run_lease_seconds),
            )
            if task.status in {
                CreatorTaskStatus.WAITING_HUMAN,
                CreatorTaskStatus.RETRYING,
            }:
                claimed_task = task.transition(
                    CreatorTaskStatus.RUNNING,
                    now=now,
                )
                await uow.tasks.save(claimed_task, expected_version=task.version)
            elif task.status == CreatorTaskStatus.RUNNING:
                claimed_task = task
            else:
                raise CreatorDecisionConflictError(
                    f"Task {task.id} cannot resume from {task.status.value}",
                    details={"decision_id": decision.id, "task_id": task.id},
                )
            await uow.runs.save(claimed_run, expected_version=run.version)
            await self._append_event(
                uow,
                task=claimed_task,
                run=claimed_run,
                event_type="decision.resume_started",
                payload={
                    "decision_id": decision.id,
                    "kind": decision.kind.value,
                    "worker_id": worker_id,
                },
                now=now,
            )
            await uow.commit()
            await self._remember_task(claimed_task, claimed_run)
            return _ClaimedDecision(
                task=claimed_task,
                run=claimed_run,
                decision=decision,
                runtime_decision=runtime_decision,
                request_hash=decision.submission_hash,
                scope=(
                    f"creator.decision.submit:{task.tenant_id}:"
                    f"{task.creator_id}:{task.id}:{decision.id}"
                ),
                key_hash=decision.idempotency_key_hash,
            )

    async def _invoke_runtime(
        self,
        request: RuntimeStartRequest,
        *,
        run_id: str,
        worker_id: str,
    ) -> RuntimeOutcome:
        try:
            raw_outcome = await self._runtime.start(
                request,
                on_events=lambda events: self._publish_runtime_events(
                    run_id=run_id,
                    worker_id=worker_id,
                    events=events,
                ),
            )
            try:
                outcome = RuntimeOutcome.model_validate(raw_outcome)
            except ValidationError as exc:
                raise CreatorRuntimeContractError(
                    "Runtime returned an invalid outcome"
                ) from exc
            self._validate_runtime_outcome(outcome)
            return outcome
        except CreatorRuntimeRetryableError as exc:
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.RETRYABLE_ERROR,
                error=RuntimeErrorInfo(
                    code=exc.error_code,
                    message=str(exc)[:4_000],
                    retryable=True,
                    details=exc.details,
                ),
            )
        except (CreatorRuntimeFatalError, CreatorRuntimeContractError) as exc:
            error_code = getattr(exc, "error_code", exc.code)
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.FAILED,
                error=RuntimeErrorInfo(
                    code=error_code,
                    message=str(exc)[:4_000],
                    retryable=False,
                    details=exc.details,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Unexpected creator runtime failure task_id=%s run_id=%s",
                request.task_id,
                request.run_id,
            )
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.RETRYABLE_ERROR,
                error=RuntimeErrorInfo(
                    code="RUNTIME_UNEXPECTED_ERROR",
                    message=f"Runtime raised {type(exc).__name__}",
                    retryable=True,
                ),
            )

    async def _invoke_runtime_resume(
        self,
        request: RuntimeResumeRequest,
        *,
        run_id: str,
        worker_id: str,
    ) -> RuntimeOutcome:
        try:
            raw_outcome = await self._runtime.resume(
                request,
                on_events=lambda events: self._publish_runtime_events(
                    run_id=run_id,
                    worker_id=worker_id,
                    events=events,
                ),
            )
            try:
                outcome = RuntimeOutcome.model_validate(raw_outcome)
            except ValidationError as exc:
                raise CreatorRuntimeContractError(
                    "Runtime returned an invalid resume outcome"
                ) from exc
            self._validate_runtime_outcome(outcome)
            if (
                outcome.status
                not in {
                    RuntimeOutcomeStatus.RETRYABLE_ERROR,
                }
                and outcome.applied_decision_id != request.decision.decision_id
            ):
                raise CreatorRuntimeContractError(
                    "Runtime did not confirm the submitted decision"
                )
            return outcome
        except CreatorRuntimeRetryableError as exc:
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.RETRYABLE_ERROR,
                error=RuntimeErrorInfo(
                    code=exc.error_code,
                    message=str(exc)[:4_000],
                    retryable=True,
                    details=exc.details,
                ),
            )
        except (CreatorRuntimeFatalError, CreatorRuntimeContractError) as exc:
            error_code = getattr(exc, "error_code", exc.code)
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.FAILED,
                error=RuntimeErrorInfo(
                    code=error_code,
                    message=str(exc)[:4_000],
                    retryable=False,
                    details=exc.details,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Unexpected creator runtime resume failure task_id=%s run_id=%s",
                request.task_id,
                request.run_id,
            )
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.RETRYABLE_ERROR,
                error=RuntimeErrorInfo(
                    code="RUNTIME_RESUME_UNEXPECTED_ERROR",
                    message=f"Runtime raised {type(exc).__name__}",
                    retryable=True,
                ),
            )

    async def _apply_runtime_outcome(
        self,
        *,
        run_id: str,
        worker_id: str,
        outcome: RuntimeOutcome,
        resume_context: _DecisionResumeContext | None = None,
    ) -> CreatorRunExecutionResult:
        now = self._clock()
        async with self._uow_factory() as uow:
            run = await self._require_run(uow, run_id, for_update=True)
            task = await self._require_task(uow, run.task_id, for_update=True)
            if run.lease_owner != worker_id or run.status != CreatorRunStatus.RUNNING:
                raise CreatorStaleWorkerResultError(
                    f"Worker {worker_id} no longer owns run {run.id}",
                    details={
                        "run_id": run.id,
                        "worker_id": worker_id,
                        "lease_owner": run.lease_owner,
                        "run_status": run.status.value,
                    },
                )

            resumed_decision: CreatorHumanDecision | None = None
            if resume_context is not None:
                resumed_decision = await uow.decisions.get(
                    resume_context.decision_id,
                    for_update=True,
                )
                if resumed_decision is None:
                    raise CreatorDecisionNotFoundError(
                        f"Creator decision {resume_context.decision_id} was not found",
                        details={"decision_id": resume_context.decision_id},
                    )
                if (
                    resumed_decision.status != CreatorDecisionStatus.SUBMITTED
                    or resumed_decision.submission_hash != resume_context.request_hash
                    or resumed_decision.idempotency_key_hash != resume_context.key_hash
                ):
                    raise CreatorDecisionConflictError(
                        f"Decision {resumed_decision.id} submission changed",
                        details={"decision_id": resumed_decision.id},
                    )

            for runtime_event in outcome.events:
                await self._append_event(
                    uow,
                    task=task,
                    run=run,
                    event_type=runtime_event.type,
                    payload=runtime_event.payload,
                    now=now,
                )

            status_payload: dict[str, Any]
            if task.cancel_requested:
                updated_task = task.transition(CreatorTaskStatus.CANCELLED, now=now)
                updated_run = run.transition(
                    CreatorRunStatus.CANCELLED,
                    now=now,
                    checkpoint_id=outcome.checkpoint_id,
                )
                status_event = "task.cancelled"
                status_payload = {"reason": "cancel_requested_during_runtime"}
            elif outcome.status == RuntimeOutcomeStatus.COMPLETED:
                updated_task = task.transition(
                    CreatorTaskStatus.COMPLETED,
                    now=now,
                    final_artifact_id=outcome.final_artifact_id,
                )
                updated_run = run.transition(
                    CreatorRunStatus.COMPLETED,
                    now=now,
                    checkpoint_id=outcome.checkpoint_id,
                )
                status_event = "task.completed"
                status_payload = {"final_artifact_id": outcome.final_artifact_id}
            elif outcome.status == RuntimeOutcomeStatus.WAITING_HUMAN:
                assert outcome.decision_request is not None
                updated_task = task.transition(
                    CreatorTaskStatus.WAITING_HUMAN,
                    now=now,
                    pending_decision_id=outcome.decision_request.decision_id,
                )
                updated_run = run.transition(
                    CreatorRunStatus.WAITING_HUMAN,
                    now=now,
                    checkpoint_id=outcome.checkpoint_id,
                    pending_decision_id=outcome.decision_request.decision_id,
                )
                await self._register_runtime_decision(
                    uow,
                    task=updated_task,
                    run=updated_run,
                    outcome=outcome,
                    now=now,
                )
                status_event = "decision.required"
                status_payload = {
                    "checkpoint_id": outcome.checkpoint_id,
                    "decision_id": outcome.decision_request.decision_id,
                    "interrupt_id": outcome.decision_request.interrupt_id,
                    "kind": outcome.decision_request.kind.value,
                }
            elif outcome.status == RuntimeOutcomeStatus.RETRYABLE_ERROR:
                assert outcome.error is not None
                if run.execution_attempts < self._policy.max_runtime_attempts:
                    updated_task = task.transition(
                        CreatorTaskStatus.RETRYING,
                        now=now,
                        error_code=outcome.error.code,
                        error_message=outcome.error.message,
                    )
                    updated_run = run.transition(
                        CreatorRunStatus.RETRYING,
                        now=now,
                        checkpoint_id=outcome.checkpoint_id,
                        error_code=outcome.error.code,
                        error_message=outcome.error.message,
                        retryable=True,
                    )
                    await uow.outbox.add(
                        self._run_outbox(
                            run=updated_run,
                            task=updated_task,
                            topic=(
                                "creator.decision.resume"
                                if resume_context is not None
                                else "creator.run.start"
                            ),
                            available_at=now
                            + timedelta(seconds=self._policy.retry_delay_seconds),
                            extra_payload=(
                                {"decision_id": resume_context.decision_id}
                                if resume_context is not None
                                else None
                            ),
                        )
                    )
                    status_event = "run.retrying"
                    status_payload = {
                        "error_code": outcome.error.code,
                        "execution_attempt": run.execution_attempts,
                    }
                else:
                    updated_task = task.transition(
                        CreatorTaskStatus.FAILED,
                        now=now,
                        error_code=outcome.error.code,
                        error_message=outcome.error.message,
                    )
                    updated_run = run.transition(
                        CreatorRunStatus.FAILED,
                        now=now,
                        checkpoint_id=outcome.checkpoint_id,
                        error_code=outcome.error.code,
                        error_message=outcome.error.message,
                        retryable=False,
                    )
                    status_event = "run.failed"
                    status_payload = {
                        "error_code": outcome.error.code,
                        "reason": "runtime_attempt_budget_exhausted",
                    }
            else:
                assert outcome.error is not None
                updated_task = task.transition(
                    CreatorTaskStatus.FAILED,
                    now=now,
                    error_code=outcome.error.code,
                    error_message=outcome.error.message,
                )
                updated_run = run.transition(
                    CreatorRunStatus.FAILED,
                    now=now,
                    checkpoint_id=outcome.checkpoint_id,
                    error_code=outcome.error.code,
                    error_message=outcome.error.message,
                    retryable=False,
                )
                status_event = "run.failed"
                status_payload = {"error_code": outcome.error.code}

            await uow.tasks.save(updated_task, expected_version=task.version)
            await uow.runs.save(updated_run, expected_version=run.version)
            decision_was_applied = (
                resumed_decision is not None
                and outcome.applied_decision_id == resumed_decision.id
            )
            if decision_was_applied:
                assert resumed_decision is not None
                applied_decision = resumed_decision.mark_applied(now=now)
                await uow.decisions.save(
                    applied_decision,
                    expected_version=resumed_decision.version,
                )
            await self._append_event(
                uow,
                task=updated_task,
                run=updated_run,
                event_type=status_event,
                payload={
                    **status_payload,
                    "state_summary": outcome.state_summary,
                },
                now=now,
            )
            if (
                decision_was_applied
                and resume_context is not None
                and resume_context.persist_idempotency
            ):
                decision_result = _decision_result(
                    updated_task,
                    updated_run,
                    applied_decision_id=resume_context.decision_id,
                )
                await uow.idempotency.add(
                    CreatorIdempotencyRecord(
                        id=self._new_id(),
                        scope=resume_context.scope,
                        key_hash=resume_context.key_hash,
                        request_hash=resume_context.request_hash,
                        response=decision_result.model_dump(mode="json"),
                        task_id=task.id,
                        created_at=now,
                        expires_at=now
                        + timedelta(seconds=self._policy.idempotency_ttl_seconds),
                    )
                )
            await uow.commit()
            await self._remember_task(updated_task, updated_run)
            return _execution_result(updated_task, updated_run, invoked=True)

    async def _remember_task(self, task: CreatorTask, run: CreatorRun) -> None:
        if self._task_memory is None:
            return
        try:
            await self._task_memory.remember_task(task, run)
        except Exception as exc:
            logger.warning(
                "Creator short memory projection failed task_id=%s run_id=%s "
                "error=%s",
                task.id,
                run.id,
                type(exc).__name__,
            )

    def _validate_runtime_outcome(self, outcome: RuntimeOutcome) -> None:
        if len(outcome.events) > self._policy.max_runtime_events:
            raise CreatorRuntimeContractError(
                f"Runtime emitted {len(outcome.events)} events; limit is {self._policy.max_runtime_events}"
            )
        payloads: list[dict[str, Any]] = [event.payload for event in outcome.events] + [
            outcome.state_summary
        ]
        if outcome.decision_request is not None:
            payloads.append(outcome.decision_request.model_dump(mode="json"))
        for payload in payloads:
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise CreatorRuntimeContractError(
                    f"Runtime payload is not JSON serializable: {type(exc).__name__}"
                ) from exc
            if len(encoded) > self._policy.max_event_payload_bytes:
                raise CreatorRuntimeContractError(
                    f"Runtime payload exceeds {self._policy.max_event_payload_bytes} bytes"
                )

    async def _register_runtime_decision(
        self,
        uow: CreatorUnitOfWork,
        *,
        task: CreatorTask,
        run: CreatorRun,
        outcome: RuntimeOutcome,
        now: datetime,
    ) -> None:
        request = outcome.decision_request
        if request is None or outcome.checkpoint_id is None:
            raise CreatorRuntimeContractError(
                "WAITING_HUMAN outcome lacks decision checkpoint data"
            )
        existing = await uow.decisions.get(request.decision_id)
        if existing is not None:
            expected = (
                task.id,
                run.id,
                outcome.checkpoint_id,
                request.interrupt_id,
                request.kind,
                request.source_artifact_id,
                request.allowed_actions,
                request.allowed_option_ids,
            )
            actual = (
                existing.task_id,
                existing.run_id,
                existing.checkpoint_id,
                existing.interrupt_id,
                existing.kind,
                existing.source_artifact_id,
                existing.allowed_actions,
                existing.allowed_option_ids,
            )
            if actual != expected or existing.status != CreatorDecisionStatus.PENDING:
                raise CreatorDecisionConflictError(
                    f"Decision request {request.decision_id} changed",
                    details={"decision_id": request.decision_id},
                )
            return
        await uow.decisions.add(
            CreatorHumanDecision(
                id=request.decision_id,
                task_id=task.id,
                run_id=run.id,
                checkpoint_id=outcome.checkpoint_id,
                interrupt_id=request.interrupt_id,
                kind=request.kind,
                prompt=request.prompt,
                source_artifact_id=request.source_artifact_id,
                allowed_actions=request.allowed_actions,
                allowed_option_ids=request.allowed_option_ids,
                status=CreatorDecisionStatus.PENDING,
                version=1,
                created_at=now,
            )
        )

    async def _append_event(
        self,
        uow: CreatorUnitOfWork,
        *,
        task: CreatorTask,
        run: CreatorRun,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        sequence = await uow.events.next_sequence(run.id)
        await uow.events.add(
            CreatorRunEvent(
                id=self._new_id(),
                task_id=task.id,
                run_id=run.id,
                sequence=sequence,
                type=event_type,
                payload=payload,
                trace_id=task.trace_id,
                created_at=now,
            )
        )

    async def _publish_runtime_events(
        self,
        *,
        run_id: str,
        worker_id: str,
        events: tuple[Any, ...],
    ) -> None:
        if not events:
            return
        now = self._clock()
        try:
            async with self._uow_factory() as uow:
                run = await self._require_run(uow, run_id, for_update=True)
                task = await self._require_task(uow, run.task_id, for_update=True)
                if (
                    run.lease_owner != worker_id
                    or run.status != CreatorRunStatus.RUNNING
                ):
                    return
                renewed = run.renew_lease(
                    worker_id=worker_id,
                    now=now,
                    lease_expires_at=now
                    + timedelta(seconds=self._policy.run_lease_seconds),
                )
                await uow.runs.save(renewed, expected_version=run.version)
                for runtime_event in events:
                    await self._append_event(
                        uow,
                        task=task,
                        run=renewed,
                        event_type=runtime_event.type,
                        payload=runtime_event.payload,
                        now=now,
                    )
                await uow.commit()
        except Exception:
            logger.exception(
                "Failed to publish mid-run creator events run_id=%s worker_id=%s",
                run_id,
                worker_id,
            )

    async def get_run_tenant_id(self, run_id: str) -> str:
        tenant_id, _ = await self.get_run_scope(run_id)
        return tenant_id

    async def get_run_scope(self, run_id: str) -> tuple[str, str]:
        async with self._uow_factory() as uow:
            run = await self._require_run(uow, run_id)
            task = await self._require_task(uow, run.task_id)
            return task.tenant_id, task.creator_id

    def _run_outbox(
        self,
        *,
        run: CreatorRun,
        task: CreatorTask,
        topic: str,
        available_at: datetime,
        extra_payload: dict[str, Any] | None = None,
    ) -> CreatorOutboxMessage:
        now = self._clock()
        return CreatorOutboxMessage(
            id=self._new_id(),
            aggregate_type="creator_run",
            aggregate_id=run.id,
            topic=topic,
            payload={
                "task_id": task.id,
                "run_id": run.id,
                "trace_id": task.trace_id,
                "tenant_id": task.tenant_id,
                "creator_id": task.creator_id,
                **(extra_payload or {}),
            },
            status=OutboxStatus.PENDING,
            available_at=available_at,
            created_at=now,
            updated_at=now,
        )

    async def _replay_after_create_race(
        self,
        scope: str,
        key_hash: str,
        request_hash: str,
    ) -> CreatorTaskResult:
        async with self._uow_factory() as uow:
            existing = await uow.idempotency.get(scope, key_hash)
            if existing is None:
                raise CreatorPersistenceConflictError(
                    "Persistence conflict was not caused by an idempotency race"
                )
            return _replay_idempotent(existing, request_hash)

    async def _require_task(
        self,
        uow: CreatorUnitOfWork,
        task_id: str,
        *,
        for_update: bool = False,
    ) -> CreatorTask:
        task = await uow.tasks.get(task_id, for_update=for_update)
        if task is None:
            raise CreatorTaskNotFoundError(
                f"Creator task {task_id} was not found",
                details={"task_id": task_id},
            )
        return task

    async def _require_run(
        self,
        uow: CreatorUnitOfWork,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> CreatorRun:
        run = await uow.runs.get(run_id, for_update=for_update)
        if run is None:
            raise CreatorRunNotFoundError(
                f"Creator run {run_id} was not found",
                details={"run_id": run_id},
            )
        return run

    @staticmethod
    def _require_scope(task: CreatorTask, tenant_id: str, creator_id: str) -> None:
        if task.tenant_id != tenant_id or task.creator_id != creator_id:
            raise CreatorScopeViolationError(
                f"Actor cannot access creator task {task.id}",
                details={"task_id": task.id},
            )


def _task_result(task: CreatorTask, *, replayed: bool = False) -> CreatorTaskResult:
    return CreatorTaskResult(
        task_id=task.id,
        run_id=task.active_run_id,
        status=task.status,
        version=task.version,
        trace_id=task.trace_id,
        pending_decision_id=task.pending_decision_id,
        replayed=replayed,
    )


def _execution_result(
    task: CreatorTask,
    run: CreatorRun,
    *,
    invoked: bool,
) -> CreatorRunExecutionResult:
    return CreatorRunExecutionResult(
        task_id=task.id,
        run_id=run.id,
        task_status=task.status,
        run_status=run.status,
        task_version=task.version,
        invoked=invoked,
        final_artifact_id=task.final_artifact_id,
        pending_decision_id=task.pending_decision_id,
        error_code=task.error_code,
    )


def _decision_result(
    task: CreatorTask,
    run: CreatorRun,
    *,
    applied_decision_id: str | None,
    replayed: bool = False,
) -> CreatorDecisionResult:
    return CreatorDecisionResult(
        task_id=task.id,
        run_id=run.id,
        task_status=task.status,
        run_status=run.status,
        task_version=task.version,
        final_artifact_id=task.final_artifact_id,
        pending_decision_id=task.pending_decision_id,
        applied_decision_id=applied_decision_id,
        replayed=replayed,
    )


def _replay_idempotent(
    record: CreatorIdempotencyRecord,
    request_hash: str,
) -> CreatorTaskResult:
    if record.request_hash != request_hash:
        raise CreatorIdempotencyConflictError(
            "Idempotency key was already used with a different request",
            details={"task_id": record.task_id},
        )
    return CreatorTaskResult.model_validate(record.response).model_copy(
        update={"replayed": True}
    )


def _replay_decision_idempotent(
    record: CreatorIdempotencyRecord,
    request_hash: str,
) -> CreatorDecisionResult:
    if record.request_hash != request_hash:
        raise CreatorIdempotencyConflictError(
            "Idempotency key was already used with a different decision",
            details={"task_id": record.task_id},
        )
    return CreatorDecisionResult.model_validate(record.response).model_copy(
        update={"replayed": True}
    )


def _create_idempotency_scope(command: CreateCreatorTaskCommand) -> str:
    return f"creator.task.create:{command.tenant_id}:{command.creator_id}"


def _retry_idempotency_scope(command: RetryCreatorTaskCommand) -> str:
    return (
        f"creator.task.retry:{command.tenant_id}:{command.creator_id}:{command.task_id}"
    )


def _decision_idempotency_scope(
    command: SubmitCreatorDecisionCommand,
) -> str:
    return (
        f"creator.decision.submit:{command.tenant_id}:"
        f"{command.creator_id}:{command.task_id}:{command.decision_id}"
    )


def _create_request_hash(command: CreateCreatorTaskCommand) -> str:
    payload = {
        "tenant_id": command.tenant_id,
        "creator_id": command.creator_id,
        "kind": command.kind.value,
        "goal": command.creator_goal().model_dump(mode="json"),
        "session_id": command.session_id,
    }
    return _canonical_hash(payload)


def _retry_request_hash(command: RetryCreatorTaskCommand) -> str:
    return _canonical_hash(
        {
            "tenant_id": command.tenant_id,
            "creator_id": command.creator_id,
            "task_id": command.task_id,
            "expected_version": command.expected_version,
        }
    )


def _decision_request_hash(command: SubmitCreatorDecisionCommand) -> str:
    return _canonical_hash(
        {
            "tenant_id": command.tenant_id,
            "creator_id": command.creator_id,
            "task_id": command.task_id,
            "decision_id": command.decision_id,
            "action": command.action.value,
            "actor_id": command.actor_id,
            "selected_option_id": command.selected_option_id,
            "feedback": command.feedback,
            "edited_payload": command.edited_payload,
        }
    )


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
