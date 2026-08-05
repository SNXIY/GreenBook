from __future__ import annotations

import asyncio
import faulthandler
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import ValidationError

from app.creator.agents.schemas import DraftDocument
from app.creator.domain.errors import (
    CreatorRuntimeContractError,
    CreatorRuntimeRetryableError,
)
from app.creator.domain.models import (
    CreatorTaskKind,
    RuntimeDecisionRequest,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
    RuntimeResumeRequest,
    RuntimeStartRequest,
)
from app.creator.runtime.artifacts import build_artifact
from app.creator.runtime.graph import CreatorRuntimeGraph
from app.creator.runtime.models import (
    ArtifactKind,
    ArtifactPayload,
    BudgetLimits,
    BudgetUsage,
    CreatorArtifact,
    CreatorGraphState,
    HumanInterruptPayload,
    RuntimeControlStatus,
    RunIdentity,
)
from app.creator.runtime.ports import CreatorArtifactStore


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class _EventCursor:
    plan_revisions: frozenset[int] = frozenset()
    execution_ids: frozenset[str] = frozenset()
    artifact_ids: frozenset[str] = frozenset()
    progress_keys: frozenset[str] = frozenset()


RuntimeEventPublisher = Callable[[tuple[RuntimeEvent, ...]], Awaitable[None]]


