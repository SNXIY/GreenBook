from __future__ import annotations

import asyncio
import faulthandler
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send, interrupt

from pydantic import ValidationError

from app.creator.agents.schemas import (
    ContentOutlineDocument,
    DraftDocument,
    DraftSectionAnnotation,
    TopicOption,
    TopicOptionsDocument,
    TopicRecommendation,
)
from app.creator.agents.specialists import SpecialistAgentError
from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorGoal,
    RuntimeHumanDecision,
)
from app.creator.runtime.artifacts import (
    assert_complete_artifact_load,
    build_artifact,
    next_artifact_revision,
)
from app.creator.runtime.checkpoints import configure_creator_checkpointer
from app.creator.runtime.models import (
    AgentDispatchEnvelope,
    AgentExecutionContext,
    ArtifactKind,
    ArtifactPayload,
    ArtifactRef,
    BudgetUsage,
    CreatorArtifact,
    CreatorGraphState,
    FactRecord,
    HumanInterruptPayload,
    PlanStep,
    PlanStepStatus,
    ProgressEntry,
    RuntimeControlStatus,
    RuntimeFailure,
    StepExecution,
    SupervisorAction,
    utc_now,
)
from app.creator.runtime.ports import CreatorArtifactStore
from app.creator.runtime.registry import AgentRegistryError, CreatorAgentRegistry
from app.creator.runtime.supervisor import CreatorSupervisorAgent, execution_id


logger = logging.getLogger("uvicorn.error")


TerminalNode = Literal["await_human", "finalize", "fail"]