class LangGraphCreatorRuntime:
    name = "mindflow-creator-langgraph-v2"

    def __init__(
        self,
        *,
        graph: CreatorRuntimeGraph,
        artifact_store: CreatorArtifactStore,
        limits: BudgetLimits | None = None,
    ):
        self._graph = graph
        self._artifacts = artifact_store
        self._limits = limits or BudgetLimits()

    async def start(
        self,
        request: RuntimeStartRequest,
        *,
        on_events: RuntimeEventPublisher | None = None,
    ) -> RuntimeOutcome:
        identity = RunIdentity.from_request(request)
        config = self._config(request.thread_id)
        try:
            await self._seed_input_artifacts(identity, request)
            persisted = await self._artifacts.list_for_run(request.run_id)
            recovered = _terminal_recovery_outcome(persisted)
            if recovered is not None:
                return recovered
            snapshot = await self._graph.compiled.aget_state(config)
            if snapshot.values:
                interrupts = _snapshot_interrupts(snapshot)
                if interrupts:
                    state = _validate_graph_state(dict(snapshot.values))
                    return await self._interrupt_outcome(
                        state=state,
                        interrupt_value=interrupts[0],
                        checkpoint_id=_checkpoint_id(snapshot),
                    )
                previous = _event_cursor(dict(snapshot.values))
                return await self._invoke_streaming(
                    None,
                    config=config,
                    previous=previous,
                    on_events=on_events,
                )

            seed_refs = tuple(artifact.as_ref() for artifact in persisted)
            initial: CreatorGraphState = {
                "identity": identity,
                "goal": request.goal,
                "limits": self._limits,
                "usage": _recovered_budget_usage(request.kind, persisted),
                "plan": None,
                "plan_history": (),
                "executions": {},
                "artifacts": {ref.id: ref for ref in seed_refs},
                "facts": {},
                "progress": (),
                "errors": (),
                "decision": None,
                "control_status": RuntimeControlStatus.RUNNING,
                "final_artifact_id": None,
                "pending_decision_artifact_id": None,
                "applied_decision_id": None,
            }
            return await self._invoke_streaming(
                initial,
                config=config,
                previous=_EventCursor(),
                on_events=on_events,
            )
        except CreatorRuntimeContractError:
            raise
        except Exception as exc:
            logger.exception(
                "Creator LangGraph invocation failed task_id=%s run_id=%s",
                request.task_id,
                request.run_id,
            )
            raise CreatorRuntimeRetryableError(
                "Creator graph invocation failed",
                error_code="CREATOR_GRAPH_INVOCATION_FAILED",
                details={"exception": type(exc).__name__},
            ) from exc

    async def resume(
        self,
        request: RuntimeResumeRequest,
        *,
        on_events: RuntimeEventPublisher | None = None,
    ) -> RuntimeOutcome:
        config = self._config(request.thread_id)
        try:
            request_artifact = await self._artifacts.get(request.decision.decision_id)
            if (
                request_artifact is None
                or request_artifact.task_id != request.task_id
                or request_artifact.run_id != request.run_id
                or request_artifact.kind != ArtifactKind.DECISION_REQUEST
            ):
                raise CreatorRuntimeContractError(
                    "Decision request artifact does not belong to this run"
                )

            snapshot = await self._graph.compiled.aget_state(config)
            if not snapshot.values:
                raise CreatorRuntimeContractError(
                    "No checkpoint state exists for the creator thread"
                )
            state = _validate_graph_state(dict(snapshot.values))
            previous = _event_cursor(dict(snapshot.values))
            interrupts = _snapshot_interrupts(snapshot)
            active = next(
                (
                    item
                    for item in interrupts
                    if item.id == request.decision.interrupt_id
                ),
                None,
            )

            if active is not None:
                actual_checkpoint_id = _checkpoint_id(snapshot)
                if actual_checkpoint_id != request.checkpoint_id:
                    raise CreatorRuntimeContractError(
                        "Decision checkpoint changed before submission"
                    )
                return await self._invoke_streaming(
                    Command(
                        resume={
                            request.decision.interrupt_id: (
                                request.decision.model_dump(mode="json")
                            )
                        }
                    ),
                    config=config,
                    previous=previous,
                    on_events=on_events,
                    expected_applied_decision_id=request.decision.decision_id,
                )

            if state["applied_decision_id"] != request.decision.decision_id:
                raise CreatorRuntimeContractError(
                    "Decision interrupt is no longer active"
                )
            if interrupts:
                return await self._interrupt_outcome(
                    state=state,
                    interrupt_value=interrupts[0],
                    checkpoint_id=_checkpoint_id(snapshot),
                    previous=previous,
                    expected_applied_decision_id=request.decision.decision_id,
                )
            if snapshot.next:
                return await self._invoke_streaming(
                    None,
                    config=config,
                    previous=previous,
                    on_events=on_events,
                    expected_applied_decision_id=request.decision.decision_id,
                )
            return self._to_outcome(
                state,
                checkpoint_id=_checkpoint_id(snapshot),
                previous=previous,
                expected_applied_decision_id=request.decision.decision_id,
            )
        except CreatorRuntimeContractError:
            raise
        except Exception as exc:
            logger.exception(
                "Creator LangGraph resume failed task_id=%s run_id=%s decision_id=%s",
                request.task_id,
                request.run_id,
                request.decision.decision_id,
            )
            raise CreatorRuntimeRetryableError(
                "Creator graph resume failed",
                error_code="CREATOR_GRAPH_RESUME_FAILED",
                details={"exception": type(exc).__name__},
            ) from exc

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": f"creator:{thread_id}",
            },
            "recursion_limit": (
                self._limits.max_supervisor_turns
                + self._limits.max_agent_dispatches
                + 16
            ),
        }

    async def _invoke_streaming(
        self,
        graph_input: Any,
        *,
        config: dict[str, Any],
        previous: _EventCursor,
        on_events: RuntimeEventPublisher | None = None,
        expected_applied_decision_id: str | None = None,
    ) -> RuntimeOutcome:
        cursor = previous
        last_raw: dict[str, Any] = {}
        stack_timer = threading.Timer(10.0, faulthandler.dump_traceback)
        stack_timer.daemon = True
        stack_timer.start()
        async for chunk in self._graph.compiled.astream(
            graph_input,
            config=config,
            stream_mode="values",
        ):
            logger.info(
                "Creator graph chunk received run_id=%s keys=%s",
                config.get("configurable", {}).get("thread_id"),
                tuple(chunk) if isinstance(chunk, dict) else type(chunk).__name__,
            )
            if not isinstance(chunk, dict):
                continue
            last_raw = dict(chunk)
            try:
                state = _validate_graph_state(last_raw)
            except CreatorRuntimeContractError:
                continue
            if on_events is not None:
                events = _runtime_events(state, previous=cursor)
                if events:
                    try:
                        await asyncio.wait_for(on_events(events), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.exception(
                            "Creator runtime event publication timed out; continuing graph run_id=%s event_count=%s",
                            config.get("configurable", {}).get("thread_id"),
                            len(events),
                        )
                    cursor = _advance_event_cursor(cursor, state)
            logger.info(
                "Creator graph chunk processed run_id=%s artifact_count=%s progress_count=%s",
                config.get("configurable", {}).get("thread_id"),
                len(state.get("artifacts", {})),
                len(state.get("progress", ())),
            )
        stack_timer.cancel()
        logger.info(
            "Creator graph stream finished run_id=%s has_state=%s",
            config.get("configurable", {}).get("thread_id"),
            bool(last_raw),
        )
        if not last_raw:
            snapshot = await self._graph.compiled.aget_state(config)
            last_raw = dict(snapshot.values or {})
        return await self._outcome_after_invoke(
            last_raw,
            config=config,
            previous=cursor,
            expected_applied_decision_id=expected_applied_decision_id,
        )

    async def _outcome_after_invoke(
        self,
        raw_state: dict[str, Any],
        *,
        config: dict[str, Any],
        previous: "_EventCursor",
        expected_applied_decision_id: str | None = None,
    ) -> RuntimeOutcome:
        snapshot = await self._graph.compiled.aget_state(config)
        state = _validate_graph_state(dict(snapshot.values or raw_state))
        interrupts = _raw_interrupts(raw_state) or _snapshot_interrupts(snapshot)
        if interrupts:
            return await self._interrupt_outcome(
                state=state,
                interrupt_value=interrupts[0],
                checkpoint_id=_checkpoint_id(snapshot),
                previous=previous,
                expected_applied_decision_id=expected_applied_decision_id,
            )
        return self._to_outcome(
            state,
            checkpoint_id=_checkpoint_id(snapshot),
            previous=previous,
            expected_applied_decision_id=expected_applied_decision_id,
        )

    async def _interrupt_outcome(
        self,
        *,
        state: CreatorGraphState,
        interrupt_value: Interrupt,
        checkpoint_id: str,
        previous: "_EventCursor | None" = None,
        expected_applied_decision_id: str | None = None,
    ) -> RuntimeOutcome:
        payload = HumanInterruptPayload.model_validate(interrupt_value.value)
        artifact = await self._artifacts.get(payload.decision_id)
        if (
            artifact is None
            or artifact.kind != ArtifactKind.DECISION_REQUEST
            or artifact.run_id != state["identity"].run_id
        ):
            raise CreatorRuntimeContractError(
                "Interrupt references an invalid decision artifact"
            )
        decision_request = RuntimeDecisionRequest(
            decision_id=payload.decision_id,
            interrupt_id=interrupt_value.id,
            kind=payload.kind,
            prompt=payload.prompt,
            source_artifact_id=payload.source_artifact_id,
            allowed_actions=payload.allowed_actions,
            allowed_option_ids=payload.allowed_option_ids,
        )
        applied_id = state["applied_decision_id"]
        if (
            expected_applied_decision_id is not None
            and applied_id != expected_applied_decision_id
        ):
            raise CreatorRuntimeContractError(
                "Graph did not record the resumed human decision"
            )
        summary = _state_summary(
            state,
            pending_decision_id=payload.decision_id,
            checkpoint_resume_supported=True,
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.WAITING_HUMAN,
            checkpoint_id=checkpoint_id,
            decision_request=decision_request,
            applied_decision_id=applied_id,
            events=(
                *_runtime_events(state, previous=previous),
                RuntimeEvent(
                    type="decision.interrupted",
                    payload={
                        "decision_id": payload.decision_id,
                        "interrupt_id": interrupt_value.id,
                        "kind": payload.kind.value,
                    },
                ),
            ),
            state_summary=summary,
        )

    async def _seed_input_artifacts(
        self,
        identity: RunIdentity,
        request: RuntimeStartRequest,
    ) -> None:
        if request.kind != CreatorTaskKind.IMPROVE_DRAFT:
            return
        raw_draft = request.goal.constraints.get("draft")
        if raw_draft is None:
            return
        try:
            if isinstance(raw_draft, str):
                document = DraftDocument(
                    title=str(request.goal.constraints.get("title") or "Source draft"),
                    body_markdown=raw_draft,
                )
            else:
                document = DraftDocument.model_validate(raw_draft)
        except ValidationError as exc:
            raise CreatorRuntimeContractError(
                "constraints.draft is not a valid source draft"
            ) from exc
        artifact = build_artifact(
            identity=identity,
            step_id="runtime:input",
            producer="RuntimeInputAdapter",
            revision=1,
            payload=ArtifactPayload(
                kind=ArtifactKind.SOURCE_DRAFT,
                content=document.model_dump(mode="json"),
                metadata={"source": "task.constraints.draft"},
            ),
        )
        await self._artifacts.put(artifact)

    @staticmethod
    def _to_outcome(
        state: CreatorGraphState,
        *,
        checkpoint_id: str,
        previous: "_EventCursor | None" = None,
        expected_applied_decision_id: str | None = None,
    ) -> RuntimeOutcome:
        status = state["control_status"]
        events = _runtime_events(state, previous=previous)
        summary = _state_summary(
            state,
            pending_decision_id=state["pending_decision_artifact_id"],
            checkpoint_resume_supported=True,
        )
        applied_id = state["applied_decision_id"]
        if (
            expected_applied_decision_id is not None
            and applied_id != expected_applied_decision_id
        ):
            raise CreatorRuntimeContractError(
                "Graph did not record the resumed human decision"
            )
        if status == RuntimeControlStatus.COMPLETED:
            if state["final_artifact_id"] is None:
                raise CreatorRuntimeContractError(
                    "Completed graph did not produce a final artifact"
                )
            return RuntimeOutcome(
                status=RuntimeOutcomeStatus.COMPLETED,
                checkpoint_id=checkpoint_id,
                final_artifact_id=state["final_artifact_id"],
                applied_decision_id=applied_id,
                events=events,
                state_summary=summary,
            )
        if status == RuntimeControlStatus.RUNNING:
            raise CreatorRuntimeContractError(
                "Graph stopped without a terminal state or active interrupt"
            )
        failure = state["errors"][-1] if state["errors"] else None
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.FAILED,
            checkpoint_id=checkpoint_id,
            applied_decision_id=applied_id,
            error=RuntimeErrorInfo(
                code=failure.code if failure else "CREATOR_GRAPH_FAILED",
                message=failure.message if failure else "Creator graph failed",
                retryable=False,
                details={
                    "step_id": failure.step_id if failure else None,
                    "agent": failure.agent if failure else None,
                },
            ),
            events=events,
            state_summary=summary,
        )


def _validate_graph_state(raw_state: dict[str, Any]) -> CreatorGraphState:
    required = {
        "identity",
        "goal",
        "limits",
        "usage",
        "plan_history",
        "executions",
        "artifacts",
        "facts",
        "progress",
        "errors",
        "control_status",
        "applied_decision_id",
    }
    missing = required - raw_state.keys()
    if missing:
        raise CreatorRuntimeContractError(
            f"Creator graph state is missing keys: {sorted(missing)}"
        )
    normalized = dict(raw_state)
    try:
        normalized["control_status"] = RuntimeControlStatus(
            normalized["control_status"]
        )
    except (TypeError, ValueError) as exc:
        raise CreatorRuntimeContractError(
            "Creator graph state has an invalid control_status"
        ) from exc
    return cast(CreatorGraphState, normalized)


def _runtime_events(
    state: CreatorGraphState,
    *,
    previous: _EventCursor | None = None,
) -> tuple[RuntimeEvent, ...]:
    cursor = previous or _EventCursor()
    events: list[RuntimeEvent] = []
    for plan in state["plan_history"]:
        if plan.revision in cursor.plan_revisions:
            continue
        events.append(
            RuntimeEvent(
                type="supervisor.plan.created",
                payload={
                    "revision": plan.revision,
                    "reason": plan.reason,
                    "steps": [
                        {
                            "step_id": step.id,
                            "capability": step.capability.value,
                            "dependencies": step.dependencies,
                        }
                        for step in plan.steps
                    ],
                },
            )
        )
    for entry in state["progress"]:
        if entry.sequence_key in cursor.progress_keys:
            continue
        if entry.type in {
            "supervisor.plan.created",
            "agent.completed",
            "agent.failed",
            "artifact.created",
        }:
            continue
        events.append(
            RuntimeEvent(
                type=entry.type,
                payload={
                    "message": entry.message,
                    "step_id": entry.step_id,
                    "agent": entry.agent,
                    "sequence_key": entry.sequence_key,
                },
            )
        )
    for execution in state["executions"].values():
        if execution.id in cursor.execution_ids:
            continue
        events.append(
            RuntimeEvent(
                type=(
                    "agent.completed"
                    if execution.status.value == "SUCCEEDED"
                    else "agent.failed"
                ),
                payload={
                    "execution_id": execution.id,
                    "step_id": execution.step_id,
                    "agent": execution.agent,
                    "capability": execution.capability.value,
                    "artifact_ids": execution.artifact_ids,
                    "error_code": execution.error_code,
                },
            )
        )
    terminal_artifact_id = (
        state["final_artifact_id"] or state["pending_decision_artifact_id"]
    )
    if terminal_artifact_id and terminal_artifact_id not in cursor.artifact_ids:
        events.append(
            RuntimeEvent(
                type="artifact.created",
                payload={"artifact_id": terminal_artifact_id},
            )
        )
    return tuple(events)