class CreatorRuntimeGraph:
    """Stable LangGraph control loop with dynamic capability-based fan-out."""

    def __init__(
        self,
        *,
        registry: CreatorAgentRegistry,
        supervisor: CreatorSupervisorAgent,
        artifact_store: CreatorArtifactStore,
        checkpointer: BaseCheckpointSaver,
        clock: Callable[[], datetime] | None = None,
        specialist_timeout_seconds: float = 90.0,
    ):
        self._registry = registry
        self._supervisor = supervisor
        self._artifacts = artifact_store
        self._checkpointer = configure_creator_checkpointer(checkpointer)
        self._clock = clock or utc_now
        self._specialist_timeout_seconds = max(1.0, float(specialist_timeout_seconds))
        self.compiled = self._build()

    def _build(self):
        builder = StateGraph(CreatorGraphState)
        builder.add_node("supervise", self._supervise)
        builder.add_node("execute_agent", self._execute_agent)
        builder.add_node("await_human", self._await_human)
        builder.add_node("finalize", self._finalize)
        builder.add_node("fail", self._fail)
        builder.add_edge(START, "supervise")
        builder.add_conditional_edges("supervise", self._route_supervisor)
        builder.add_edge("execute_agent", "supervise")
        builder.add_edge("await_human", "supervise")
        builder.add_edge("finalize", END)
        builder.add_edge("fail", END)
        return builder.compile(checkpointer=self._checkpointer)

    async def _supervise(self, state: CreatorGraphState) -> dict[str, Any]:
        identity = state["identity"]
        started = time.monotonic()
        logger.info(
            "graph_node_started node_name=supervise task_id=%s graph_thread_id=%s event_loop_id=%s thread_id=%s",
            identity.task_id, identity.thread_id, id(asyncio.get_running_loop()), threading.get_ident(),
        )
        faulthandler.dump_traceback_later(10, repeat=False)
        try:
            turn = self._supervisor.decide(state)
        finally:
            faulthandler.cancel_dump_traceback_later()
        update: dict[str, Any] = {
            "decision": turn.decision,
            "usage": turn.usage_delta,
            "progress": turn.progress,
        }
        if turn.plan_is_new:
            assert turn.plan is not None
            update["plan"] = turn.plan
            update["plan_history"] = (turn.plan,)
        logger.info(
            "graph_node_returned node_name=supervise task_id=%s graph_thread_id=%s returned_keys=%s duration_ms=%.1f",
            identity.task_id, identity.thread_id, tuple(update), (time.monotonic() - started) * 1000,
        )
        return update

    def _route_supervisor(
        self,
        state: CreatorGraphState,
    ) -> list[Send] | TerminalNode:
        decision = state["decision"]
        if decision is None:
            return "fail"
        if decision.action == SupervisorAction.REQUEST_HUMAN:
            return "await_human"
        if decision.action == SupervisorAction.FINISH:
            return "finalize"
        if decision.action == SupervisorAction.FAIL:
            return "fail"

        plan = state["plan"]
        if plan is None:
            return "fail"
        step_by_id = {step.id: step for step in plan.steps}
        refs = tuple(state["artifacts"].values())
        facts = tuple(state["facts"].values())
        sends: list[Send] = []
        for step_id in decision.dispatch_step_ids:
            step = step_by_id.get(step_id)
            if step is None:
                return "fail"
            selected_refs = _select_input_refs(refs, step)
            sends.append(
                Send(
                    "execute_agent",
                    AgentDispatchEnvelope(
                        identity=state["identity"],
                        goal=state["goal"],
                        plan_revision=plan.revision,
                        step=step,
                        artifact_refs=selected_refs,
                        facts=facts,
                    ),
                )
            )
        return sends

    async def _execute_agent(
        self,
        envelope: AgentDispatchEnvelope,
    ) -> dict[str, Any]:
        started_at = self._clock()
        step = envelope["step"]
        plan_revision = envelope["plan_revision"]
        run_identity = envelope["identity"]
        execution_key = execution_id(plan_revision, step)
        agent_name = "unresolved"
        logger.info(
            "graph_node_started node_name=execute_agent task_id=%s graph_thread_id=%s node_name=%s event_loop_id=%s thread_id=%s",
            run_identity.task_id, run_identity.thread_id, step.id, id(asyncio.get_running_loop()), threading.get_ident(),
        )
        try:
            agent = self._registry.resolve(step.capability)
            agent_name = agent.descriptor.name
            requested_ids = tuple(ref.id for ref in envelope["artifact_refs"])
            input_artifacts = await self._artifacts.get_many(requested_ids)
            assert_complete_artifact_load(requested_ids, input_artifacts)
            try:
                result = await asyncio.wait_for(
                    agent.execute(
                        AgentExecutionContext(
                            identity=run_identity,
                            goal=envelope["goal"],
                            plan_revision=plan_revision,
                            step=step,
                            artifacts=input_artifacts,
                            facts=envelope["facts"],
                        )
                    ),
                    timeout=self._specialist_timeout_seconds,
                )
            except TimeoutError:
                return self._failed_execution_update(
                    step=step,
                    plan_revision=plan_revision,
                    execution_key=execution_key,
                    agent_name=agent_name,
                    started_at=started_at,
                    code="SPECIALIST_TIMEOUT",
                    message=(
                        f"{agent_name} exceeded "
                        f"{self._specialist_timeout_seconds:.0f}s step timeout"
                    ),
                    retryable=True,
                )
            if not result.artifacts:
                raise SpecialistAgentError(
                    "AGENT_OUTPUT_MISSING",
                    f"{agent_name} returned no artifacts",
                    retryable=True,
                )
            if not any(
                payload.kind == step.output_kind for payload in result.artifacts
            ):
                raise SpecialistAgentError(
                    "AGENT_OUTPUT_CONTRACT_ERROR",
                    (
                        f"{agent_name} did not produce required artifact "
                        f"{step.output_kind.value}"
                    ),
                    retryable=True,
                )

            run_artifacts = await self._artifacts.list_for_run(run_identity.run_id)
            existing_refs = [artifact.as_ref() for artifact in run_artifacts]
            produced = []
            for payload in result.artifacts:
                revision = next_artifact_revision(existing_refs, payload.kind)
                artifact = build_artifact(
                    identity=run_identity,
                    step_id=execution_key,
                    producer=agent_name,
                    revision=revision,
                    payload=payload,
                    created_at=self._clock(),
                )
                persist_started = time.monotonic()
                logger.info(
                    "artifact_persist_started task_id=%s graph_thread_id=%s artifact_id=%s node_name=%s",
                    run_identity.task_id, run_identity.thread_id, artifact.id, step.id,
                )
                await self._artifacts.put(artifact)
                logger.info(
                    "artifact_persist_finished task_id=%s graph_thread_id=%s artifact_id=%s duration_ms=%.1f",
                    run_identity.task_id, run_identity.thread_id, artifact.id, (time.monotonic() - persist_started) * 1000,
                )
                produced.append(artifact)
                existing_refs.append(artifact.as_ref())

            primary_id = next(
                artifact.id
                for artifact in produced
                if artifact.kind == step.output_kind
            )
            facts = {
                fact.id: fact
                for fact in (
                    _build_fact(
                        execution_key=execution_key,
                        index=index,
                        source_artifact_id=primary_id,
                        key=draft.key,
                        value=draft.value,
                        confidence=draft.confidence,
                        created_at=self._clock(),
                    )
                    for index, draft in enumerate(result.facts)
                )
            }
            finished_at = self._clock()
            execution = StepExecution(
                id=execution_key,
                plan_revision=plan_revision,
                step_id=step.id,
                capability=step.capability,
                agent=agent_name,
                status=PlanStepStatus.SUCCEEDED,
                attempt=step.attempt,
                artifact_ids=tuple(artifact.id for artifact in produced),
                usage=result.usage,
                started_at=started_at,
                finished_at=finished_at,
            )
            update = {
                "executions": {execution_key: execution},
                "artifacts": {artifact.id: artifact.as_ref() for artifact in produced},
                "facts": facts,
                "usage": BudgetUsage(
                    agent_dispatches=1,
                    model_calls=result.usage.model_calls,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    tool_calls=result.usage.tool_calls,
                ),
                "progress": (
                    ProgressEntry(
                        sequence_key=f"execution:{execution_key}",
                        type="agent.completed",
                        message=result.summary,
                        step_id=step.id,
                        agent=agent_name,
                        created_at=finished_at,
                    ),
                ),
            }
            logger.info(
                "graph_node_returned node_name=execute_agent task_id=%s graph_thread_id=%s node_name=%s returned_keys=%s",
                run_identity.task_id, run_identity.thread_id, step.id, tuple(update),
            )
            return update
        except SpecialistAgentError as exc:
            return self._failed_execution_update(
                step=step,
                plan_revision=plan_revision,
                execution_key=execution_key,
                agent_name=agent_name,
                started_at=started_at,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        except AgentRegistryError as exc:
            return self._failed_execution_update(
                step=step,
                plan_revision=plan_revision,
                execution_key=execution_key,
                agent_name=agent_name,
                started_at=started_at,
                code="CAPABILITY_UNAVAILABLE",
                message=str(exc),
                retryable=False,
            )
        except Exception as exc:
            logger.exception(
                "Unexpected specialist failure run_id=%s step_id=%s agent=%s",
                run_identity.run_id,
                step.id,
                agent_name,
            )
            return self._failed_execution_update(
                step=step,
                plan_revision=plan_revision,
                execution_key=execution_key,
                agent_name=agent_name,
                started_at=started_at,
                code="SPECIALIST_UNEXPECTED_ERROR",
                message=f"{type(exc).__name__}: {str(exc)[:1_000]}",
                retryable=True,
            )

    def _failed_execution_update(
        self,
        *,
        step: PlanStep,
        plan_revision: int,
        execution_key: str,
        agent_name: str,
        started_at: datetime,
        code: str,
        message: str,
        retryable: bool,
    ) -> dict[str, Any]:
        finished_at = self._clock()
        execution = StepExecution(
            id=execution_key,
            plan_revision=plan_revision,
            step_id=step.id,
            capability=step.capability,
            agent=agent_name,
            status=PlanStepStatus.FAILED,
            attempt=step.attempt,
            error_code=code,
            error_message=message[:4_000],
            retryable=retryable,
            started_at=started_at,
            finished_at=finished_at,
        )
        failure = RuntimeFailure(
            code=code,
            message=message[:4_000],
            retryable=retryable,
            step_id=step.id,
            agent=agent_name,
            created_at=finished_at,
        )
        return {
            "executions": {execution_key: execution},
            "usage": BudgetUsage(agent_dispatches=1),
            "errors": (failure,),
            "progress": (
                ProgressEntry(
                    sequence_key=f"execution:{execution_key}:failed",
                    type="agent.failed",
                    message=message[:2_000],
                    step_id=step.id,
                    agent=agent_name,
                    created_at=finished_at,
                ),
            ),
        }

    async def _await_human(self, state: CreatorGraphState) -> dict[str, Any]:
        decision = state["decision"]
        assert decision is not None and decision.human_request is not None
        request = decision.human_request
        source = await self._artifacts.get(request.source_artifact_id)
        if source is None:
            failure = RuntimeFailure(
                code="HUMAN_DECISION_SOURCE_MISSING",
                message="Decision source artifact could not be loaded",
            )
            return {
                "control_status": RuntimeControlStatus.FAILED,
                "errors": (failure,),
            }

        options: list[dict[str, Any]] = []
        allowed_option_ids = request.allowed_option_ids
        if source.kind == ArtifactKind.TOPIC_OPTIONS:
            topics = TopicOptionsDocument.model_validate(source.content)
            options = [option.model_dump(mode="json") for option in topics.options]
            allowed_option_ids = tuple(option.id for option in topics.options)

        payload = ArtifactPayload(
            kind=ArtifactKind.DECISION_REQUEST,
            content={
                "decision_kind": request.kind.value,
                "prompt": request.prompt,
                "source_artifact_id": source.id,
                "allowed_actions": [action.value for action in request.allowed_actions],
                "allowed_option_ids": list(allowed_option_ids),
                "options": options,
                "status": "PENDING",
            },
            parent_ids=(source.id,),
            metadata={
                "decision_kind": request.kind.value,
                "status": "PENDING",
            },
        )
        artifact = build_artifact(
            identity=state["identity"],
            step_id=f"human:{request.kind.value.lower()}",
            producer=self._supervisor.name,
            revision=next_artifact_revision(
                state["artifacts"].values(),
                ArtifactKind.DECISION_REQUEST,
            ),
            payload=payload,
            created_at=self._clock(),
        )
        await self._artifacts.put(artifact)

        resume_payload = interrupt(
            HumanInterruptPayload(
                decision_id=artifact.id,
                kind=request.kind,
                prompt=request.prompt,
                source_artifact_id=source.id,
                allowed_actions=request.allowed_actions,
                allowed_option_ids=allowed_option_ids,
                options=tuple(options),
            ).model_dump(mode="json")
        )
        human_decision = RuntimeHumanDecision.model_validate(resume_payload)
        _validate_human_decision(
            human_decision,
            request_id=artifact.id,
            kind=request.kind,
            allowed_actions=request.allowed_actions,
            allowed_option_ids=allowed_option_ids,
        )
        edited_artifact: CreatorArtifact | None = None
        if human_decision.action == CreatorDecisionAction.EDIT:
            try:
                if (
                    human_decision.kind == CreatorDecisionKind.DRAFT_REVIEW
                    and _draft_edit_is_annotations_only(human_decision.edited_payload)
                ):
                    edited_artifact = None
                else:
                    edited_artifact = _build_human_edited_artifact(
                        identity=state["identity"],
                        source=source,
                        decision=human_decision,
                        artifact_refs=state["artifacts"].values(),
                        created_at=self._clock(),
                    )
            except (ValidationError, ValueError) as exc:
                failure = RuntimeFailure(
                    code="HUMAN_EDIT_INVALID",
                    message=str(exc)[:4_000],
                    retryable=False,
                )
                return {
                    "control_status": RuntimeControlStatus.FAILED,
                    "errors": (failure,),
                }
            if edited_artifact is not None:
                await self._artifacts.put(edited_artifact)
        updated_goal = _apply_human_decision(
            state["goal"],
            human_decision,
            source_artifact_id=source.id,
            edited_artifact_id=(
                edited_artifact.id if edited_artifact is not None else None
            ),
        )
        decision_parents = (artifact.id, source.id)
        if edited_artifact is not None:
            decision_parents = (*decision_parents, edited_artifact.id)
        decision_artifact = build_artifact(
            identity=state["identity"],
            step_id=f"human-decision:{artifact.id}",
            producer="Human",
            revision=next_artifact_revision(
                state["artifacts"].values(),
                ArtifactKind.HUMAN_DECISION,
            ),
            payload=ArtifactPayload(
                kind=ArtifactKind.HUMAN_DECISION,
                content=human_decision.model_dump(mode="json"),
                parent_ids=decision_parents,
                metadata={
                    "decision_kind": human_decision.kind.value,
                    "action": human_decision.action.value,
                    "actor_id": human_decision.actor_id,
                    "edited_artifact_id": (
                        edited_artifact.id if edited_artifact is not None else None
                    ),
                },
            ),
            created_at=self._clock(),
        )
        await self._artifacts.put(decision_artifact)
        artifact_updates = {
            artifact.id: artifact.as_ref(),
            decision_artifact.id: decision_artifact.as_ref(),
        }
        if edited_artifact is not None:
            artifact_updates[edited_artifact.id] = edited_artifact.as_ref()
        return {
            "goal": updated_goal,
            "artifacts": artifact_updates,
            "pending_decision_artifact_id": None,
            "applied_decision_id": artifact.id,
            "control_status": RuntimeControlStatus.RUNNING,
            "progress": (
                ProgressEntry(
                    sequence_key=f"decision-applied:{artifact.id}",
                    type="decision.applied",
                    message=(
                        f"Human decision {human_decision.action.value} was applied."
                    ),
                    created_at=self._clock(),
                ),
            ),
        }

    async def _finalize(self, state: CreatorGraphState) -> dict[str, Any]:
        decision = state["decision"]
        assert decision is not None and decision.final_source_artifact_id is not None
        source = await self._artifacts.get(decision.final_source_artifact_id)
        if source is None:
            failure = RuntimeFailure(
                code="FINAL_SOURCE_MISSING",
                message="Final source artifact could not be loaded",
            )
            return {
                "control_status": RuntimeControlStatus.FAILED,
                "errors": (failure,),
            }
        related = tuple(
            ref.id
            for ref in state["artifacts"].values()
            if ref.kind
            in {
                ArtifactKind.CRITIQUE,
                ArtifactKind.EVALUATION_REPORT,
                ArtifactKind.HUMAN_DECISION,
            }
        )
        artifact = build_artifact(
            identity=state["identity"],
            step_id="runtime:finalize",
            producer=self._supervisor.name,
            revision=next_artifact_revision(
                state["artifacts"].values(),
                ArtifactKind.FINAL_CONTENT,
            ),
            payload=ArtifactPayload(
                kind=ArtifactKind.FINAL_CONTENT,
                content={
                    "source_artifact_id": source.id,
                    "source_kind": source.kind.value,
                    "document": source.content,
                },
                parent_ids=(source.id, *related),
                metadata={
                    "source_kind": source.kind.value,
                    "reviewed": any(
                        ref.kind == ArtifactKind.CRITIQUE
                        for ref in state["artifacts"].values()
                    ),
                },
                confidence=source.confidence,
            ),
            created_at=self._clock(),
        )
        await self._artifacts.put(artifact)
        return {
            "artifacts": {artifact.id: artifact.as_ref()},
            "final_artifact_id": artifact.id,
            "control_status": RuntimeControlStatus.COMPLETED,
            "progress": (
                ProgressEntry(
                    sequence_key=f"final:{artifact.id}",
                    type="artifact.finalized",
                    message="Final immutable artifact created.",
                    created_at=self._clock(),
                ),
            ),
        }

    async def _fail(self, state: CreatorGraphState) -> dict[str, Any]:
        decision = state["decision"]
        failure = (
            decision.failure
            if decision is not None and decision.failure is not None
            else RuntimeFailure(
                code="SUPERVISOR_ROUTING_ERROR",
                message="Supervisor did not emit a valid terminal route",
            )
        )
        return {
            "control_status": RuntimeControlStatus.FAILED,
            "errors": (failure,),
        }


def _select_input_refs(
    refs: tuple[ArtifactRef, ...],
    step: PlanStep,
) -> tuple[ArtifactRef, ...]:
    selected: list[ArtifactRef] = []
    for kind in step.input_kinds:
        matches = [ref for ref in refs if ref.kind == kind]
        latest = max(
            matches,
            key=lambda ref: (ref.revision, ref.created_at),
            default=None,
        )
        if latest is not None:
            selected.append(latest)
    by_id: dict[str, ArtifactRef] = {}
    for ref in selected:
        by_id[ref.id] = ref
    return tuple(by_id.values())


def _build_fact(
    *,
    execution_key: str,
    index: int,
    source_artifact_id: str,
    key: str,
    value: Any,
    confidence: float,
    created_at: datetime,
) -> FactRecord:
    seed = f"{execution_key}:{index}:{key}:{source_artifact_id}"
    fact_id = f"fact_{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"
    return FactRecord(
        id=fact_id,
        key=key,
        value=value,
        source_artifact_id=source_artifact_id,
        confidence=confidence,
        created_at=created_at,
    )


def _validate_human_decision(
    decision: RuntimeHumanDecision,
    *,
    request_id: str,
    kind: CreatorDecisionKind,
    allowed_actions: tuple[CreatorDecisionAction, ...],
    allowed_option_ids: tuple[str, ...],
) -> None:
    if decision.decision_id != request_id or decision.kind != kind:
        raise ValueError("Human decision does not match the active request")
    if decision.action not in allowed_actions:
        raise ValueError(
            f"Action {decision.action.value} is not allowed for {kind.value}"
        )
    if (
        decision.selected_option_id is not None
        and decision.selected_option_id not in allowed_option_ids
    ):
        raise ValueError(
            f"Option {decision.selected_option_id} is not allowed for {request_id}"
        )
    if decision.action == CreatorDecisionAction.EDIT:
        if kind == CreatorDecisionKind.TOPIC_SELECTION and not decision.selected_option_id:
            raise ValueError("EDIT topic selection requires selected_option_id")
        if not isinstance(decision.edited_payload, dict) or not decision.edited_payload:
            raise ValueError("EDIT requires edited_payload")
        if kind == CreatorDecisionKind.DRAFT_REVIEW:
            _validate_draft_edit_payload(decision.edited_payload)


def _draft_edit_is_annotations_only(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    has_document = isinstance(payload.get("document"), dict)
    annotations = payload.get("annotations")
    has_annotations = isinstance(annotations, list) and bool(annotations)
    return has_annotations and not has_document


def _validate_draft_edit_payload(payload: dict) -> None:
    document = payload.get("document")
    annotations = payload.get("annotations")
    has_document = isinstance(document, dict)
    has_annotations = isinstance(annotations, list) and bool(annotations)
    if not has_document and not has_annotations:
        raise ValueError(
            "Draft EDIT requires edited_payload.document and/or annotations"
        )
    if has_annotations:
        for item in annotations:
            DraftSectionAnnotation.model_validate(item)


def _apply_human_decision(
    goal: CreatorGoal,
    decision: RuntimeHumanDecision,
    *,
    source_artifact_id: str,
    edited_artifact_id: str | None = None,
) -> CreatorGoal:
    constraints = dict(goal.constraints)
    if decision.kind == CreatorDecisionKind.TOPIC_SELECTION:
        if decision.action in {
            CreatorDecisionAction.SELECT,
            CreatorDecisionAction.EDIT,
        }:
            constraints["selected_topic_id"] = decision.selected_option_id
            constraints.pop("topic_revision_requested_from", None)
            constraints.pop("topic_feedback", None)
            constraints.pop("outline_approved", None)
            constraints.pop("outline_revision_requested_from", None)
            constraints.pop("outline_feedback", None)
            if decision.action == CreatorDecisionAction.EDIT and edited_artifact_id:
                constraints["human_edited_topic_artifact_id"] = edited_artifact_id
        else:
            constraints.pop("selected_topic_id", None)
            constraints["topic_revision_requested_from"] = source_artifact_id
            constraints["topic_feedback"] = decision.feedback
    elif decision.kind == CreatorDecisionKind.OUTLINE_APPROVAL:
        if decision.action in {
            CreatorDecisionAction.APPROVE,
            CreatorDecisionAction.EDIT,
        }:
            constraints["outline_approved"] = True
            constraints.pop("outline_revision_requested_from", None)
            constraints.pop("outline_feedback", None)
            if decision.action == CreatorDecisionAction.EDIT and edited_artifact_id:
                constraints["human_edited_outline_artifact_id"] = edited_artifact_id
        else:
            constraints["outline_approved"] = False
            constraints["outline_revision_requested_from"] = source_artifact_id
            constraints["outline_feedback"] = decision.feedback
    elif decision.kind == CreatorDecisionKind.DRAFT_REVIEW:
        if decision.action == CreatorDecisionAction.APPROVE:
            constraints["draft_approved_artifact_id"] = source_artifact_id
            constraints.pop("draft_revision_requested_from", None)
            constraints.pop("draft_feedback", None)
            constraints.pop("draft_annotations", None)
            constraints.pop("draft_auto_approve_next", None)
        elif decision.action == CreatorDecisionAction.EDIT:
            payload = decision.edited_payload or {}
            if edited_artifact_id:
                constraints["draft_approved_artifact_id"] = edited_artifact_id
                constraints["human_edited_draft_artifact_id"] = edited_artifact_id
                constraints.pop("draft_revision_requested_from", None)
                constraints.pop("draft_feedback", None)
                constraints.pop("draft_annotations", None)
                constraints.pop("draft_auto_approve_next", None)
            else:
                annotations = payload.get("annotations") or []
                constraints["draft_annotations"] = list(annotations)
                constraints["draft_revision_requested_from"] = source_artifact_id
                constraints["draft_auto_approve_next"] = True
                constraints.pop("draft_approved_artifact_id", None)
                constraints.pop("draft_feedback", None)
        else:
            constraints["draft_revision_requested_from"] = source_artifact_id
            constraints["draft_feedback"] = decision.feedback
            constraints["draft_auto_approve_next"] = True
            constraints.pop("draft_approved_artifact_id", None)
            constraints.pop("draft_annotations", None)
    return goal.model_copy(update={"constraints": constraints})


def _build_human_edited_artifact(
    *,
    identity,
    source: CreatorArtifact,
    decision: RuntimeHumanDecision,
    artifact_refs,
    created_at: datetime,
) -> CreatorArtifact:
    payload = decision.edited_payload or {}
    if decision.kind == CreatorDecisionKind.TOPIC_SELECTION:
        if source.kind != ArtifactKind.TOPIC_OPTIONS:
            raise ValueError("Topic EDIT requires a TOPIC_OPTIONS source artifact")
        selected_id = str(decision.selected_option_id or "")
        raw_option = payload.get("option")
        if not isinstance(raw_option, dict):
            raise ValueError("Topic EDIT edited_payload.option must be an object")
        topics = TopicOptionsDocument.model_validate(source.content)
        edited_option = TopicOption.model_validate({**raw_option, "id": selected_id})
        if edited_option.recommendation == TopicRecommendation.SKIP:
            raise ValueError("Edited selected topic cannot be SKIP")
        merged = []
        replaced = False
        for option in topics.options:
            if option.id == selected_id:
                merged.append(edited_option)
                replaced = True
            else:
                merged.append(option)
        if not replaced:
            raise ValueError(f"Selected topic {selected_id!r} was not found")
        document = TopicOptionsDocument(
            options=tuple(merged),
            recommended_option_id=selected_id,
            recommendation_reason=(
                "Human-edited topic selected as the working direction."
            ),
        )
        return build_artifact(
            identity=identity,
            step_id=f"human-edit:topic:{decision.decision_id}",
            producer="Human",
            revision=next_artifact_revision(artifact_refs, ArtifactKind.TOPIC_OPTIONS),
            payload=ArtifactPayload(
                kind=ArtifactKind.TOPIC_OPTIONS,
                content=document.model_dump(mode="json"),
                parent_ids=(source.id,),
                metadata={
                    "human_edited": True,
                    "selected_option_id": selected_id,
                    "source_artifact_id": source.id,
                },
                confidence=1.0,
            ),
            created_at=created_at,
        )

    if decision.kind == CreatorDecisionKind.OUTLINE_APPROVAL:
        if source.kind != ArtifactKind.CONTENT_OUTLINE:
            raise ValueError("Outline EDIT requires a CONTENT_OUTLINE source artifact")
        raw_outline = payload.get("outline")
        if not isinstance(raw_outline, dict):
            raise ValueError("Outline EDIT edited_payload.outline must be an object")
        document = ContentOutlineDocument.model_validate(raw_outline)
        return build_artifact(
            identity=identity,
            step_id=f"human-edit:outline:{decision.decision_id}",
            producer="Human",
            revision=next_artifact_revision(
                artifact_refs, ArtifactKind.CONTENT_OUTLINE
            ),
            payload=ArtifactPayload(
                kind=ArtifactKind.CONTENT_OUTLINE,
                content=document.model_dump(mode="json"),
                parent_ids=(source.id,),
                metadata={
                    "human_edited": True,
                    "source_artifact_id": source.id,
                },
                confidence=1.0,
            ),
            created_at=created_at,
        )

    if decision.kind == CreatorDecisionKind.DRAFT_REVIEW:
        if source.kind != ArtifactKind.DRAFT:
            raise ValueError("Draft EDIT requires a DRAFT source artifact")
        raw_document = payload.get("document")
        if not isinstance(raw_document, dict):
            raise ValueError("Draft EDIT edited_payload.document must be an object")
        document = DraftDocument.model_validate(raw_document)
        return build_artifact(
            identity=identity,
            step_id=f"human-edit:draft:{decision.decision_id}",
            producer="Human",
            revision=next_artifact_revision(artifact_refs, ArtifactKind.DRAFT),
            payload=ArtifactPayload(
                kind=ArtifactKind.DRAFT,
                content=document.model_dump(mode="json"),
                parent_ids=(source.id,),
                metadata={
                    "human_edited": True,
                    "source_artifact_id": source.id,
                },
                confidence=1.0,
            ),
            created_at=created_at,
        )

    raise ValueError(f"EDIT is not supported for {decision.kind.value}")