def _advance_event_cursor(
    previous: _EventCursor,
    state: CreatorGraphState,
) -> _EventCursor:
    terminal_artifact_id = (
        state["final_artifact_id"] or state["pending_decision_artifact_id"]
    )
    artifact_ids = set(previous.artifact_ids)
    if terminal_artifact_id:
        artifact_ids.add(terminal_artifact_id)
    return replace(
        previous,
        plan_revisions=frozenset(plan.revision for plan in state["plan_history"]),
        execution_ids=frozenset(state["executions"]),
        artifact_ids=frozenset(artifact_ids),
        progress_keys=frozenset(
            entry.sequence_key for entry in state["progress"]
        ),
    )


def _terminal_recovery_outcome(
    artifacts: tuple[CreatorArtifact, ...],
) -> RuntimeOutcome | None:
    final_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.kind == ArtifactKind.FINAL_CONTENT
    ]
    if final_artifacts:
        final = max(
            final_artifacts,
            key=lambda artifact: (artifact.revision, artifact.created_at),
        )
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.COMPLETED,
            final_artifact_id=final.id,
            events=(
                RuntimeEvent(
                    type="artifact.recovered",
                    payload={"artifact_id": final.id, "kind": final.kind.value},
                ),
            ),
            state_summary={
                "control_status": RuntimeControlStatus.COMPLETED.value,
                "artifact_count": len(artifacts),
                "recovered_from_artifact_store": True,
                "checkpoint_resume_supported": True,
            },
        )
    return None


def _recovered_budget_usage(
    task_kind: CreatorTaskKind,
    artifacts: tuple[CreatorArtifact, ...],
) -> BudgetUsage:
    agent_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.producer not in {"RuntimeInputAdapter", "CreatorSupervisorAgent"}
    ]
    draft_count = sum(artifact.kind == ArtifactKind.DRAFT for artifact in artifacts)
    writer_revisions = (
        draft_count
        if task_kind == CreatorTaskKind.IMPROVE_DRAFT
        else max(0, draft_count - 1)
    )
    return BudgetUsage(
        agent_dispatches=len(agent_artifacts),
        model_calls=len(agent_artifacts),
        writer_revisions=writer_revisions,
    )


def _checkpoint_id(snapshot: StateSnapshot) -> str:
    configurable = snapshot.config.get("configurable", {}) if snapshot.config else {}
    checkpoint_id = configurable.get("checkpoint_id")
    if not checkpoint_id:
        raise CreatorRuntimeContractError(
            "LangGraph state does not expose a checkpoint ID"
        )
    return str(checkpoint_id)


def _snapshot_interrupts(snapshot: StateSnapshot) -> tuple[Interrupt, ...]:
    return tuple(
        interrupt_value
        for task in snapshot.tasks
        for interrupt_value in task.interrupts
    )


def _raw_interrupts(raw_state: dict[str, Any]) -> tuple[Interrupt, ...]:
    values = raw_state.get("__interrupt__", ())
    return tuple(value for value in values if isinstance(value, Interrupt))


def _event_cursor(raw_state: dict[str, Any]) -> _EventCursor:
    state = _validate_graph_state(raw_state)
    return _EventCursor(
        plan_revisions=frozenset(plan.revision for plan in state["plan_history"]),
        execution_ids=frozenset(state["executions"]),
        artifact_ids=frozenset(state["artifacts"]),
        progress_keys=frozenset(
            entry.sequence_key for entry in state["progress"]
        ),
    )


def _state_summary(
    state: CreatorGraphState,
    *,
    pending_decision_id: str | None,
    checkpoint_resume_supported: bool,
) -> dict[str, Any]:
    return {
        "control_status": state["control_status"].value,
        "plan_revision": (
            state["plan"].revision if state["plan"] is not None else None
        ),
        "plan_count": len(state["plan_history"]),
        "execution_count": len(state["executions"]),
        "artifact_count": len(state["artifacts"]),
        "usage": state["usage"].model_dump(mode="json"),
        "pending_decision_artifact_id": pending_decision_id,
        "applied_decision_id": state["applied_decision_id"],
        "checkpoint_resume_supported": checkpoint_resume_supported,
    }
