"""Canonical conversation-to-Agent Runtime composition boundary.

The adapter owns request scope and projections only.  User input enters as a
typed ``Command``, is decomposed into a ``GoalTree``, and is then handed to
``AgentLoop``.  Durable Task and Execution state stay in their respective
repositories; this module never interprets natural language or runs a tool
directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.agent import AgentLoop, AgentRunResult
from greenbook_agent_core.agent.actions import AgentAction, AgentActionType
from greenbook_agent_core.agent.recovery import ResumeContext
from greenbook_agent_core.artifact.models import Artifact
from greenbook_agent_core.command import (
    CommandInterpreter,
    TargetResolutionStatus,
    TargetResolver,
)
from greenbook_agent_core.command.models import (
    Command,
    CommandTarget,
    CommandType,
    TargetKind,
    TargetReferenceType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import is_failed_objective_retry
from greenbook_agent_core.context import ContextBuilder, ContextSnapshot, SessionContext
from greenbook_agent_core.conversation import (
    ExecutionControlCommand,
)
from greenbook_agent_core.execution.action_observation import (
    INCREMENTAL_PLAN_SOURCE,
    ActionObservation,
)
from greenbook_agent_core.execution.input import ExecutionInput
from greenbook_agent_core.execution.models import (
    ArtifactHandle,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.execution.submission import QueueExecutionSubmissionService
from greenbook_agent_core.goal import GoalCompiler, GoalDecomposer
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.goal.ready_work import select_ready_work
from greenbook_agent_core.goal.satisfaction import (
    goal_states,
    select_unsatisfied_goal_id,
)
from greenbook_agent_core.memory import MemoryRetriever
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.manager import (
    TaskManagerError,
    TaskStateTransitionError,
)
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskResourceRef,
    TaskRevision,
    TaskRevisionType,
    TaskStatus,
)
from greenbook_agent_core.task.objective_reducer import is_context_isolated_task
from greenbook_agent_core.task.provider import TaskProvider, TaskProviderError, TaskScope
from greenbook_agent_core.task.repository import TaskRepositoryError
from greenbook_agent_core.execution.operation_ledger import is_reconciliation_exhausted
from greenbook_contracts.events import EVENT_UNDERSTANDING
from greenbook_contracts.tool_contract import SemanticAction

from ..models.runtime_context import RuntimeContext, TargetContext, TaskContext
from ..models.runtime_result import RuntimeResult
from .retrieval_synthesis_projection import build_retrieval_interaction
from .runtime_agent_service import RuntimeAgentService

logger = logging.getLogger(__name__)


# A Task/Goal mutation and a business side effect are deliberately different
# contracts.  This map is only the bridge from a *canonical semantic action*
# to the existing capability catalog; it never infers an action from user
# text, chooses a resource, or invokes a Tool.
_SEMANTIC_ACTION_CAPABILITIES: dict[str, str] = {
    SemanticAction.SEARCH_POSTS.value: "SEARCH_COMMUNITY",
    SemanticAction.GET_POST.value: "GET_POST_DETAIL",
    SemanticAction.LIST_OWN_POSTS.value: "LIST_OWN_POSTS",
    SemanticAction.CREATE_DRAFT.value: "GENERATE_CONTENT",
    SemanticAction.GET_DRAFT.value: "GET_DRAFT",
    SemanticAction.LIST_DRAFTS.value: "LIST_DRAFTS",
    SemanticAction.UPDATE_DRAFT.value: "MANAGE_DRAFT",
    SemanticAction.DELETE_DRAFT.value: "DELETE_DRAFT",
    SemanticAction.DELETE_POST.value: "DELETE_POST",
    SemanticAction.CREATE_SCHEDULE.value: "SCHEDULE_PUBLISH",
    SemanticAction.GET_SCHEDULE.value: "GET_SCHEDULE_STATUS",
    SemanticAction.UPDATE_SCHEDULE.value: "MANAGE_SCHEDULE",
    SemanticAction.CANCEL_SCHEDULE.value: "CANCEL_SCHEDULE",
    SemanticAction.PUBLISH_NOW.value: "PUBLISH_NOW",
    SemanticAction.LIST_COMMENTS.value: "LIST_COMMENTS",
    SemanticAction.REPLY_COMMENT.value: "REPLY_USER",
    SemanticAction.GET_POST_PERFORMANCE.value: "ANALYZE_PERFORMANCE",
    SemanticAction.GET_ACCOUNT_SUMMARY.value: "ANALYZE_PERFORMANCE",
}

_UPDATE_CONTENT_PROMPT = """You rewrite a GreenBook draft body per a user edit.

Return exactly one JSON object {"content": "..."}. The content is the FULL
rewritten draft body in the same language as the user request. Apply the
requested change to the existing content and return the complete updated body,
not a diff or a partial excerpt. If no existing content is available, write a
coherent body that satisfies the user request.
"""


async def _ensure_update_draft_content(
    arguments: dict[str, Any],
    command: Any,
    *,
    mcp: Any,
    llm: Any,
    model: str,
    session: Any,
    auth: Any,
    conversation_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Fill the replacement body for an UPDATE_DRAFT the model left empty.

    The decision model occasionally emits an update with only draft_id and no
    mutation field; the tool would reject that as a no-op.  Read the current
    draft and rewrite it per the user request so the edit is not silently lost.
    """
    args = dict(arguments)
    if args.get("content") or args.get("title"):
        return args
    draft_id = str(args.get("draft_id") or "")
    existing = ""
    if draft_id and mcp is not None:
        try:
            rd = await mcp.execute_tool(
                "content.get_draft",
                auth=auth,
                session=session,
                trace_id=trace_id,
                agent_run_id=conversation_id or trace_id,
                tool_call_id=str(uuid.uuid4()),
                draft_id=draft_id,
            )
            if isinstance(rd, Mapping) and rd.get("ok"):
                data = rd.get("data") or {}
                existing = str(data.get("content") or "")[:6000]
        except Exception:  # noqa: BLE001 - content synthesis is best-effort
            existing = ""
    instruction = str(
        getattr(command, "raw_input", "") or getattr(command, "requested_goal", "") or ""
    )
    if llm is None or not instruction:
        return args
    try:
        from greenbook_agent_core.llm_compat import extract_top_level_json, structured_call

        resp = await structured_call(
            llm,
            model,
            _UPDATE_CONTENT_PROMPT,
            "rewritten_content",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The full rewritten draft body in the user's language."}
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            {
                "user_request": instruction,
                "existing_content": existing or "(no existing body provided)",
            },
        )
        text = str((getattr(resp, "choices", [None]) or [None])[0].message.content or "")
        content = str(
            json.loads(extract_top_level_json(text)).get("content") or ""
        ).strip()
        if content:
            args["content"] = content
    except Exception:  # noqa: BLE001 - fall back to no synthesis
        logging.getLogger(__name__).exception(
            "update_draft_content_synthesis_failed draft_id=%s", draft_id
        )
    return args


class ConversationRuntimeAdapter:
    """Bind one request to the canonical Agent and Reliable Runtime layers."""

    def __init__(
        self,
        *,
        task_provider: TaskProvider | Any | None = None,
        task_manager: Any | None = None,
        runtime_service: RuntimeAgentService | Any | None = None,
        execution_repository: Any | None = None,
        external_operation_store: Any | None = None,
        observation_store: Any | None = None,
        container: RuntimeContainer | None = None,
        command_runtime: CommandInterpreter | None = None,
        goal_decomposer: GoalDecomposer | None = None,
        agent_loop: AgentLoop | None = None,
        goal_compiler: GoalCompiler | None = None,
        tool_registry: Any | None = None,
        target_resolver: TargetResolver | Any | None = None,
        control_service: Any | None = None,
        approval_service: Any | None = None,
        preference_provider: Any | None = None,
        conversation_service: Any | None = None,
        context_builder: ContextBuilder | None = None,
        memory_retriever: MemoryRetriever | None = None,
        max_concurrent_work_per_conversation: int = 3,
        max_concurrent_direct_tools: int = 6,
    ) -> None:
        self._container = (
            container
            or getattr(runtime_service, "container", None)
            or RuntimeContainer.for_testing()
        )
        self._artifact_registry = self._container.artifact_registry
        self._task_provider = task_provider or TaskProvider()
        self._task_manager = task_manager
        self._runtime_service = runtime_service or RuntimeAgentService(
            repository=execution_repository,
            container=self._container,
        )
        self._execution_repository = execution_repository or getattr(
            self._runtime_service,
            "_execution_repository",
            None,
        )
        self._external_operation_store = external_operation_store
        self._observation_store = observation_store or getattr(
            runtime_service, "_observation_store", None
        )
        self._command_runtime = command_runtime
        self._goal_decomposer = goal_decomposer
        self._agent_loop = agent_loop
        # Goal compilation belongs to the retired GoalTree path.  The
        # production entry point is Objective + ActionLoop; retain the
        # optional field only so historical test/repair tooling can report a
        # clear missing dependency instead of constructing legacy state.
        self._goal_compiler = goal_compiler
        self._tool_registry = tool_registry or self._container.tool_registry
        self._target_resolver = target_resolver or TargetResolver()
        self._max_concurrent_work_per_conversation = max(
            1, max_concurrent_work_per_conversation
        )
        self._direct_tool_semaphore = asyncio.Semaphore(max(1, max_concurrent_direct_tools))
        # Bounded per-conversation semaphore cache.  A long-running API must
        # not grow this dict forever: cap it and evict the least-recently-used
        # entries once the cap is exceeded (design goal 0813 — no unbounded
        # in-process state).
        self._conversation_work_semaphores: dict[str, asyncio.Semaphore] = {}
        self._conversation_semaphore_cap = 1024
        # Recent message fingerprint cache for per-conversation duplicate
        # suppression (idempotency).  Bounded LRU: a long-running API must not
        # leak one entry per message forever.  The window is deliberately
        # short — a double-submit of the same user message in one conversation
        # must not create two Tasks, while two distinct messages always run.
        self._recent_message_keys: dict[str, str] = {}
        self._recent_message_cap = 4096
        self._control_service = control_service
        self._approval_service = approval_service
        self._preference_provider = preference_provider
        memory_manager = getattr(self._runtime_service, "_memory_mgr", None)
        if memory_retriever is None and memory_manager is not None:
            store = getattr(memory_manager, "store", None)
            if store is not None:
                memory_retriever = MemoryRetriever(store)
        self._context_builder = context_builder or ContextBuilder(
            conversation_source=conversation_service,
            task_provider=self._task_provider,
            task_manager=self._task_manager,
            execution_repository=self._execution_repository,
            external_operation_store=self._external_operation_store,
            artifact_store=getattr(self._runtime_service, "_artifact_store", None),
            memory_retriever=memory_retriever,
            preference_provider=preference_provider,
            task_scope_factory=TaskScope,
        )

    async def execute(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        message: str,
        history: Sequence[Mapping[str, str]] | None = None,
        session: SessionContext | Any | None = None,
        timezone: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        mcp: Any = None,
        llm: Any = None,
        model: str = "",
        auth: Any = None,
        detach: bool = False,
        completion_callback: Any = None,
        _command_override: ExecutionControlCommand | None = None,
        activity_callback: Any = None,
        idempotency_key: str = "",
        _pre_interpreted_command: Any = None,
    ) -> RuntimeResult:
        """Run a natural-language turn through the one production path."""

        timings = _start_timings()
        request_session = self._coerce_session(
            session,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone or "Asia/Shanghai",
        )
        run = run_id or str(uuid.uuid4())
        trace = trace_id or str(uuid.uuid4())

        # Per-conversation duplicate suppression: the same user message (or an
        # explicit Idempotency-Key) submitted twice within the short window
        # returns the already-accepted run instead of creating a second Task
        # (design goal 0813 — a double-click/retry must not double-execute).
        # Distinct messages and distinct keys always run.
        duplicate_run_id = self._claim_message_idempotency(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message=message,
            idempotency_key=idempotency_key,
            run_id=run,
        )
        if duplicate_run_id and duplicate_run_id != run:
            return RuntimeResult(
                success=True,
                status="COMPLETED",
                run_id=duplicate_run_id,
                trace_id=trace,
                execution_path="agent_loop",
                content="已收到这条消息，正在处理中。",
                summary="已收到这条消息，正在处理中。",
            )
        try:
            self._validate_session_scope(
                request_session,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            context = await self._build_context_snapshot(
                request_session,
                history=history,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            timings["context_ready_at"] = _now_timing()

            if _command_override is not None:
                return await self._execute_control(
                    _command_override,
                    context=context,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run,
                    trace_id=trace,
                )

            if self._command_runtime is None:
                raise TaskProviderError(
                    "CANONICAL_COMMAND_RUNTIME_UNAVAILABLE",
                    "CommandInterpreter is required for the production runtime.",
                )
            if self._goal_decomposer is None or self._agent_loop is None:
                raise TaskProviderError(
                    "CANONICAL_RUNTIME_INCOMPLETE",
                    "GoalDecomposer and AgentLoop are required for the production runtime.",
                )

            if _pre_interpreted_command is not None:
                # Phase 3A: TurnCoordinator already interpreted and gated this
                # Command.  Reuse it verbatim so the Complex Path does not make
                # a second understanding LLM call (no divergence on target or
                # semantic action).
                command = _pre_interpreted_command
            else:
                command = await self._command_runtime.interpret(
                    message,
                    context,
                    llm=llm,
                    model=model,
                )
            timings["command_ready_at"] = _now_timing()
            # First-step visibility (design goal 0813 — the user must see what
            # the agent understood BEFORE it keeps executing, so a wrong
            # understanding can be stopped instead of discovered after the
            # fact).  Emit the understood task list as the first business
            # activity of the Run.
            if activity_callback is not None:
                understanding = _command_understanding(command)
                emitted = activity_callback(EVENT_UNDERSTANDING, understanding)
                if inspect.isawaitable(emitted):
                    await emitted
            self._apply_active_resource_binding(command, request_session)
            if (
                command.type == CommandType.CONTROL
                and "PUBLISH_NOW" in {
                    str(item).upper() for item in command.required_capabilities
                }
            ):
                # Some providers over-classify an immediate business action
                # as CONTROL. The capability contract distinguishes publish
                # now from an approval or execution control.
                command.type = CommandType.MODIFY
            if command.type == CommandType.CONTROL:
                raise TaskProviderError(
                    "EXECUTION_CONTROL_REQUIRES_TYPED_PAYLOAD",
                    "Execution controls must use the explicit control contract.",
                )
            if command.task_changes:
                # Command normalization boundary (defensive; the interpreter
                # already normalizes): decompose any delta whose desired_changes
                # span several business resources so each business action is
                # scheduled independently.
                from greenbook_agent_core.command.normalization import (
                    normalize_task_deltas,
                )

                command.task_changes = normalize_task_deltas(command.task_changes)
            if command.task_changes and any(
                _delta_is_meaningful(change) for change in command.task_changes
            ):
                # Phase 4 dynamic conversation: the message is a set of
                # desired-state mutations on existing conversation work. The
                # command target checks below do not apply (each delta carries
                # its own target_reference); apply deterministically and let
                # AgentLoop reconcile the affected Tasks against their latest
                # GoalTree. No whole-task GoalTree regeneration and no second
                # Understanding LLM.
                result = await self._run_task_deltas(
                    deltas=command.task_changes,
                    command=command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run,
                    trace_id=trace,
                    llm=llm,
                    model=model,
                    mcp=mcp,
                    auth=auth,
                    activity_callback=activity_callback,
                )
                timings["run_completed_at"] = _now_timing()
                _attach_timings(result, timings)
                return result
            # Every declared change is malformed/empty (observed: the model
            # echoes a full independent request as an empty CREATE_TASK delta).
            # Treat the message as a fresh request instead of asking the user
            # to repeat a complete, unambiguous outcome (design goal 0813 —
            # the user must not be stuck re-typing a full request).

            if command.type in {CommandType.MODIFY, CommandType.CANCEL} and not command.task_changes:
                # Phase 4.1: a new request that mutates existing conversation
                # work must carry TaskDelta. Without it the old path would
                # regenerate the whole GoalTree and overwrite the existing
                # Task. Fail closed instead of silently rebuilding.
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run,
                    trace_id=trace,
                    error_code="MUTATION_REQUIRES_DELTA",
                )

            partial_task_progress = self._supports_partial_task_progress(command)
            if command.needs_clarification and not partial_task_progress:
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run,
                    trace_id=trace,
                    error_code="COMMAND_CLARIFICATION_REQUIRED",
                )
            if (
                command.target_resolution == TargetResolutionStatus.AMBIGUOUS.value
                and not partial_task_progress
            ):
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run,
                    trace_id=trace,
                    error_code="AMBIGUOUS_TARGET",
                )
            if command.is_broad_destructive:
                return self._broad_destructive_result(
                    command,
                    run_id=run,
                    trace_id=trace,
                )
            if command.requires_target and not partial_task_progress:
                if command.target_resolution != TargetResolutionStatus.RESOLVED.value:
                    return await self._clarification_result_with_task(
                        command,
                        context=context,
                        request_session=request_session,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        run_id=run,
                        trace_id=trace,
                        error_code="TARGET_CLARIFICATION_REQUIRED",
                    )
                self._require_resolved_target(command)

            # Rebuild once with the structured Command so MemoryRetriever can
            # use the actual objective while preserving the same fact sources.
            context = await self._build_context_snapshot(
                request_session,
                history=history,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                current_command=command,
            )

            bootstrap_action = self._bootstrap_action(command)
            if bootstrap_action is None:
                # Decompose preserves multi-Goal semantics (a reasoning
                # ANALYZE Goal must stay separate from content production);
                # only a SIMPLE single-capability request is bootstrapped.
                goal_tree = await self._goal_decomposer.decompose(
                    command,
                    context,
                    available_capabilities=self._container.capability_registry.list_all(),
                    llm=llm,
                    model=model,
                )
            else:
                goal_tree = self._bootstrap_goal_tree(command, bootstrap_action)
            timings["goal_tree_ready_at"] = _now_timing()
            result = await self._run_agent_loop(
                command=command,
                goal_tree=goal_tree,
                bootstrap_action=bootstrap_action,
                context_snapshot=context,
                request_session=request_session,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=run,
                trace_id=trace,
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
                detach=detach,
                completion_callback=completion_callback,
                activity_callback=activity_callback,
            )
            timings["run_completed_at"] = _now_timing()
            _attach_timings(result, timings)
            return result
        except TaskProviderError as exc:
            return self._failure_result(
                exc,
                run_id=run,
                trace_id=trace,
            )
        except Exception as exc:
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=run,
                trace_id=trace,
                execution_path="agent_loop",
                error_code=str(getattr(exc, "code", "RUNTIME_ADAPTER_FAILED")),
                error_message=str(exc) or "Canonical Runtime failed.",
            )

    async def submit_fast_path_write(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        capability: str,
        semantic_action: str,
        command: Command,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        session: Any,
        timezone: str,
        mcp: Any,
        llm: Any,
        model: str,
        auth: Any,
        completion_callback: Any = None,
        activity_callback: Any = None,
        task_id: str | None = None,
        objective_id: str = "",
        plan_mode: str = "FAST_PATH_WRITE",
    ) -> RuntimeResult:
        """Durably submit one Fast Path write through the canonical Runtime.

        This mirrors ``submit_tool`` but without AgentLoop state: a single-step
        TaskPlan is queued through ``RuntimeAgentService.submit_plan`` so the
        Worker still runs Java, produces VerificationEvidence and an
        OperationReceipt, and drives UserActivity.  Fast Path writes never
        skip the durable pipeline.
        """
        task_id = task_id or self._fast_path_task_id(command, session)
        incremental = str(plan_mode or "FAST_PATH_WRITE").upper() == "INCREMENTAL"
        if str(semantic_action or "").upper() == "UPDATE_DRAFT":
            arguments = await _ensure_update_draft_content(
                arguments,
                command,
                mcp=mcp,
                llm=llm,
                model=model,
                session=session,
                auth=auth,
                conversation_id=conversation_id,
                trace_id=trace_id,
            )
        plan = TaskPlan(
            task_id=task_id,
            plan_source=INCREMENTAL_PLAN_SOURCE if incremental else "FAST_PATH_WRITE",
            steps=[
                PlanStep(
                    # One Run may resume several Objectives.  The step identity
                    # must therefore include immutable Objective ownership;
                    # otherwise the second Objective reuses the first one's
                    # OperationLedger claim and ResourceRefs.
                    step_id=f"fast-path-{run_id}-{objective_id or 'task'}-{semantic_action}",
                    ordinal=1,
                    capability=capability,
                    tool_name=tool_name,
                    description=f"Fast Path {semantic_action}",
                    constraints=dict(arguments),
                    # Fast Path writes are still scoped to the current
                    # Objective. The queue/worker uses this identity for
                    # ResourceBinding, completion projection, and write
                    # guards; dropping it here makes a later PUBLISH or
                    # SCHEDULE indistinguishable from an unowned write.
                    goal_id=objective_id or None,
                )
            ],
        )
        execution_input = ExecutionInput.from_executable_plan(
            task_id=task_id,
            plan=plan,
            executable=plan,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            goal_id=objective_id,
            goal=_write_goal(command, session),
            goal_category=_fast_path_goal_category(capability),
            target=_fast_path_target(command),
            execution_metadata={
                "plan_mode": "INCREMENTAL" if incremental else "FAST_PATH_WRITE",
                "command": (
                    command.model_dump(mode="json")
                    if command is not None
                    else {}
                ),
                "semantic_action": semantic_action,
            },
            policy_catalog=self._tool_registry,
            trace_context={
                "conversation_id": conversation_id,
                "run_id": run_id,
                "trace_id": trace_id,
            },
        )
        # A stable idempotency key lets the Execution Runtime dedupe this write
        # across duplicate delivery and process restart (the plan/step ids are
        # not stable across a restart).
        execution_input.idempotency_key = _fast_path_stable_key(
            conversation_id,
            task_id,
            semantic_action,
            arguments,
            objective_id,
        )
        task_context = TaskContext(
            task_id=task_id,
            goal=_write_goal(command, session),
            execution_input=execution_input,
            target=_fast_path_target_context(command, task_id),
            constraints=(dict(arguments),),
            artifact_refs=(),
        )
        runtime_context = RuntimeContext(
            conversation_id=conversation_id,
            run_id=run_id,
            trace_id=trace_id,
            task_id=task_id,
            task_context=task_context,
            execution_input=execution_input,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone,
            user_message=_write_goal(command, session),
            conversation_context={},
            session=session,
            active_artifact_id=getattr(session, "active_artifact_id", None),
            active_draft_id=getattr(session, "active_draft_id", None),
            active_schedule_id=getattr(session, "active_schedule_id", None),
            mcp=mcp,
            llm=llm,
            model=model,
            auth=auth,
            objective_id=objective_id,
        )
        submit = getattr(self._runtime_service, "submit_plan", None)
        if not callable(submit):
            raise RuntimeError(
                "RuntimeAgentService.submit_plan is required for Fast Path write execution."
            )
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("execution_submit_start", run_id=run_id)
        except Exception:
            pass
        result = await submit(
            runtime_context,
            plan,
            completion_callback=completion_callback,
        )
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("execution_submitted", run_id=run_id)
        except Exception:
            pass
        result = _ensure_runtime_result(result)
        result.task_id = task_id
        if activity_callback is not None:
            owned = {
                "task_id": task_id,
                "semantic_action": semantic_action,
                "tool_name": tool_name,
            }
            emitted = activity_callback("FAST_WRITE_SUBMITTED", owned)
            if inspect.isawaitable(emitted):
                await emitted
        if result.execution_id:
            bind_execution = getattr(self._task_manager, "bind_execution", None)
            if callable(bind_execution):
                bound = bind_execution(
                    task_id,
                    result.execution_id,
                    goal_id=objective_id or None,
                    status=str(result.status or "SUBMITTED"),
                )
                bound = await bound if inspect.isawaitable(bound) else bound
        return result

    @staticmethod
    def _fast_path_task_id(command: Command, session: Any) -> str:
        target = command.resolved_target or (
            command.target.model_dump(mode="json") if command.target is not None else None
        )
        if isinstance(target, Mapping):
            task_id = str(target.get("task_id") or "")
            if task_id:
                return task_id
            resource_id = str(
                target.get("resource_id")
                or target.get("id")
                or target.get("draft_id")
                or target.get("schedule_id")
                or ""
            )
            if resource_id:
                return resource_id
        for field in ("active_task_id", "active_draft_id", "active_schedule_id"):
            identifier = str(getattr(session, field, "") or "").strip()
            if identifier:
                return identifier
        return f"fast-path-{command.command_id}"

    async def execute_fast_path_read(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        user_request: str = "",
        synthesis_requested: bool = False,
        llm: Any | None = None,
        model: str = "",
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        session: Any,
        auth: Any,
        mcp: Any,
    ) -> RuntimeResult:
        """Run one Fast Path read through the MCP tool boundary.

        Returns the real tool response; no activity is fabricated.  Reads are
        the only Fast Path branch that runs synchronously because they have no
        side effect and produce no OperationReceipt by design.
        """
        if mcp is None or not callable(getattr(mcp, "execute_tool", None)):
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=run_id,
                trace_id=trace_id,
                execution_path="fast_path",
                error_code="FAST_READ_HANDLER_UNAVAILABLE",
                error_message="Fast Path read boundary is unavailable.",
            )
        value = await mcp.execute_tool(
            tool_name,
            auth=auth,
            session=session,
            trace_id=trace_id,
            agent_run_id=run_id,
            tool_call_id=str(uuid.uuid4()),
            **arguments,
        )
        # Preserve the canonical tool failure envelope at the read boundary.
        # ActionLoop consumes RuntimeResult, but must still be able to classify
        # argument/auth/backend failures without guessing from a generic status.
        if isinstance(value, Mapping) and not value.get("ok", True):
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage(
                    "read_tool_failure",
                    {
                        "tool_name": tool_name,
                        "arguments": dict(arguments),
                        "code": value.get("code"),
                        "message": value.get("message"),
                        "user_message": value.get("user_message"),
                        "retryable": value.get("retryable"),
                        "request_sent": value.get("request_sent"),
                        "state": value.get("state"),
                        "data": value.get("data"),
                        "auth_token_present": bool(getattr(auth, "raw_access_token", None)),
                        "auth_token_length": len(str(getattr(auth, "raw_access_token", "") or "")),
                        "auth_token_fingerprint": hashlib.sha256(
                            str(getattr(auth, "raw_access_token", "") or "").encode()
                        ).hexdigest()[:12],
                    },
                )
            except Exception:  # diagnostics must never affect execution
                pass
        if not isinstance(value, Mapping) or not value.get("ok", True):
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=run_id,
                trace_id=trace_id,
                execution_path="fast_path",
                error_code=str(
                    value.get("code") if isinstance(value, Mapping) else "FAST_READ_FAILED"
                ),
                error_message=str(
                    value.get("error") or value.get("message") or "Fast Path read failed."
                    if isinstance(value, Mapping)
                    else "Fast Path read failed."
                ),
                failure_state=(
                    dict(value.get("state") or {})
                    if isinstance(value, Mapping) else None
                ),
                artifacts=[dict(value)] if isinstance(value, Mapping) else [],
            )
        tool_result = dict(value)
        tool_result.setdefault("tool_name", tool_name)
        interaction, safe_message = await build_retrieval_interaction(
            request=user_request or str(arguments.get("query") or tool_name),
            tool_results=[tool_result],
            synthesis_requested=synthesis_requested,
            llm=llm,
            model=model,
        )
        partial_results: dict[str, Any] = {}
        if interaction is not None:
            partial_results["user_facing_interaction"] = interaction
        content = safe_message or str(value.get("content") or _stringify(value))
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=run_id,
            trace_id=trace_id,
            execution_path="fast_path",
            content=content,
            summary=content,
            artifacts=[dict(value)],
            partial_results=partial_results,
        )

    async def _execute_control(
        self,
        command: ExecutionControlCommand,
        *,
        context: ContextSnapshot,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        resolution = self._target_resolver.resolve(command.as_command(), context)
        if not resolution.is_resolved or resolution.target is None:
            if resolution.status == TargetResolutionStatus.AMBIGUOUS:
                return RuntimeResult(
                    success=False,
                    status="WAITING_HUMAN",
                    run_id=run_id,
                    trace_id=trace_id,
                    execution_path="runtime",
                    error_code="AMBIGUOUS_TARGET",
                    error_message="Select one execution target before continuing.",
                    partial_results={
                        "clarification": {
                            "command": command.model_dump(mode="json"),
                            "candidates": [
                                item.model_dump(mode="json")
                                for item in resolution.candidates
                            ],
                        }
                    },
                )
            raise TaskProviderError(
                "EXECUTION_TARGET_NOT_FOUND",
                "The execution control target could not be resolved.",
            )
        target = resolution.target
        if command.is_execution_control:
            if self._control_service is None:
                raise TaskProviderError(
                    "EXECUTION_CONTROL_UNAVAILABLE",
                    "Conversation execution control is not configured.",
                )
            return await self._control_service.execute(
                command,
                target,
                run_id=run_id,
                trace_id=trace_id,
            )
        if command.is_approval:
            if self._approval_service is None:
                raise TaskProviderError(
                    "APPROVAL_CONTROL_UNAVAILABLE",
                    "Conversation approval control is not configured.",
                )
            return await self._approval_service.execute_command(
                command,
                target,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=run_id,
                trace_id=trace_id,
            )
        raise TaskProviderError(
            "EXECUTION_CONTROL_INVALID",
            "Unsupported execution control command.",
        )

    async def _run_task_deltas(
        self,
        *,
        deltas: Sequence[TaskDelta],
        command: Command,
        context: Any,
        request_session: SessionContext | Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        llm: Any,
        model: str,
        mcp: Any = None,
        auth: Any = None,
        activity_callback: Any = None,
    ) -> RuntimeResult:
        """Apply TaskDelta mutations and reconcile affected Tasks with AgentLoop.

        Each delta is validated deterministically and applied through
        TaskManager (or a GoalTree patch on the task's own snapshot); a change
        that cannot be safely grounded upgrades to ASK_USER. Affected Tasks are
        then resumed with their latest GoalTree so AgentLoop decides the next
        real business action (cancel schedule, generate draft, ...). In-flight
        Executions are never mutated.
        """
        manager = self._task_manager
        if manager is None:
            return self._failure_result(
                TaskProviderError(
                    "TASK_MANAGER_UNAVAILABLE",
                    "TaskDelta apply requires a TaskManager.",
                ),
                run_id=run_id,
                trace_id=trace_id,
            )
        mutations: list[dict[str, Any]] = []
        affected: dict[str, Task | Any] = {}
        applied_change_ids: set[str] = set()

        for delta in deltas:
            operation = delta.operation
            if operation == TaskDeltaOperation.NO_CHANGE:
                continue
            if delta.change_id:
                if delta.change_id in applied_change_ids:
                    continue
                applied_change_ids.add(delta.change_id)
            if operation == TaskDeltaOperation.ASK_USER:
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    error_code="DELTA_CLARIFICATION_REQUIRED",
                    persist_task=False,
                )
            user_objective_retry = _is_user_triggered_objective_retry(delta)
            if delta.needs_target_resolution and not user_objective_retry:
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    error_code="MUTATION_TARGET_REQUIRED",
                    persist_task=False,
                )
            if user_objective_retry:
                try:
                    resolved = await self._resolve_delta_target(
                        delta,
                        request_session,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        user_input=str(getattr(command, "raw_input", "") or ""),
                        return_resolution=True,
                    )
                    task, resolution = resolved
                    if task is None or resolution is None or not resolution.is_resolved:
                        if resolution is not None and resolution.reason == "retry_requires_reconciliation":
                            error_code = "RETRY_REQUIRES_RECONCILIATION"
                        elif resolution is not None and resolution.status == TargetResolutionStatus.AMBIGUOUS:
                            error_code = "AMBIGUOUS_FAILED_OBJECTIVE"
                        else:
                            error_code = "FAILED_OBJECTIVE_NOT_FOUND"
                        return await self._clarification_result_with_task(
                            command,
                            context=context,
                            request_session=request_session,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            run_id=run_id,
                            trace_id=trace_id,
                            error_code=error_code,
                            persist_task=False,
                        )
                    retry_task = await self._create_user_objective_retry_task(
                        manager,
                        task,
                        delta,
                        command,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                    )
                    affected.setdefault(retry_task.task_id, retry_task)
                    mutations.append(
                        self._mutation_record(
                            delta,
                            retry_task,
                            "NEW_RETRY_TASK",
                            predecessor_task_id=str(getattr(task, "task_id", "") or ""),
                            predecessor_objective_id=str(
                                delta.target_reference.get("objective_id")
                                or delta.target_reference.get("target_objective_id")
                                or delta.desired_changes.get("objective_id")
                                or delta.desired_changes.get("target_objective_id")
                                or ""
                            ),
                        )
                    )
                    continue
                except TaskDeltaGroundingError:
                    return await self._clarification_result_with_task(
                        command,
                        context=context,
                        request_session=request_session,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        error_code="FAILED_OBJECTIVE_NOT_FOUND",
                        persist_task=False,
                    )
            if operation == TaskDeltaOperation.UPDATE_GOAL and not _has_delta_fields(
                delta.desired_changes
            ):
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    error_code="MUTATION_FIELDS_REQUIRED",
                    persist_task=False,
                )
            try:
                if operation == TaskDeltaOperation.CREATE_TASK:
                    if not (delta.desired_changes.get("required_capabilities") or []):
                        # A CREATE delta without any capability cannot build an
                        # executable Goal.  Executing an empty Goal makes
                        # AgentLoop flail (observed: repeated read tools under a
                        # GENERATE_CONTENT label, then a silent "COMPLETED" with
                        # no user-visible result).  Ask the user instead of
                        # silently doing nothing useful.
                        return await self._clarification_result_with_task(
                            command,
                            context=context,
                            request_session=request_session,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            run_id=run_id,
                            trace_id=trace_id,
                            error_code="DELTA_REQUIRES_CAPABILITIES",
                            persist_task=False,
                        )
                    task = await self._delta_create_task(
                        delta, command, conversation_id, user_id, tenant_id,
                    )
                    affected.setdefault(task.task_id, task)
                else:
                    task = await self._resolve_delta_target(
                        delta,
                        request_session,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        user_input=str(getattr(command, "raw_input", "") or ""),
                    )
                    if task is None:
                        return await self._clarification_result_with_task(
                            command,
                            context=context,
                            request_session=request_session,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            run_id=run_id,
                            trace_id=trace_id,
                            error_code=(
                                "MUTATION_TARGET_REQUIRED"
                                if operation
                                in {
                                    TaskDeltaOperation.UPDATE_GOAL,
                                    TaskDeltaOperation.CANCEL_GOAL,
                                    TaskDeltaOperation.CANCEL_TASK,
                                    TaskDeltaOperation.CONTINUE_TASK,
                                }
                                else "DELTA_TARGET_NOT_FOUND"
                            ),
                            persist_task=False,
                        )
                    semantic_action = _semantic_action_for_delta(delta)
                    # A semantic business action is not a Task lifecycle
                    # command even if an upstream interpreter happened to
                    # label it ``CANCEL_TASK``.  In particular, a natural
                    # language "cancel publication" must never cancel the
                    # long-lived Task before Java confirms the Schedule has
                    # actually been cancelled.  The semantic operation is
                    # compiled into a durable action Goal below instead.
                    if (
                        operation == TaskDeltaOperation.CANCEL_TASK
                        and not semantic_action
                    ):
                        if task.status == TaskStatus.CANCELLED:
                            mutations.append(self._mutation_record(delta, task, "ALREADY_CANCELLED"))
                            continue
                        task = await manager.cancel_task(
                            task.task_id,
                            reason=str(delta.desired_changes.get("reason") or "User cancelled the task."),
                        )
                    elif operation == TaskDeltaOperation.CONTINUE_TASK:
                        await self._resume_task_if_paused(manager, task)
                    elif semantic_action or operation in {
                        TaskDeltaOperation.ADD_GOAL,
                        TaskDeltaOperation.UPDATE_GOAL,
                        TaskDeltaOperation.CANCEL_GOAL,
                    }:
                        # A resolved Task is still not sufficient evidence for
                        # a write.  Bind the action to one resource owned by
                        # that Task before changing its GoalTree.  This makes
                        # a later Worker invocation independent from mutable
                        # conversation-global ``active_*`` fields.
                        delta = _bind_semantic_action_resource(delta, task)
                        task = await self._apply_goal_mutation(
                            manager,
                            task,
                            delta,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            tenant_id=tenant_id,
                        )
                    else:
                        mutations.append(self._mutation_record(delta, task, "UNSUPPORTED_OPERATION"))
                        continue
                    # Several semantic mutations in one turn may target the
                    # same long-lived Task.  Keep the newest CAS-persisted
                    # snapshot so the resume sees every appended action Goal;
                    # retaining the first object would silently drop later
                    # mutations from the execution fan-out.
                    affected[task.task_id] = task
                mutations.append(self._mutation_record(delta, task, "APPLIED"))
            except TaskStateTransitionError as exc:
                return self._failure_result(
                    TaskProviderError("DELTA_TRANSITION_REJECTED", str(exc)),
                    run_id=run_id,
                    trace_id=trace_id,
                )
            except TaskDeltaGroundingError:
                # A malformed or stale Goal reference is a clarification
                # boundary, not an internal apply failure.  In particular,
                # never let a partial/ambiguous label update another Goal.
                return await self._clarification_result_with_task(
                    command,
                    context=context,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    error_code="MUTATION_TARGET_REQUIRED",
                    persist_task=False,
                )
            except TaskManagerError as exc:
                return self._failure_result(
                    TaskProviderError("DELTA_APPLY_FAILED", str(exc)),
                    run_id=run_id,
                    trace_id=trace_id,
                )

        if not affected:
            return RuntimeResult(
                success=True,
                status="COMPLETED",
                run_id=run_id,
                trace_id=trace_id,
                execution_path="task_delta",
                content="已处理当前对话中的任务变更。",
                partial_results={"task_delta": mutations},
            )

        results: list[RuntimeResult] = []
        # A Task owns its Artifact and ResourceBinding. Never inject a
        # sibling Task's draft into this Task's execution context: otherwise
        # a multi-task turn can schedule or revise the wrong article.

        async def _resume_one(task: Task | Any) -> RuntimeResult | None:
            tree = self._task_goal_tree(task)
            if tree is None:
                return None
            # A delta may fan out to several Tasks concurrently.  Give each
            # loop a resource-scoped working set instead of the conversation
            # global active_draft_id/active_schedule_id; otherwise a valid
            # Java/Agent resource from sibling Task A can be used by Task B.
            task_session = _session_scoped_to_task(request_session, task)
            task_context = _context_scoped_to_task(context, task)
            # The original Command describes the aggregate user turn. For a
            # TASK_DELTA tree, handing that aggregate envelope to every
            # concurrent loop would let sibling constraints leak back in via
            # command-level context. Use the Task-owned semantic envelope;
            # the GoalTree remains the source of executable facts.
            task_command = _command_scoped_to_goal_tree(command, tree)
            # Each Task resumes with its own GoalTree so its AgentLoop decides
            # the next real business action independently. Tasks are resumed
            # CONCURRENTLY (design reference: nanobot multi-goal turns) — a
            # three-post request must not serialize three full loops, which
            # exhausts the per-run iteration budget before every task is done.
            return await self._run_agent_loop(
                command=task_command,
                goal_tree=tree,
                context_snapshot=task_context,
                request_session=task_session,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=run_id,
                trace_id=trace_id,
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
                detach=False,
                completion_callback=None,
                activity_callback=activity_callback,
                existing_task=task,
                mutation_applied=mutations,
            )

        resumed_results = await asyncio.gather(
            *(_resume_one(task) for task in affected.values())
        )
        results = [item for item in resumed_results if item is not None]
        merged = self._merge_mutation_results(results, mutations)
        return merged

    async def _apply_goal_mutation(
        self,
        manager: Any,
        current_task: Any,
        delta: TaskDelta,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> Task:
        """Apply a GoalTree mutation with optimistic CAS retry.

        Each attempt reloads the latest Task/GoalTree and lets bind_goal_tree's
        expected_version predicate reject a lost update; on conflict it retries
        against the newest snapshot (merge for independent Goals, latest intent
        wins for the same Goal).
        """
        task_id = str(current_task.task_id)
        last_error: Exception | None = None
        for _attempt in range(3):
            task = await manager.get_required(
                task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            tree = self._task_goal_tree(task)
            if tree is None:
                if not _semantic_action_for_delta(delta):
                    raise TaskProviderError(
                        "DELTA_TASK_NO_GOAL_TREE",
                        "Task has no GoalTree to mutate.",
                    )
                return await self._append_objective_mutation(
                    manager,
                    task,
                    delta,
                )
            tree = self._apply_goal_delta(tree, delta)
            try:
                return await manager.bind_goal_tree(task_id, tree)
            except TaskRepositoryError as exc:
                last_error = exc
        raise TaskProviderError(
            "DELTA_CAS_CONFLICT",
            f"Concurrent mutation conflict after retries: {last_error}",
        )

    async def _append_objective_mutation(
        self,
        manager: Any,
        task: Task,
        delta: TaskDelta,
    ) -> Task:
        """Append one cross-turn business Objective to an Objective-first Task.

        This is the Objective-first counterpart of the legacy GoalTree action
        append.  The historical Objective remains untouched; only the exact
        resolved ResourceBinding is carried into the new logical outcome.
        """

        semantic_action = _semantic_action_for_delta(delta)
        if not semantic_action:
            raise TaskProviderError(
                "DELTA_SEMANTIC_ACTION_REQUIRED",
                "An Objective-first mutation requires a canonical semantic action.",
            )
        desired = dict(delta.desired_changes or {})
        change_id = str(delta.change_id or "")
        resource_id = _delta_resource_id(delta)
        if resource_id and not desired.get("target_objective_id"):
            predecessor_id = _task_resource_owner_objective_id(
                task,
                resource_id,
                _delta_resource_kind(delta),
            )
            if predecessor_id:
                # The new mutation Objective is distinct, but admission must
                # retain the exact historical Objective that owns the bound
                # resource.  This is required for cross-turn cancellation
                # after a prior schedule update.
                desired["target_objective_id"] = predecessor_id
                desired.setdefault("objective_id", predecessor_id)
        from greenbook_agent_core.task.objective_reducer import (
            mutation_conflicts,
            mutation_details,
            mutation_execution_state,
            mutation_objective_details,
            mutation_objective_is_superseded,
            supersede_mutation_objective,
        )
        metadata = mutation_details(semantic_action, desired, resource_id)
        if change_id:
            existing = next(
                (
                    objective
                    for objective in (getattr(task, "objectives", ()) or ())
                    if str(
                        (getattr(objective, "constraints", {}) or {}).get(
                            "mutation_change_id", ""
                        )
                    ) == change_id
                ),
                None,
            )
            if existing is not None:
                if mutation_objective_is_superseded(existing):
                    # A replay of an older turn must not resurrect the
                    # superseded logical mutation.
                    desired["objective_id"] = str(existing.objective_id)
                    delta.desired_changes = desired
                    return task
                desired["objective_id"] = str(existing.objective_id)
                delta.desired_changes = desired
                return task

        # Same resource/domain/value is one logical mutation.  Reuse it while
        # pending, in-flight, unknown, or already verified; only a changed
        # desired value creates a new Objective.
        existing = next(
            (
                item for item in (getattr(task, "objectives", ()) or ())
                if not mutation_objective_is_superseded(item)
                and mutation_objective_details(item)["resource_id"] == resource_id
                and mutation_objective_details(item)["domain"]
                == str(metadata["mutation_domain"] or "").upper()
                and mutation_objective_details(item)["expected_state"]
                == metadata["mutation_expected_state"]
                and mutation_execution_state(task, item)
                in {"PENDING", "INFLIGHT", "UNKNOWN", "COMPLETED"}
            ),
            None,
        )
        if existing is not None:
            desired["objective_id"] = str(existing.objective_id)
            delta.desired_changes = desired
            return task

        supersede_candidates = [
            item
            for item in (getattr(task, "objectives", ()) or ())
            if not mutation_objective_is_superseded(item)
            and mutation_conflicts(
                item,
                {
                    "resource_id": resource_id,
                    "domain": metadata["mutation_domain"],
                    "expected_state": metadata["mutation_expected_state"],
                    "target_objective_id": metadata.get("target_objective_id", ""),
                },
            )
            and mutation_execution_state(task, item) == "PENDING"
        ]

        capability = _SEMANTIC_ACTION_CAPABILITIES[semantic_action]
        from greenbook_agent_core.task.models import TaskRevision, TaskRevisionType
        from greenbook_agent_core.task.objective_compat import objectives_for_capabilities

        templates = objectives_for_capabilities(
            [capability],
            str(task.task_id),
            fallback_intent=semantic_action,
        )
        if templates:
            objective = templates[0]
        else:
            from greenbook_agent_core.task.models import Objective

            objective = Objective(task_id=str(task.task_id))
        objective.objective_id = f"mutation-{uuid.uuid4().hex[:12]}"
        objective.description = str(
            desired.get("description")
            or desired.get("instruction")
            or semantic_action.replace("_", " ").title()
        ).strip()
        objective.intent = semantic_action
        objective.required_capabilities = [capability]
        objective.result_requirement = "RESOURCE_MUTATION"
        objective.constraints = dict(desired)
        objective.constraints.update(metadata)
        objective.constraints["mutation_status"] = "ACTIVE"
        if change_id:
            objective.constraints["mutation_change_id"] = change_id
        if resource_id:
            objective.related_resource_ids = [resource_id]
            target = dict(desired.get("resource_target") or {})
            if target:
                objective.constraints["target"] = target
        task.objectives.append(objective)
        task.revisions.append(
            TaskRevision(
                task_id=str(task.task_id),
                type=TaskRevisionType.ADD_GOAL,
                payload={
                    "kind": "CROSS_TURN_OBJECTIVE_MUTATION",
                    "objective_id": objective.objective_id,
                    "target_objective_id": str(
                        desired.get("target_objective_id") or ""
                    ),
                    "semantic_action": semantic_action,
                    "resource_id": resource_id,
                    "change_id": change_id,
                    **metadata,
                },
                previous_version=int(getattr(task, "version", 0) or 0),
            )
        )
        for old in supersede_candidates:
            supersede_mutation_objective(
                task,
                old,
                new_objective_id=objective.objective_id,
                resource_id=resource_id,
                new_details=metadata,
            )
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            task.status = manager._transition_value(task.status, TaskStatus.READY)
            task.phase = "READY"
            task.completed_at = None
            task.last_error = None
        persist = getattr(manager, "_persist", None)
        if callable(persist):
            persisted = persist(task)
            return await persisted if inspect.isawaitable(persisted) else persisted
        repository = getattr(manager, "repository", None)
        repository = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repository, "update", None)
        if callable(update):
            persisted = update(task, expected_version=getattr(task, "version", None))
            return await persisted if inspect.isawaitable(persisted) else persisted
        return task

    @staticmethod
    def _mutation_record(
        delta: TaskDelta,
        task: Any,
        outcome: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "operation": delta.operation.value,
            "semantic_action": _semantic_action_for_delta(delta),
            "change_id": delta.change_id,
            "task_id": str(getattr(task, "task_id", "")),
            "outcome": outcome,
            "version": int(getattr(task, "version", 0) or 0),
            **extra,
        }

    async def _create_user_objective_retry_task(
        self,
        manager: Any,
        predecessor_task: Task,
        delta: TaskDelta,
        command: Command,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> Task:
        """Create a fresh Task for an explicit user retry.

        The predecessor is read-only history.  Its failed Objective supplies
        the desired outcome and exact ResourceRefs, but no lifecycle field,
        execution id, operation id, or terminal status is copied into the new
        logical operation.
        """

        if await _conversation_has_exhausted_reconciliation(
            self._external_operation_store,
            conversation_id,
        ):
            raise TaskDeltaGroundingError(
                "This conversation contains a budget-exhausted RESULT_UNKNOWN; "
                "manual reconciliation is required before retry."
            )

        desired = dict(delta.desired_changes or {})
        predecessor_objective_id = str(
            desired.get("target_objective_id")
            or desired.get("objective_id")
            or (delta.target_reference or {}).get("target_objective_id")
            or (delta.target_reference or {}).get("objective_id")
            or ""
        )
        predecessor_objective = next(
            (
                item for item in (getattr(predecessor_task, "objectives", ()) or ())
                if str(getattr(item, "objective_id", ""))
                == predecessor_objective_id
            ),
            None,
        )
        if predecessor_objective is None:
            raise TaskDeltaGroundingError(
                "A user retry must identify one persisted Objective."
            )
        predecessor_status = str(
            getattr(getattr(predecessor_objective, "status", None), "value", None)
            or getattr(predecessor_objective, "status", "")
            or ""
        ).upper()
        if predecessor_status != ObjectiveStatus.FAILED.value:
            raise TaskDeltaGroundingError(
                "Only a reconciled FAILED Objective can be retried by the user."
            )
        if _objective_has_unreconciled_execution(
            predecessor_task,
            predecessor_objective,
        ):
            raise TaskDeltaGroundingError(
                "RESULT_UNKNOWN must be reconciled before a user retry."
            )

        explicit = {
            str(key): value
            for key, value in desired.items()
            if str(key)
            not in {
                "objective_id",
                "target_objective_id",
                "user_triggered_retry",
                "retry",
                "retry_of_objective_id",
                "retry_of_task_id",
                "kind",
                "status",
                "semantic_action",
                "semantic_operation",
                "resource_target",
                "target",
                "required_capabilities",
            }
        }
        old_resources = _retry_resource_refs(
            predecessor_task,
            predecessor_objective,
        )
        resource_kinds = {
            str(getattr(item, "resource_kind", "") or "").upper()
            for item in old_resources
        }
        user_changes_resource = bool(
            explicit.get("title")
            or explicit.get("content")
            or explicit.get("body")
            or explicit.get("instruction")
            or explicit.get("summary")
        )
        capabilities = _retry_remaining_capabilities(
            predecessor_objective,
            resource_kinds=resource_kinds,
            user_changes_resource=user_changes_resource,
            has_schedule_changes=bool(
                explicit.get("run_at")
                or explicit.get("scheduled_at")
                or explicit.get("publish_at")
                or explicit.get("publish_time")
            ),
        )
        if not capabilities:
            raise TaskDeltaGroundingError(
                "The failed Objective has no safe remaining outcome to retry."
            )

        constraints = dict(getattr(predecessor_objective, "constraints", {}) or {})
        # Terminal/runtime metadata is historical and must not become the new
        # execution's completion evidence.
        for key in (
            "objective_id",
            "target_objective_id",
            "mutation_change_id",
            "mutation_status",
            "execution_id",
            "operation_id",
        ):
            constraints.pop(key, None)
        nested_constraints = explicit.pop("constraints", None)
        if isinstance(nested_constraints, Mapping):
            constraints.update(dict(nested_constraints))
        constraints.update(explicit)
        constraints["retry_of_task_id"] = str(predecessor_task.task_id)
        constraints["retry_of_objective_id"] = predecessor_objective_id
        constraints["user_triggered_retry"] = True

        target = dict(
            desired.get("resource_target")
            or desired.get("target")
            or {}
        )
        if not target:
            target = {}
        for resource in old_resources:
            kind = str(getattr(resource, "resource_kind", "") or "").upper()
            resource_id = str(getattr(resource, "resource_id", "") or "")
            if kind == "DRAFT" and resource_id and "draft_id" not in target:
                target.update({"kind": "DRAFT", "id": resource_id, "resource_id": resource_id})
                constraints.setdefault("draft_id", resource_id)
            elif kind == "SCHEDULE" and resource_id:
                constraints.setdefault("schedule_id", resource_id)
            elif kind == "POST" and resource_id:
                constraints.setdefault("post_id", resource_id)
        if target:
            constraints["resource_target"] = dict(target)

        description = str(
            explicit.get("description")
            or explicit.get("title")
            or getattr(predecessor_objective, "description", "")
            or getattr(predecessor_objective, "intent", "")
            or command.requested_goal
            or "Retry failed objective"
        ).strip()
        new_objective_id = f"retry-objective-{uuid.uuid4().hex[:12]}"
        goal = Goal(
            goal_id=new_objective_id,
            description=description,
            goal_type="TASK",
            required_capabilities=list(capabilities),
            constraints=[dict(constraints)] if constraints else [],
            target=dict(target),
            semantic_operation=str(
                getattr(predecessor_objective, "intent", "") or ""
            ),
        )
        tree = GoalTree(
            root=goal,
            command_id=command.command_id,
            source="TASK_DELTA",
            version=1,
        )
        new_task = await manager.create_task(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            root_goal=goal,
            goal_tree=tree,
        )
        new_objective = next(
            (
                item for item in (getattr(new_task, "objectives", ()) or ())
                if str(getattr(item, "objective_id", "")) == new_objective_id
            ),
            None,
        )
        if new_objective is None:
            new_objective = Objective(
                objective_id=new_objective_id,
                task_id=str(new_task.task_id),
            )
            new_task.objectives = [new_objective]
        new_objective.task_id = str(new_task.task_id)
        new_objective.description = description
        new_objective.intent = str(
            getattr(predecessor_objective, "intent", "") or description
        )
        new_objective.status = ObjectiveStatus.PENDING
        new_objective.required_capabilities = list(capabilities)
        new_objective.expected_resource_kind = str(
            getattr(predecessor_objective, "expected_resource_kind", "") or ""
        )
        new_objective.result_requirement = str(
            getattr(predecessor_objective, "result_requirement", "DIRECT_RESULT")
            or "DIRECT_RESULT"
        )
        new_objective.min_sources = int(
            getattr(predecessor_objective, "min_sources", 1) or 1
        )
        new_objective.constraints = dict(constraints)
        new_objective.dependencies = []
        new_objective.related_resource_ids = []
        new_objective.related_artifact_ids = []
        new_objective.related_operations = []
        new_objective.completed_at = None

        copied_resources: list[TaskResourceRef] = []
        for resource in old_resources:
            copied_resources.append(
                resource.model_copy(
                    update={"objective_id": new_objective_id},
                    deep=True,
                )
            )
            resource_id = str(getattr(resource, "resource_id", "") or "")
            if resource_id and resource_id not in new_objective.related_resource_ids:
                new_objective.related_resource_ids.append(resource_id)
        new_task.resource_index = copied_resources
        new_task.revisions.append(
            TaskRevision(
                task_id=str(new_task.task_id),
                type=TaskRevisionType.ADD_GOAL,
                payload={
                    "kind": "USER_OBJECTIVE_RETRY",
                    "retry_of_task_id": str(predecessor_task.task_id),
                    "retry_of_objective_id": predecessor_objective_id,
                    "objective_id": new_objective_id,
                    "resource_ids": list(new_objective.related_resource_ids),
                    "user_changes": dict(explicit),
                },
                previous_version=int(getattr(new_task, "version", 0) or 0),
            )
        )
        persist = getattr(manager, "_persist", None)
        if callable(persist):
            persisted = persist(new_task)
            return await persisted if inspect.isawaitable(persisted) else persisted
        repository = getattr(manager, "repository", None)
        repository = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repository, "update", None)
        if callable(update):
            persisted = update(
                new_task,
                expected_version=getattr(new_task, "version", None),
            )
            return await persisted if inspect.isawaitable(persisted) else persisted
        return new_task

    async def _delta_create_task(
        self,
        delta: TaskDelta,
        command: Command,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> Task:
        manager = self._task_manager
        assert manager is not None
        # ``Command.constraints`` describes the whole user turn. A turn may
        # create several independent Tasks, however, and its shared envelope
        # must never be copied into every Task (for example Java's ``run_at``
        # must not become the Agent article's publish time). CREATE_TASK
        # deltas are the ownership boundary: only their own structured facts
        # are persisted in the new GoalTree. Missing per-task facts remain
        # missing and are clarified later; they are never guessed from a
        # sibling or request-global value.
        desired = dict(delta.desired_changes or {})
        description = str(
            desired.get("description")
            or desired.get("goal")
            or command.requested_goal
            or ""
        ).strip()
        if not description:
            raise TaskManagerError("CREATE_TASK requires a goal description.")
        capabilities = list(
            dict.fromkeys(desired.get("required_capabilities") or [])
        )
        if not capabilities:
            raise TaskManagerError(
                "DELTA_REQUIRES_CAPABILITIES",
                "The change does not declare any capability; ask the user what to do instead of executing an empty Goal.",
            )
        # New Task creation is Objective-first.  GoalTree is retained only as
        # a reader/compatibility boundary for historical rows; a fresh delta
        # must never materialize a legacy snapshot or TaskGoal projection.
        task = await manager.create_task(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            goal=description,
            goal_category=str(desired.get("goal_category") or ""),
        )
        from greenbook_agent_core.task.models import Objective
        from greenbook_agent_core.task.objective_compat import objectives_for_capabilities

        objectives = objectives_for_capabilities(
            capabilities,
            str(task.task_id),
            fallback_intent=description,
        )
        if not objectives:
            objectives = [Objective(task_id=str(task.task_id), description=description, intent=description)]
        objective = objectives[0]
        objective.description = description
        objective.intent = str(desired.get("semantic_operation") or description)
        objective.required_capabilities = [str(item).upper() for item in capabilities]
        constraints = dict(desired.get("constraints") or {})
        for key in ("run_at", "timezone", "temporal_constraint", "publication_intent", "target"):
            if desired.get(key) is not None:
                constraints[key] = desired[key]
        objective.constraints = constraints
        if any(str(item).upper() in {"GENERATE_CONTENT", "MANAGE_DRAFT", "SCHEDULE_PUBLISH", "MANAGE_SCHEDULE", "CANCEL_SCHEDULE", "PUBLISH_NOW", "DELETE_DRAFT", "DELETE_POST"} for item in capabilities):
            objective.result_requirement = "RESOURCE_MUTATION"
        task.objectives = objectives
        repository = getattr(manager, "repository", None)
        repository = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repository, "update", None)
        if callable(update):
            persisted = update(task, expected_version=getattr(task, "version", None))
            return await persisted if inspect.isawaitable(persisted) else persisted
        return task

    async def _resolve_delta_target(
        self,
        delta: TaskDelta,
        session: SessionContext | Any,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        user_input: str = "",
        return_resolution: bool = False,
    ) -> Task | tuple[Task | None, Any | None] | None:
        manager = self._task_manager
        if manager is None:
            return (None, None) if return_resolution else None
        candidates = await manager.get_resolvable_tasks(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        exhausted = await _conversation_has_exhausted_reconciliation(
            self._external_operation_store,
            conversation_id,
        )
        resolver = getattr(self, "_target_resolver", None) or TargetResolver()
        target_candidates: list[dict[str, Any]] = []
        for task in candidates:
            if is_context_isolated_task(task):
                continue
            if exhausted and _task_status_value(task) == "FAILED":
                # A failed Task in a conversation with an exhausted unknown
                # write is historical evidence, not a safe retry target.
                continue
            task_id = str(getattr(task, "task_id", "") or "")
            tree = self._task_goal_tree(task)
            goals = tree.all_goals() if tree is not None else []
            objectives = list(getattr(task, "objectives", ()) or ())
            first_goal = goals[0] if len(goals) == 1 else None
            if first_goal is None and len(objectives) == 1:
                first_goal = objectives[0]
            run_at = _first_goal_run_at(first_goal)
            draft_title = _task_draft_title(task)
            resource_refs = _task_resource_targets(task)
            execution_statuses = [
                str(getattr(item, "status", "") or "").upper()
                for item in (getattr(task, "execution_refs", ()) or ())
            ]
            created = str(getattr(task, "created_at", "") or "")
            updated = str(getattr(task, "updated_at", "") or "")
            label = str(getattr(task, "goal", "") or "")
            target_candidates.append({
                "id": task_id,
                "task_id": task_id,
                "kind": "TASK",
                "label": label,
                "run_at": run_at,
                "created_at": created,
                "updated_at": updated,
                "goals": [
                    {"goal_id": goal.goal_id, "description": goal.description}
                    for goal in goals
                ],
                "objectives": [
                    {
                        "objective_id": str(getattr(item, "objective_id", "")),
                        "description": str(getattr(item, "description", "") or ""),
                        "intent": str(getattr(item, "intent", "") or ""),
                    }
                    for item in objectives
                ],
                "resource_index": resource_refs,
                "metadata": {
                    "run_at": run_at,
                    "created_at": created,
                    "updated_at": updated,
                    "draft_title": draft_title,
                    "resource_refs": resource_refs,
                    "execution_statuses": execution_statuses,
                },
            })
            # Objective is the canonical cross-turn target.  Legacy Goals are
            # projected only when no matching Objective exists; emitting both
            # would manufacture an ambiguity for the same logical target.
            projected_ids: set[str] = set()
            for objective in objectives:
                objective_id = str(getattr(objective, "objective_id", "") or "")
                if not objective_id:
                    continue
                projected_ids.add(objective_id)
                constraints = dict(getattr(objective, "constraints", {}) or {})
                objective_status = str(
                    getattr(getattr(objective, "status", None), "value", None)
                    or getattr(objective, "status", "")
                    or ""
                ).upper()
                objective_execution_statuses = [
                    str(getattr(item, "status", "") or "").upper()
                    for item in (getattr(task, "execution_refs", ()) or ())
                    if str(getattr(item, "goal_id", "") or "") == objective_id
                ]
                if not objective_execution_statuses and len(objectives) == 1:
                    objective_execution_statuses = list(execution_statuses)
                related_resource_ids = [
                    str(value)
                    for value in (getattr(objective, "related_resource_ids", ()) or ())
                    if value
                ]
                target_candidates.append({
                    "id": objective_id,
                    "goal_id": objective_id,
                    "objective_id": objective_id,
                    "task_id": task_id,
                    "kind": "TASK",
                    # Keep the owning Task's business label alongside the
                    # objective label.  A natural-language failed-objective
                    # retry commonly names the failed Task ("写一篇 Java...")
                    # while the Objective description is the shorter topic
                    # ("Java...").  This is candidate evidence only; the
                    # resolver still requires exactly one FAILED objective.
                    "task_label": label,
                    "label": str(
                        getattr(objective, "description", "")
                        or getattr(objective, "intent", "")
                        or ""
                    ),
                    "status": objective_status,
                    "run_at": str(constraints.get("run_at") or ""),
                    "created_at": created,
                    "updated_at": str(
                        getattr(objective, "updated_at", "") or updated
                    ),
                    "metadata": {
                        "objective_id": objective_id,
                        "run_at": str(constraints.get("run_at") or ""),
                        "created_at": created,
                        "updated_at": str(
                            getattr(objective, "updated_at", "") or updated
                        ),
                        "draft_title": draft_title,
                        "resource_refs": resource_refs,
                        "execution_statuses": objective_execution_statuses,
                        "related_resource_ids": related_resource_ids,
                    },
                    "execution_statuses": objective_execution_statuses,
                    "related_resource_ids": related_resource_ids,
                })
            for goal in goals:
                goal_id = str(getattr(goal, "goal_id", "") or "")
                if not goal_id or goal_id in projected_ids:
                    continue
                target_candidates.append({
                    "id": goal_id,
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "kind": "TASK",
                    "label": goal.description,
                    "run_at": _goal_run_at(goal),
                    "created_at": created,
                    "updated_at": updated,
                    "metadata": {
                        "objective_id": goal_id,
                        "run_at": _goal_run_at(goal),
                        "created_at": created,
                        "updated_at": updated,
                        "draft_title": draft_title,
                        "resource_refs": resource_refs,
                    },
                })
        resolve_delta = getattr(resolver, "resolve_task_delta", None)
        if not callable(resolve_delta):
            return (None, None) if return_resolution else None
        resolution = resolve_delta(
            delta,
            target_candidates,
            active_task_id=str(getattr(session, "active_task_id", "") or ""),
            conversation_focus_task_id=str(
                getattr(session, "conversation_focus_task_id", "") or ""
            ),
            user_input=user_input,
        )
        if not resolution.is_resolved or resolution.target is None:
            # Multiple or missing candidates are a clarification boundary.
            # Never substitute persistence recency for a user reference.
            return (None, resolution) if return_resolution else None
        # Preserve the resolver's exact Objective/resource binding for the
        # subsequent mutation boundary.  This is structured propagation, not
        # another target decision; the ActionLoop will receive the same
        # canonical identity after the Task mutation is persisted.
        target_metadata = getattr(resolution.target, "metadata", {}) or {}
        objective_id = str(target_metadata.get("objective_id") or "")
        owner_task_id = str(
            resolution.target.task_id or resolution.target.id or ""
        )
        task = next(
            (task for task in candidates if str(task.task_id) == owner_task_id),
            None,
        )
        if task is not None and not objective_id:
            # A natural-language follow-up may carry only a typed label.  The
            # resolver has already grounded that label to one bounded
            # resource, so use its concrete id to recover the persisted
            # predecessor Objective before the new mutation Goal is appended.
            # Without this hand-off CANCEL_SCHEDULE reaches ActionLoop with
            # the right schedule id but no target_objective_id and is rejected
            # as an ownership mismatch.
            resource_id = _delta_resource_id(delta) or str(
                getattr(resolution.target, "resource_id", "") or ""
            )
            if resource_id:
                objective_id = _task_resource_owner_objective_id(
                    task,
                    resource_id,
                    _delta_resource_kind(delta),
                )
        if objective_id:
            desired = dict(delta.desired_changes or {})
            # ``target_objective_id`` identifies the historical Objective used
            # to ground a natural reference.  ``objective_id`` is retained as
            # the action owner until the Objective-first runtime creates the
            # new cross-turn mutation Objective.
            desired.setdefault("target_objective_id", objective_id)
            desired.setdefault("objective_id", objective_id)
            delta.desired_changes = desired
            reference = dict(delta.target_reference or {})
            reference.setdefault("target_objective_id", objective_id)
            reference.setdefault("objective_id", objective_id)
            delta.target_reference = reference
        record_focus = getattr(session, "record_conversation_focus", None)
        if task is not None and callable(record_focus):
            record_focus(
                str(getattr(task, "task_id", "") or ""),
                label=str(getattr(resolution.target, "label", "") or "") or None,
            )
        return (task, resolution) if return_resolution else task

    async def _latest_goal_tree_for_observation(self, observation: ActionObservation) -> GoalTree | None:
        """Latest persistent desired state for an observation's Task, if any.

        If the Task no longer exists or has no GoalTree snapshot, return None
        so the caller falls back to the observation's historical snapshot.
        """
        manager = self._task_manager
        task_id = str(getattr(observation, "task_id", "") or "")
        if manager is None or not task_id:
            return None
        session_payload = (observation.payload or {}).get("session") or {}
        if not isinstance(session_payload, Mapping):
            session_payload = {}
        try:
            task = await manager.get_task(
                task_id,
                conversation_id=observation.conversation_id,
                user_id=str(session_payload.get("user_id") or ""),
                tenant_id=str(session_payload.get("tenant_id") or ""),
            )
        except Exception:
            return None
        return self._task_goal_tree(task)

    def _task_goal_tree(self, task: Any) -> GoalTree | None:
        snapshot = getattr(task, "goal_tree_snapshot", None) or {}
        if not snapshot:
            return None
        try:
            return GoalTree.model_validate(snapshot)
        except Exception:
            return None

    def _apply_goal_delta(self, tree: GoalTree, delta: TaskDelta) -> GoalTree:
        operation = delta.operation
        # The business operation vocabulary is authoritative at the boundary
        # between Task desired state and a Java side effect.  Do this before
        # examining the TaskDelta lifecycle verb so an interpreter's
        # ``CANCEL_TASK`` label cannot turn ``CANCEL_SCHEDULE`` into a local
        # task cancellation.
        if _semantic_action_for_delta(delta):
            return _append_business_action_goal(tree, delta)
        if operation == TaskDeltaOperation.ADD_GOAL:
            return _append_delta_goal(tree, delta)
        # A canonical business action is represented as a new, durable action
        # Goal.  In particular, ``CANCEL_SCHEDULE`` must not remove the Goal
        # or cancel the Task before Java has confirmed the schedule's state.
        # The existing Goal remains a long-lived anchor for its Draft and
        # Schedule resources; AgentLoop executes the appended action Goal.
        if operation == TaskDeltaOperation.UPDATE_GOAL:
            return _patch_delta_goal(tree, delta)
        if operation == TaskDeltaOperation.CANCEL_GOAL:
            return _cancel_delta_goal(tree, delta)
        return tree

    async def _resume_task_if_paused(self, manager: Any, task: Any) -> None:
        if str(getattr(task, "status", "") or "") in {"PAUSED", "WAITING_HUMAN"}:
            await manager.resume_task(task.task_id)

    def _merge_mutation_results(
        self,
        results: Sequence[RuntimeResult],
        mutations: Sequence[dict[str, Any]],
    ) -> RuntimeResult:
        first = results[0] if results else None
        partial: dict[str, Any] = {"task_delta": list(mutations)}
        if first is not None:
            for key in ("first_capability", "task_ids", "execution_ids"):
                value = (first.partial_results or {}).get(key)
                if value:
                    partial.setdefault(key, value)
            if not partial.get("first_capability"):
                partial["first_capability"] = (
                    first.partial_results or {}
                ).get("first_capability")
            content = getattr(first, "content", "") or ""
            # Parallel task deltas resume each affected Task concurrently; the
            # merged Run reflects the whole fan-out, not just the first child.
            # Any child still working keeps the Run RUNNING so observations can
            # resume it; a FAILED child fails the Run; only when every child is
            # COMPLETED is the Run COMPLETED (design reference: nanobot).
            statuses = {
                str(getattr(item, "status", "") or "").upper()
                for item in results
                if item is not None
            }
            if "FAILED" in statuses:
                status = "FAILED"
            elif "CANCELLED" in statuses:
                status = "CANCELLED"
            elif statuses and statuses <= {"COMPLETED"}:
                status = "COMPLETED"
            else:
                status = "RUNNING"
            success = status == "COMPLETED"
            error_code = ""
            error_message = ""
            for item in results:
                if item is None:
                    continue
                error_code = error_code or getattr(item, "error_code", "") or ""
                error_message = error_message or getattr(item, "error_message", "") or ""
        else:
            content = "已处理当前对话中的任务变更。"
            success = True
            status = "COMPLETED"
            error_code = ""
            error_message = ""
        return RuntimeResult(
            success=success,
            status=status,
            run_id=first.run_id if first else "",
            trace_id=first.trace_id if first else "",
            execution_path="task_delta",
            content=content,
            error_code=error_code,
            error_message=error_message,
            partial_results=partial,
        )

    def _bootstrap_action(self, command: Command) -> AgentAction | None:
        """Return a validated first action for SIMPLE requests, else None.

        The understanding output is only a bootstrap hint: the first action
        must be one of the Command's own required capabilities (semantic
        monotonicity) and must resolve to exactly one catalog tool. Any
        mismatch upgrades the request to the full COMPLEX path instead of
        guessing.

        Two deterministic guards keep the bootstrap from emitting a tool call
        that cannot execute:
        * a Goal with more than one required capability (search → summarize →
          write → schedule) is never SIMPLE — it goes through the full
          decompose/reason path so every step gets its real arguments;
        * a single-capability bootstrap must be able to derive the tool's
          required arguments (e.g. SEARCH_COMMUNITY needs ``query``) from the
          structured Command; otherwise fall back to the full path where the
          model supplies the arguments.
        """
        if str(command.request_complexity or "").upper() != "SIMPLE":
            return None
        first = str(command.first_action or "").upper()
        if not first:
            return None
        required = {str(item).upper() for item in command.required_capabilities}
        if first not in required:
            return None
        if len(command.required_capabilities) > 1:
            # Multi-step goals must be planned, not bootstrapped: the
            # bootstrap tree has a single node and would skip every later
            # capability (observed: a search+summarize+write+schedule request
            # executed only the search step).
            return None
        candidates = [
            item
            for item in self._available_tool_metadata()
            if first in {str(capability).upper() for capability in getattr(item, "capabilities", ())}
        ]
        if len(candidates) != 1:
            return None
        tool_name = str(candidates[0].name)
        tool_args = self._bootstrap_tool_args(command, tool_name, first)
        if tool_args is None:
            return None
        return AgentAction(
            action=AgentActionType.TOOL_CALL,
            tool_name=tool_name,
            tool_args=tool_args,
            reason=f"Phase 3.5 bootstrap: first action {first} from understanding.",
        )

    def _bootstrap_tool_args(
        self,
        command: Command,
        tool_name: str,
        capability: str,
    ) -> dict[str, Any] | None:
        """Derive a single-capability bootstrap's tool arguments, or None.

        The runtime never invents a query/title: it only uses structured
        values the model already emitted (entities/parameters), and falls
        back to the full reasoning path when none exist.
        """
        entities = command.entities if isinstance(command.entities, Mapping) else {}
        parameters = command.parameters if isinstance(command.parameters, Mapping) else {}
        if capability == "SEARCH_COMMUNITY":
            query = (
                str(entities.get("topic") or entities.get("query") or parameters.get("query") or "")
            ).strip()
            if not query:
                return None
            return {"query": query}
        if capability == "GENERATE_CONTENT":
            target_title = (
                getattr(command.target, "title", None)
                if command.target is not None
                else None
            )
            title = str(
                entities.get("title")
                or parameters.get("title")
                or target_title
                or ""
            ).strip()
            instruction = str(
                entities.get("instruction") or parameters.get("instruction") or command.requested_goal
            ).strip()
            if not title or not instruction:
                return None
            return {"title": title, "instruction": instruction}
        return {}

    @staticmethod
    def _bootstrap_goal_tree(command: Command, action: AgentAction) -> GoalTree:
        """Deterministically wrap a SIMPLE Command into a single-Goal tree."""
        goal_id = "bootstrap-goal-1"
        capability = next(
            (capability for capability in command.required_capabilities
             if str(capability).upper() == str(command.first_action or "").upper()),
            str(command.required_capabilities[0]) if command.required_capabilities else "",
        )
        return GoalTree(
            root=Goal(
                goal_id=goal_id,
                description=command.requested_goal,
                goal_type="TASK",
                required_capabilities=list(dict.fromkeys(command.required_capabilities)),
                constraints=[dict(command.constraints)] if command.constraints else [],
                semantic_operation=command.semantic_operation,
                target=command.target.model_dump(mode="json") if command.target else {},
                expected_outputs=[command.requested_goal] if command.requested_goal else [],
            ),
            task_nodes=[
                TaskNode(
                    task_id="bootstrap-task-1",
                    goal_id=goal_id,
                    capability=capability,
                )
            ],
            command_id=command.command_id,
            source="COMMAND_BOOTSTRAP",
        )

    async def _run_agent_loop(
        self,
        *,
        command: Command,
        goal_tree: GoalTree,
        bootstrap_action: Any = None,
        context_snapshot: ContextSnapshot,
        request_session: SessionContext | Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        mcp: Any,
        llm: Any,
        model: str,
        auth: Any,
        detach: bool,
        completion_callback: Any,
        resume_context: ResumeContext | None = None,
        activity_callback: Any = None,
        allow_fanout: bool = True,
        existing_task: Task | None = None,
        mutation_applied: Sequence[Mapping[str, Any]] | None = None,
    ) -> RuntimeResult:
        del detach

        # A valid structured decomposer response may omit optional TaskNodes.
        # Materialize them before binding the durable Task: AgentLoop and its
        # result recorders need these identities to project facts to a Goal.
        materialize = getattr(self._goal_compiler, "materialize_task_nodes", None)
        if callable(materialize):
            goal_tree = materialize(goal_tree)

        if allow_fanout:
            ready = self._ready_goals_for_fanout(
                goal_tree,
                context_snapshot,
            )
            if len(ready) > 1:
                return await self._run_ready_goals_concurrently(
                    ready=ready,
                    command=command,
                    context_snapshot=context_snapshot,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    mcp=mcp,
                    llm=llm,
                    model=model,
                    auth=auth,
                    completion_callback=completion_callback,
                    activity_callback=activity_callback,
                    resume_context=resume_context,
                )

        if existing_task is not None:
            durable_task = existing_task
        else:
            durable_task = await self._bind_task(
                command=command,
                goal_tree=goal_tree,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                session=request_session,
                existing_task_id=str(
                    getattr(resume_context, "task_id", "") or ""
                ),
            )

        def task_id_for(state: Any, plan: Any) -> str:
            return str(
                getattr(getattr(state, "task", None), "task_id", "")
                or getattr(plan, "task_id", "")
                or getattr(getattr(state, "goal", None), "goal_id", "")
            )

        def runtime_context_for(state: Any, plan: Any) -> RuntimeContext:
            task_id = task_id_for(state, plan)
            root_goal = getattr(state, "goal", None)
            # Incremental executions carry one current Goal. Preserve its
            # semantic owner across the queue boundary so Durable Runtime can
            # bind Objective-owned resources and project completion back to
            # that same Goal.
            objective_id = str(
                getattr(getattr(state, "current_task", None), "goal_id", "")
                or (
                    getattr(getattr(plan, "steps", [None])[0], "goal_id", "")
                    if getattr(plan, "steps", None)
                    else ""
                )
                or ""
            ) or None
            description = str(
                getattr(root_goal, "description", "")
                or getattr(root_goal, "goal_type", "")
                or "Goal execution"
            )
            execution_input = ExecutionInput.from_executable_plan(
                task_id=task_id,
                plan=plan,
                executable=plan,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                goal_id=objective_id,
                goal=description,
                goal_category="COMPOSITE",
                execution_metadata={
                    "goal_tree": (
                        state.goal_tree.model_dump(mode="json")
                        if getattr(state, "goal_tree", None) is not None
                        else {}
                    ),
                    "context_snapshot_id": getattr(state, "context_snapshot_id", ""),
                    "plan_mode": (
                        "INCREMENTAL"
                        if str(getattr(plan, "plan_source", "")) == INCREMENTAL_PLAN_SOURCE
                        else "WHOLE_PLAN"
                    ),
                    "command": (
                        state.command.model_dump(mode="json")
                        if getattr(state, "command", None) is not None
                        else {}
                    ),
                },
                policy_catalog=self._tool_registry,
                trace_context={
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "trace_id": trace_id,
                },
            )
            task_context = TaskContext(
                task_id=task_id,
                goal=description,
                execution_input=execution_input,
                constraints=tuple(
                    {str(key): value for key, value in getattr(step, "constraints", {}).items()}
                    for step in getattr(plan, "steps", ())
                ),
                artifact_refs=tuple(),
            )
            return RuntimeContext(
                conversation_id=conversation_id,
                run_id=run_id,
                trace_id=trace_id,
                task_id=task_id,
                task_context=task_context,
                execution_input=execution_input,
                user_id=user_id,
                tenant_id=tenant_id,
                timezone=getattr(request_session, "timezone", "Asia/Shanghai"),
                objective_id=objective_id,
                user_message=description,
                conversation_context=context_snapshot.decision_payload(),
                session=request_session,
                # The Worker cannot safely recover a business target from a
                # conversation-global session.  Carry the already task-scoped
                # bindings into the durable RuntimeContext explicitly.
                active_artifact_id=getattr(request_session, "active_artifact_id", None),
                active_draft_id=getattr(request_session, "active_draft_id", None),
                active_schedule_id=getattr(request_session, "active_schedule_id", None),
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
            )

        async def submit_plan(*, graph: Any, plan: Any, state: Any) -> Mapping[str, Any]:
            del graph
            plan = _incremental_plan(state, plan)
            _inject_reasoning_context(state, plan)
            self._require_new_request_incremental_plan(plan)
            if str(getattr(plan, "plan_source", "")) == INCREMENTAL_PLAN_SOURCE:
                deduplicated = _find_incremental_submission(
                    self._execution_repository,
                    plan,
                )
                if deduplicated is not None:
                    return deduplicated
            # A reasoning-backed capability (ANALYZE_CONTENT_PATTERNS, etc.)
            # must be produced inside AgentLoop (PRODUCE_RESULT); the Worker
            # rejects it with WRONG_EXECUTION_SEMANTICS.  Intercept before the
            # durable queue so the loop can re-decide instead of failing a
            # queued execution (observed live: second task's ANALYZE failed in
            # the worker under the multi-task parallel path).
            reasoning = _first_reasoning_step(
                plan,
                getattr(self._container, "capability_registry", None),
            )
            if reasoning is not None:
                return {
                    "ok": False,
                    "code": "REASONING_STEP_NOT_SUBMITTABLE",
                    "message": (
                        f"Reasoning-backed capability '{reasoning}' must be "
                        "produced in AgentLoop via PRODUCE_RESULT, not queued."
                    ),
                    "user_message": "这一步需要先在对话中完成分析，正在重新处理。",
                    "retryable": False,
                    "request_sent": False,
                    "state": {
                        "phase": "PRE_EXECUTION_VALIDATION_FAILED",
                        "downstream_called": False,
                        "side_effect_started": False,
                        "safe_to_retry": True,
                        "reasoning_capability": reasoning,
                    },
                    "trace_id": trace_id,
                }
            submit = getattr(self._runtime_service, "submit_plan", None)
            if not callable(submit):
                raise RuntimeError(
                    "RuntimeAgentService.submit_plan is required for queue-native execution."
                )
            self._require_plan_goal_coverage(
                plan,
                getattr(state, "goal_tree", None),
            )
            try:
                from greenbook_agent_core.observability.run_metrics import record_stage
                record_stage("execution_submit_start", run_id=run_id)
            except Exception:
                pass
            result = await submit(
                runtime_context_for(state, plan),
                plan,
                completion_callback=completion_callback,
            )
            try:
                from greenbook_agent_core.observability.run_metrics import record_stage
                record_stage("execution_submitted", run_id=run_id)
            except Exception:
                pass
            return self._mapping_result(result)

        async def submit_tool(
            *,
            tool_name: str,
            arguments: dict[str, Any],
            state: Any,
        ) -> Mapping[str, Any]:
            """Queue a selected side-effect Tool using its metadata contract."""

            metadata = next(
                (
                    item
                    for item in self._available_tool_metadata()
                    if str(getattr(item, "name", "")) == tool_name
                ),
                None,
            )
            if metadata is None:
                raise TaskProviderError(
                    "TOOL_METADATA_REQUIRED",
                    f"No ToolMetadata exists for '{tool_name}'.",
                )
            capabilities = tuple(getattr(metadata, "capabilities", ()) or ())
            if len(capabilities) > 1:
                raise TaskProviderError(
                    "TOOL_CAPABILITY_AMBIGUOUS",
                    f"Tool '{tool_name}' declares multiple semantic capabilities.",
                )
            capability = str(
                next(iter(capabilities), "")
                or getattr(getattr(state, "current_task", None), "capability", "")
            )
            if not capability:
                raise TaskProviderError(
                    "TOOL_CAPABILITY_REQUIRED",
                    f"Tool '{tool_name}' has no semantic capability contract.",
                )
            executable_goal_ids = self._executable_goal_ids(
                getattr(state, "goal_tree", None),
            )
            current_goal_id = str(
                getattr(getattr(state, "current_task", None), "goal_id", "")
                or ""
            )
            if not current_goal_id and len(executable_goal_ids) == 1:
                current_goal_id = next(iter(executable_goal_ids))
            task_id = task_id_for(state, state.goal_tree)
            plan = TaskPlan(
                task_id=task_id,
                plan_source="AGENT_TOOL_SUBMISSION",
                steps=[
                    PlanStep(
                        step_id=f"agent-tool-{state.iteration}",
                        ordinal=1,
                        capability=capability,
                        tool_name=tool_name,
                        description=f"Agent selected {tool_name}",
                        constraints=dict(arguments),
                        # The direct-tool path remains valid for exactly one
                        # logical Goal. Multi-goal side effects are promoted
                        # by AgentLoop to GoalCompiler; if a caller bypasses
                        # that guard, the submission coverage check below
                        # rejects this incomplete one-step plan.
                        goal_id=current_goal_id or None,
                    )
                ],
            )
            result = dict(await submit_plan(graph=None, plan=plan, state=state))
            execution_id = str(result.get("execution_id") or "")
            current_task = getattr(state, "task", None)
            bind_execution = getattr(self._task_manager, "bind_execution", None)
            # A replayed incremental plan already has a durable Execution.
            # Rebinding it mutates Task execution_refs on every continuation
            # and can make a completed action look like fresh work.
            if (
                execution_id
                and not bool(result.get("deduplicated"))
                and current_task is not None
                and callable(bind_execution)
            ):
                updated = bind_execution(
                    current_task.task_id,
                    execution_id,
                    goal_id=current_goal_id or None,
                    status=str(result.get("status") or "SUBMITTED"),
                )
                updated = await updated if inspect.isawaitable(updated) else updated
                state.task = updated
            return result

        queue_submission = QueueExecutionSubmissionService(submit_plan)

        async def raw_tool_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
            if mcp is None or not callable(getattr(mcp, "execute_tool", None)):
                return {
                    "ok": False,
                    "code": "MCP_RUNTIME_UNAVAILABLE",
                    "message": "MCP execution boundary is unavailable.",
                    "retryable": False,
                }
            async with self._direct_tool_semaphore:
                result = await mcp.execute_tool(
                    tool_name,
                    auth=auth,
                    session=request_session,
                    trace_id=trace_id,
                    agent_run_id=run_id,
                    tool_call_id=str(uuid.uuid4()),
                    **tool_args,
                )
            return result if isinstance(result, dict) else {
                "ok": False,
                "code": "TOOL_RESULT_INVALID",
                "message": "Tool Runtime returned an invalid result.",
            }

        async def emit_activity(
            event_type: str,
            payload: Mapping[str, Any],
        ) -> None:
            """Attach durable Task ownership to every business activity."""

            if activity_callback is None:
                return
            owned = dict(payload)
            if durable_task is not None:
                owned["task_id"] = durable_task.task_id
            emitted = activity_callback(event_type, owned)
            if inspect.isawaitable(emitted):
                await emitted

        result = await self._agent_loop.run(
            command,
            goal_tree,
            conversation_context=context_snapshot,
            available_tools=self._available_tool_metadata(),
            memory_snapshot=context_snapshot.decision_payload(),
            tool_runtime=ToolRuntime(
                raw_tool_handler,
                ledger=ToolExecutionLedger(),
            ),
            tool_submission=submit_tool,
            execution_submission=queue_submission,
            task_manager=self._task_manager,
            task=durable_task,
            execution_runtime=queue_submission,
            context_builder=self._context_builder,
            resume_context=resume_context,
            activity_callback=emit_activity,
            bootstrap_action=bootstrap_action,
            reasoning_result_recorder=self.record_reasoning_result,
            tool_result_recorder=self.record_tool_result,
            context_scope={
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "timezone": getattr(request_session, "timezone", "Asia/Shanghai"),
                "session": request_session,
            },
            llm=llm,
            model=model,
        )
        runtime_result = self._agent_loop_result(
            result,
            run_id=run_id,
            trace_id=trace_id,
        )
        if not runtime_result.task_id and durable_task is not None:
            runtime_result.task_id = str(durable_task.task_id)
        if runtime_result.status in {"WAITING_HUMAN", "WAITING_APPROVAL"}:
            wait_for_human = getattr(self._task_manager, "wait_for_human", None)
            if durable_task is not None and callable(wait_for_human):
                # Pause only the Goal that actually needs human input;
                # independent sibling Goals keep running (design goal 0813).
                state = getattr(result, "state", None)
                waiting_goal_id = str(
                    getattr(getattr(state, "current_task", None), "goal_id", "")
                    or getattr(getattr(state, "goal", None), "goal_id", "")
                )
                await wait_for_human(
                    durable_task.task_id,
                    reason=runtime_result.error_message or runtime_result.content,
                    goal_id=waiting_goal_id,
                )
                partial_results = dict(runtime_result.partial_results or {})
                task_ids = list(partial_results.get("task_ids") or [])
                if durable_task.task_id not in task_ids:
                    task_ids.append(durable_task.task_id)
                partial_results["task_ids"] = task_ids
                runtime_result.partial_results = partial_results
        tool_results = (runtime_result.partial_results or {}).get("tool_results") or []
        capabilities = {str(item).upper() for item in command.required_capabilities}
        semantic_operation = str(
            getattr(command, "semantic_operation", "")
            or getattr(getattr(command, "resolved_semantics", None), "semantic_operation", "")
            or ""
        ).upper()
        synthesis_requested = (
            "ANALYZE_CONTENT_PATTERNS" in capabilities
            or semantic_operation in {"SUMMARIZE", "SUMMARIZE_POST", "SUMMARIZE_CONTENT"}
        )
        interaction, safe_message = await build_retrieval_interaction(
            request=command.raw_input or command.requested_goal,
            tool_results=tool_results,
            synthesis_requested=synthesis_requested,
            llm=llm,
            model=model,
        )
        if interaction is not None:
            partial_results = dict(runtime_result.partial_results or {})
            partial_results["user_facing_interaction"] = interaction
            runtime_result.partial_results = partial_results
            # Reflection is an internal control signal. Keep only the short
            # business summary in the compatibility response and let the
            # structured interaction carry the actual user-facing result.
            runtime_result.content = safe_message
            runtime_result.summary = safe_message
        return runtime_result

    def _ready_goals_for_fanout(
        self,
        goal_tree: GoalTree,
        context_snapshot: ContextSnapshot,
    ) -> list[Any]:
        """Return independent ready Goals; action choice stays in AgentLoop."""

        execution_states = list(
            getattr(context_snapshot, "execution_states", ()) or ()
        )
        facts = _facts_from_execution_states(execution_states)
        in_flight = {
            goal_id
            for goal_id, values in facts.items()
            if str(values.get("status") or "").upper()
            in {
                "QUEUED",
                "SUBMITTED",
                "RUNNING",
                "IN_PROGRESS",
                "RESULT_UNKNOWN",
                "VERIFYING_RESULT",
                "RECONCILING",
                "FAILED_RETRYABLE",
                "RETRYABLE",
                "RETRYING",
            }
        }
        active_work: list[Any] = []
        list_all = getattr(self._execution_repository, "list_all", None)
        if callable(list_all):
            try:
                active_work = list_all() or []
            except Exception:
                # A failed advisory read must not make the selector invent a
                # safe parallelism decision; AgentLoop will handle the normal
                # single-goal path and the queue guard remains fail-closed.
                logger.warning("Ready-work resource lookup failed", exc_info=True)
                active_work = [object()]
        return [
            item.goal
            for item in select_ready_work(
                goal_tree,
                facts,
                in_flight_goal_ids=in_flight,
                active_work=active_work,
                limit=self._max_concurrent_work_per_conversation,
            )
        ]

    def _claim_message_idempotency(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        message: str,
        idempotency_key: str,
        run_id: str,
    ) -> str:
        """Return the previously-accepted run_id for a duplicate, else ``run_id``.

        Fingerprint = explicit Idempotency-Key when provided, otherwise the
        normalized (conversation, user, message) triple.  The cache is bounded
        and TTL-based; the durable Execution queue and per-Task locks remain
        the authoritative cross-process guards.
        """
        fingerprint = (
            str(idempotency_key).strip()
            or f"{conversation_id}:{user_id}:{tenant_id}:{_message_fingerprint(message)}"
        )
        key = f"{conversation_id}:{user_id}:{tenant_id}:{fingerprint}"
        if len(self._recent_message_keys) >= self._recent_message_cap:
            self._recent_message_keys.clear()
        prior = self._recent_message_keys.get(key)
        if prior is not None:
            return prior
        self._recent_message_keys[key] = run_id
        return run_id

    def _semaphore_for_conversation(self, conversation_id: str) -> asyncio.Semaphore:
        """Return the per-conversation work semaphore, bounded by LRU eviction.

        The dict is capped so a long-running API cannot leak one entry per
        conversation forever; once the cap is exceeded the least-recently-used
        entry is dropped.  A dropped semaphore only affects an idle
        conversation's concurrency budget — the durable Execution queue and
        the per-Task continuation locks remain the real concurrency guards.
        """
        semaphore = self._conversation_work_semaphores.get(conversation_id)
        if semaphore is None:
            if len(self._conversation_work_semaphores) >= self._conversation_semaphore_cap:
                # Evict the oldest entry (dict preserves insertion order).
                self._conversation_work_semaphores.pop(
                    next(iter(self._conversation_work_semaphores)),
                    None,
                )
            semaphore = asyncio.Semaphore(self._max_concurrent_work_per_conversation)
            self._conversation_work_semaphores[conversation_id] = semaphore
        else:
            # Refresh recency.
            self._conversation_work_semaphores.pop(conversation_id, None)
            self._conversation_work_semaphores[conversation_id] = semaphore
        return semaphore

    async def _run_ready_goals_concurrently(
        self,
        *,
        ready: Sequence[Any],
        command: Command,
        context_snapshot: ContextSnapshot,
        request_session: SessionContext | Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        mcp: Any,
        llm: Any,
        model: str,
        auth: Any,
        completion_callback: Any,
        activity_callback: Any,
        resume_context: ResumeContext | None = None,
    ) -> RuntimeResult:
        """Run independent Goal loops together and merge only presentation."""

        conversation_semaphore = self._semaphore_for_conversation(conversation_id)

        async def run_one(goal: Any) -> RuntimeResult:
            async with conversation_semaphore:
                child_goal = goal.model_copy(deep=True)
                # Readiness has already proved these dependencies; keeping the
                # child GoalTree self-contained lets the child continuation carry
                # only its own ownership without reintroducing a planner.
                child_goal.dependencies = []
                child_tree = GoalTree(
                    root=child_goal,
                    command_id=command.command_id,
                    source="READY_WORK_FANOUT",
                    version=1,
                )
                child_command = command.model_copy(deep=True)
                if child_goal.required_capabilities:
                    child_command.required_capabilities = list(
                        child_goal.required_capabilities
                    )
                child_resume = None
                if resume_context is not None:
                    child_resume = resume_context.model_copy(deep=True)
                    child_resume.goal_states = [
                        item
                        for item in resume_context.goal_states
                        if str(item.get("goal_id") or "") == str(child_goal.goal_id)
                    ]
                return await self._run_agent_loop(
                    command=child_command,
                    goal_tree=child_tree,
                    context_snapshot=context_snapshot,
                    request_session=request_session,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    mcp=mcp,
                    llm=llm,
                    model=model,
                    auth=auth,
                    detach=False,
                    completion_callback=completion_callback,
                    resume_context=child_resume,
                    activity_callback=activity_callback,
                    allow_fanout=False,
                )

        results = await asyncio.gather(*(run_one(goal) for goal in ready))
        return _merge_parallel_runtime_results(results, run_id=run_id, trace_id=trace_id)

    async def continue_run(
        self,
        *,
        observation: ActionObservation,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None = None,
        trace_id: str | None = None,
        mcp: Any = None,
        llm: Any = None,
        model: str = "",
        auth: Any = None,
        activity_callback: Any = None,
    ) -> RuntimeResult:
        """Resume AgentLoop with one durable ActionObservation as evidence.

        The GoalTree and Command come from the observation payload (persisted
        at submission time), never from re-interpreting the user message. The
        business result (draft_id, schedule_id, artifacts) is merged into the
        context snapshot so AgentLoop observes what actually happened and
        decides the next semantic action itself.
        """
        run = run_id or str(uuid.uuid4())
        trace = trace_id or str(uuid.uuid4())
        if self._agent_loop is None:
            raise TaskProviderError(
                "CONTINUATION_AGENT_LOOP_UNAVAILABLE",
                "AgentLoop is required to continue an observation.",
            )
        # Phase 4.1: the authoritative desired state is the Task's latest
        # persistent GoalTree, not the historical snapshot carried by an old
        # Execution/Observation. A stale observation updates actual state; it
        # must never drive completion from the old desired state.
        goal_tree = await self._latest_goal_tree_for_observation(observation)
        if goal_tree is None:
            goal_tree = GoalTree.model_validate(observation.payload.get("goal_tree") or {})
        command_payload = observation.payload.get("command") or {}
        command = (
            Command.model_validate(command_payload)
            if command_payload
            else _command_from_tree(goal_tree)
        )
        session_payload = observation.payload.get("session") or {}
        request_session = self._coerce_session(
            session_payload,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=str(session_payload.get("timezone") or "Asia/Shanghai"),
        )
        context = await self._build_context_snapshot(
            request_session,
            history=None,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            current_command=command,
        )
        context = _merge_observation_evidence(context, observation)
        completed_step_id = _completed_step_id(goal_tree, observation)
        # Per-Goal satisfaction derived from the durable business facts the
        # observation injected; travels with the resume state so the per-round
        # context refresh cannot erase it.
        goal_states_projection = goal_states(
            goal_tree,
            _facts_from_execution_states(
                list(getattr(context, "execution_states", []) or [])
            ),
        )
        return await self._run_agent_loop(
            command=command,
            goal_tree=goal_tree,
            context_snapshot=context,
            request_session=request_session,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            run_id=run,
            trace_id=trace,
            mcp=mcp,
            llm=llm,
            model=model,
            auth=auth,
            detach=False,
            completion_callback=None,
            activity_callback=activity_callback,
            resume_context=ResumeContext(
                task_id=observation.task_id,
                execution_id=observation.execution_id,
                completed_step_ids=[completed_step_id] if completed_step_id else [],
                artifacts=[
                    item
                    for item in observation.resource_refs
                    if item.get("resource_id")
                ],
                goal_states=goal_states_projection,
            ),
        )

    async def _bind_task(
        self,
        *,
        command: Command,
        goal_tree: GoalTree,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        session: SessionContext | Any | None = None,
        current_command: Command | None = None,
        current_goal: Any | None = None,
        existing_task_id: str = "",
    ) -> Task | None:
        manager = self._task_manager
        if manager is None:
            return None
        if existing_task_id:
            existing = await manager.get_task(
                existing_task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            if existing is None:
                raise TaskProviderError(
                    "TASK_TARGET_NOT_FOUND",
                    "The observation Task is outside the authenticated scope.",
                )
            return existing
        target_task_id = str(
            getattr(getattr(command, "target", None), "task_id", "")
            or (getattr(command, "resolved_target", None) or {}).get("task_id", "")
        )
        command_type = command.type.value
        # A clarification turn can omit the original Task id even though the
        # conversation has one explicit active binding.  Reuse that binding
        # for a MODIFY/CANCEL command; never apply it to a new task request.
        # Phase 4.2: MODIFY/CANCEL mutation is served exclusively by TaskDelta
        # (execute() rejects delta-less mutation with MUTATION_REQUIRES_DELTA),
        # so _bind_task never rebuilds an existing Task's GoalTree for a new
        # mutation request. Only CREATE + SCHEDULE_PUBLISH follow-up appends a
        # schedule Goal to the draft's Task.
        schedule_followup = (
            command_type == "CREATE"
            and "SCHEDULE_PUBLISH" in {
                str(item).upper() for item in command.required_capabilities
            }
            and bool(getattr(session, "active_draft_id", None))
        )
        if schedule_followup and not target_task_id:
            target_task_id = str(getattr(session, "active_task_id", "") or "")
        if target_task_id and schedule_followup:
            task = await manager.get_task(
                target_task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            if task is None:
                raise TaskProviderError(
                    "TASK_TARGET_NOT_FOUND",
                    "The target Task is outside the authenticated scope.",
                )
            return await manager.bind_goal_tree(task.task_id, goal_tree)
        return await manager.create_task(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            root_goal=goal_tree.root_goal,
            goal_tree=goal_tree,
        )

    @staticmethod
    def _apply_active_resource_binding(
        command: Command,
        session: SessionContext | Any,
    ) -> None:
        """Make an explicit active draft available to a schedule follow-up.

        The model may correctly understand ``明天晚上8点发布`` as a schedule
        operation while omitting the draft identity because it is already a
        durable conversation binding.  Carry that binding into the typed
        command so planning, Task reuse, and Tool argument compilation agree.
        This is structured context propagation, not text routing.
        """

        active_draft_id = str(getattr(session, "active_draft_id", "") or "")
        active_task_id = str(getattr(session, "active_task_id", "") or "")
        capabilities = {str(item).upper() for item in command.required_capabilities}
        if not active_draft_id or not capabilities.intersection(
            {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}
        ):
            return

        values: dict[str, Any] = {}
        for source_name in ("parameters", "entities", "constraints"):
            source = getattr(command, source_name, {}) or {}
            if isinstance(source, Mapping):
                values.update({str(key).lower(): value for key, value in source.items()})
        if values.get("draft_id") or values.get("resource_id"):
            return

        command.entities.setdefault("draft_id", active_draft_id)
        command.parameters.setdefault("draft_id", active_draft_id)
        command.target = CommandTarget(
            kind=TargetKind.DRAFT,
            id=active_draft_id,
            resource_id=active_draft_id,
            task_id=active_task_id or None,
            reference_type=TargetReferenceType.ACTIVE,
        )
        command.target_resolution = TargetResolutionStatus.RESOLVED.value
        command.resolved_target = {
            "id": active_draft_id,
            "kind": TargetKind.DRAFT.value,
            "resource_id": active_draft_id,
            "task_id": active_task_id or None,
            "reference_type": TargetReferenceType.ACTIVE.value,
        }

    @staticmethod
    def _supports_partial_task_progress(command: Command) -> bool:
        """Allow structured multi-work requests to isolate clarification.

        The LLM has already declared the requested capability set and
        structured references.  This gate only decides whether decomposition
        may continue; GoalDecomposer still owns the task/goal split and each
        child remains responsible for its own target resolution.
        """

        capability_count = len(
            {str(item) for item in command.required_capabilities if str(item)}
        )
        action_values = command.entities.get("actions") if isinstance(command.entities, Mapping) else None
        action_count = len(action_values) if isinstance(action_values, Sequence) and not isinstance(action_values, (str, bytes)) else 0
        return capability_count > 1 or action_count > 1 or len(command.references) > 1

    def _available_tool_metadata(self) -> list[Any]:
        list_metadata = getattr(self._tool_registry, "list_tool_metadata", None)
        if callable(list_metadata):
            return list(list_metadata())
        list_method = getattr(self._tool_registry, "list", None)
        if callable(list_method):
            return list(list_method())
        return []

    async def get_task_index(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        scope = TaskScope(
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        tasks = await self._list_tasks(scope)
        return [
            {
                "task_id": task.task_id,
                "goal": task.goal,
                "status": str(task.status),
                "priority": task.priority,
                "goal_tree_version": task.goal_tree_version,
                "plan_version": task.plan_version,
                "active_execution_id": task.active_execution_id,
                "goals": [goal.model_dump(mode="json") for goal in task.goals],
                "execution_refs": [
                    ref.model_dump(mode="json") for ref in task.execution_refs
                ],
                "updated_at": task.updated_at,
            }
            for task in tasks
        ]

    async def _list_tasks(self, scope: TaskScope) -> list[Task]:
        list_tasks = getattr(self._task_provider, "list_tasks", None)
        if callable(list_tasks):
            values = list_tasks(scope)
            tasks = list(await values) if inspect.isawaitable(values) else list(values)
            return await self._filter_current_task_values(tasks, scope.conversation_id)
        manager = self._task_manager
        list_active = getattr(manager, "get_active_tasks", None)
        if callable(list_active):
            values = list_active(
                scope.conversation_id,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
            )
            tasks = list(await values) if inspect.isawaitable(values) else list(values)
            return await self._filter_current_task_values(tasks, scope.conversation_id)
        return []

    async def _filter_current_task_values(
        self,
        tasks: Sequence[Task],
        conversation_id: str,
    ) -> list[Task]:
        exhausted = await _conversation_has_exhausted_reconciliation(
            self._external_operation_store,
            conversation_id,
        )
        return [
            task
            for task in tasks
            if not is_context_isolated_task(task)
            and not (exhausted and _task_status_value(task) == "FAILED")
        ]

    async def _build_context_snapshot(
        self,
        session: SessionContext | Any,
        *,
        history: Sequence[Mapping[str, str]] | None,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        current_command: Command | None = None,
        current_goal: Any | None = None,
    ) -> ContextSnapshot:
        """Build one bounded snapshot for the whole turn.

        Task/Execution/Artifact joins live in ContextBuilder; this adapter no
        longer maintains a second context projection.
        """

        return await self._context_builder.build(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=str(getattr(session, "timezone", "Asia/Shanghai")),
            session=session,
            history=history,
            current_command=current_command,
            current_goal=current_goal,
            memory_recall=False,
        )

    @staticmethod
    def _require_resolved_target(command: Command) -> None:
        if command.target_resolution == TargetResolutionStatus.AMBIGUOUS.value:
            raise TaskProviderError(
                "AMBIGUOUS_TARGET",
                "The structured Command target is ambiguous.",
            )
        if command.target_resolution != TargetResolutionStatus.RESOLVED.value:
            raise TaskProviderError(
                "TASK_TARGET_NOT_FOUND",
                "The structured Command target could not be resolved.",
            )

    @staticmethod
    def _executable_goal_ids(goal_tree: GoalTree | Any | None) -> tuple[str, ...]:
        executable = getattr(goal_tree, "executable_goals", None)
        if not callable(executable):
            return ()
        return tuple(
            str(getattr(goal, "goal_id", ""))
            for goal in executable()
            if str(getattr(goal, "goal_id", ""))
        )

    @classmethod
    def _require_plan_goal_coverage(cls, plan: Any, goal_tree: GoalTree | Any | None) -> None:
        """Reject durable work that omits an executable logical Goal.

        GoalTree is the canonical semantic cardinality source. This guard is
        intentionally at the last boundary before RuntimeAgentService creates
        an Execution so a partial LLM action cannot become a misleading
        successful one-step run.

        An INCREMENTAL plan intentionally covers exactly one current Goal's
        next action; the guard then only rejects steps that reference a Goal
        outside the tree (ownership isolation), never the tree's other
        pending Goals — they remain the AgentLoop's to satisfy later.
        """

        expected = set(cls._executable_goal_ids(goal_tree))
        if not expected:
            return
        if str(getattr(plan, "plan_source", "")) == INCREMENTAL_PLAN_SOURCE:
            for step in getattr(plan, "steps", ()):
                step_goal_id = str(getattr(step, "goal_id", ""))
                if step_goal_id and step_goal_id not in expected:
                    raise TaskProviderError(
                        "PLAN_GOAL_COVERAGE_REQUIRED",
                        "An incremental step references a Goal outside the GoalTree.",
                    )
            return
        covered = {
            str(getattr(step, "goal_id", ""))
            for step in getattr(plan, "steps", ())
            if str(getattr(step, "goal_id", ""))
        }
        missing = expected - covered
        if missing:
            raise TaskProviderError(
                "PLAN_GOAL_COVERAGE_REQUIRED",
                "The execution plan does not cover every requested goal.",
            )

    @staticmethod
    def _require_new_request_incremental_plan(plan: Any) -> None:
        """Keep whole-plan execution out of the new-request control path."""

        if str(getattr(plan, "plan_source", "")) != INCREMENTAL_PLAN_SOURCE:
            raise TaskProviderError(
                "WHOLE_PLAN_NEW_REQUEST_DISABLED",
                "New Agent requests must advance one ready Goal at a time.",
            )

    @staticmethod
    def _broad_destructive_result(
        command: Command,
        *,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        normalized_scope = command.scope or "ALL_OWNED_POSTS"
        policy_decision = {
            "decision": "REJECT_UNBOUNDED_SCOPE",
            "original_scope": command.scope or command.entities.get("scope") or "",
            "normalized_scope": normalized_scope,
            "risk": command.risk or "BROAD_DESTRUCTIVE",
            "tool_invocation": False,
            "execution_side_effect": False,
        }
        logger.warning(
            "broad_destructive_scope_rejected run_id=%s trace_id=%s scope=%s normalized_scope=%s",
            run_id,
            trace_id,
            policy_decision["original_scope"],
            normalized_scope,
        )
        message = (
            "这是大范围不可逆操作。目前不能直接执行“删除全部文章”。"
            "请明确范围，例如指定文章、日期范围，或草稿/已发布范围。"
        )
        return RuntimeResult(
            success=False,
            status="WAITING_HUMAN",
            run_id=run_id,
            trace_id=trace_id,
            execution_path="agent_loop",
            error_code="UNBOUNDED_DESTRUCTIVE_SCOPE",
            error_message=message,
            content=message,
            partial_results={
                "policy_decision": policy_decision,
                "audit_event": {
                    "event": "BROAD_DESTRUCTIVE_SCOPE_REJECTED",
                    "semantic_operation": command.semantic_operation or "DELETE",
                    "scope": normalized_scope,
                    "tool_invocation": False,
                    "execution_side_effect": False,
                },
            },
        )

    @staticmethod
    def _clarification_result(
        command: Command,
        *,
        context: ContextSnapshot,
        run_id: str,
        trace_id: str,
        error_code: str,
    ) -> RuntimeResult:
        """Turn semantic ambiguity into an explicit human decision point."""

        reason = command.ambiguity or (
            "我还不能确定你想修改哪一项任务，请指定一下。"
            if error_code
            in {
                "MUTATION_TARGET_REQUIRED",
                "DELTA_TARGET_NOT_FOUND",
                "DELTA_INVALID_GROUNDING",
            }
            else (
                "Select one target before continuing."
                if error_code == "AMBIGUOUS_TARGET"
                else "Please clarify the requested outcome."
            )
        )
        return RuntimeResult(
            success=False,
            status="WAITING_HUMAN",
            run_id=run_id,
            trace_id=trace_id,
            execution_path="agent_loop",
            error_code=error_code,
            error_message=reason,
            content=reason,
            partial_results={
                "clarification": {
                    "reason": reason,
                    "command": command.model_dump(mode="json"),
                    "candidates": list(context.target_candidates),
                }
            },
        )

    async def _clarification_result_with_task(
        self,
        command: Command,
        *,
        context: ContextSnapshot,
        request_session: SessionContext | Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        error_code: str,
        persist_task: bool = True,
    ) -> RuntimeResult:
        """Keep an unresolved user task durable while it waits for input."""

        result = self._clarification_result(
            command,
            context=context,
            run_id=run_id,
            trace_id=trace_id,
            error_code=(
                "AMBIGUOUS_TARGET"
                if error_code == "TARGET_CLARIFICATION_REQUIRED"
                else error_code
            ),
        )
        if not persist_task:
            return result
        manager = self._task_manager
        if manager is None:
            return result
        goal = Goal(
            goal_id=f"clarification:{run_id}",
            description=command.requested_goal or command.raw_input,
            goal_type=command.type.value,
            required_capabilities=list(command.required_capabilities),
            target=(
                command.target.model_dump(mode="json")
                if command.target is not None
                else {}
            ),
        )
        tree = GoalTree(root=goal, source="COMMAND_CLARIFICATION", version=1)
        task = await manager.create_task(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            root_goal=goal,
            goal_tree=tree,
        )
        wait_for_human = getattr(manager, "wait_for_human", None)
        if callable(wait_for_human):
            task = await wait_for_human(
                task.task_id,
                reason=result.error_message,
                goal_id=goal.goal_id,
            )
        result.task_id = task.task_id
        partial_results = dict(result.partial_results or {})
        partial_results["task_ids"] = [task.task_id]
        result.partial_results = partial_results
        return result

    async def record_reasoning_result(
        self,
        *,
        goal_id: str,
        capability: str,
        result_type: str,
        payload: Mapping[str, Any],
        source_refs: Sequence[str],
        task_id: str,
        conversation_id: str,
        user_id: str = "",
        tenant_id: str = "",
    ) -> dict[str, Any]:
        """Persist one reasoning-backed Goal result as durable execution + artifact.

        No Worker/Queue hand-off — this is a pure reasoning step in the same
        AgentLoop.  The completed execution + artifact enter the durable fact
        sources so the next continuation observes the Goal as satisfied and
        downstream Goals consume the result through ``source_refs`` lineage.
        """
        execution_id = f"reasoning:{task_id}:{goal_id}:{capability}"
        artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, execution_id))
        summary = str(payload.get("summary") or payload.get("content") or "")
        key_points = payload.get("key_points") or payload.get("key_patterns") or []
        artifact_store = getattr(self._container, "artifact_store", None)
        if artifact_store is None:
            raise RuntimeError("REASONING_RESULT_COMMIT_FAILED: artifact store unavailable")
        missing_sources = []
        for source_ref in source_refs:
            known_artifact = artifact_store.get(str(source_ref))
            known_execution = (
                self._execution_repository.find_by_id(str(source_ref))
                if self._execution_repository is not None
                and callable(getattr(self._execution_repository, "find_by_id", None)
                ) else None
            )
            known_resource = await self._is_known_task_resource(
                str(source_ref),
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            if (
                known_artifact is None
                and known_execution is None
                and not known_resource
            ):
                missing_sources.append(str(source_ref))
        if missing_sources:
            raise RuntimeError(
                "REASONING_RESULT_COMMIT_FAILED: unknown source_refs "
                + ",".join(missing_sources)
            )
        capability_model = self._container.capability_registry.get(capability)
        artifact_type = str(
            getattr(capability_model, "output_artifact_type", "")
            or "ANALYSIS_REPORT"
        )
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            execution_id=execution_id,
            owner_task_id=task_id,
            owner_execution_id=execution_id,
            created_by_agent=capability,
            step_id=f"{goal_id}:reasoning",
            artifact_type=artifact_type,
            title=str(payload.get("title") or result_type or "分析结果")[:500],
            summary=summary[:2000],
            status="COMPLETED",
            metadata={
                "goal_id": goal_id,
                "capability": capability,
                "result_type": result_type,
                "artifact_type": artifact_type,
                "key_points": (
                    list(key_points)
                    if isinstance(key_points, list)
                    else [str(key_points)]
                ),
                "source_refs": [str(item) for item in source_refs],
                "payload": dict(payload),
            },
        )
        step = StepExecution(
            step_execution_id=str(uuid.uuid5(uuid.NAMESPACE_URL, execution_id + ":step")),
            execution_id=execution_id,
            step_id=f"{goal_id}:reasoning",
            goal_id=goal_id,
            capability=capability,
            tool_name="(reasoning)",
            status=StepStatus.COMPLETED,
            output_artifact=ArtifactHandle(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                summary=summary[:2000],
            ),
            completed_at=datetime.now(UTC).isoformat(),
        )
        execution = PlanExecution(
            execution_id=execution_id,
            plan_id=f"reasoning:{task_id}:{goal_id}:{capability}",
            task_id=task_id,
            status=ExecutionStatus.COMPLETED,
            steps=[step],
            current_step_index=1,
            has_side_effects=False,
            completed_at=datetime.now(UTC).isoformat(),
        )
        persisted = await self._persist_terminal_fact(
            artifact=artifact,
            execution=execution,
            goal_id=goal_id,
            capability=capability,
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            business_result={
                "result_type": result_type,
                "summary": summary[:500],
                "key_points": list(key_points) if isinstance(key_points, list) else [str(key_points)],
                "source_refs": [str(item) for item in source_refs],
            },
        )
        return persisted

    async def _is_known_task_resource(
        self,
        resource_id: str,
        *,
        task_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        """Return whether ``resource_id`` is a real business resource this Task
        already produced or read (draft / schedule / post).

        A reasoning result legitimately cites the concrete posts GET_POST_DETAIL
        read; those are resource ids, not artifact ids.  The lineage commit
        must accept them, otherwise PRODUCE_RESULT fails with
        REASONING_RESULT_COMMIT_FAILED (observed: unknown source_refs =
        post ids)."""
        provider = self._task_provider
        if provider is None or not task_id:
            return False
        try:
            task = await provider.get_task(
                TaskScope(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                ),
                task_id,
            )
        except Exception:
            return False
        if task is None:
            return False
        for ref in (getattr(task, "resource_index", ()) or ()):
            if str(getattr(ref, "resource_id", "") or "") == resource_id:
                return True
        return False

    async def record_tool_result(
        self,
        *,
        state: Any,
        action: AgentAction,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project an in-loop read into the shared durable fact sources."""

        task_id = str(getattr(getattr(state, "task", None), "task_id", "") or "")
        goal_id = str(getattr(getattr(state, "current_task", None), "goal_id", "") or "")
        capability = str(getattr(getattr(state, "current_task", None), "capability", "") or "")
        tool_name = str(result.get("tool_name") or action.tool_name or "")
        arguments = dict(result.get("tool_arguments") or action.tool_args or {})
        capability_model = self._container.capability_registry.get(capability)
        artifact_type = str(getattr(capability_model, "output_artifact_type", "") or "")
        if not task_id or not goal_id or not capability or not artifact_type:
            return {}
        data = result.get("data")
        if not isinstance(data, Mapping):
            data = {}
        items = data.get("items") or data.get("posts") or data.get("results") or []
        count = len(items) if isinstance(items, list) else int(data.get("total") or 0)
        identity = json.dumps(
            {"task_id": task_id, "goal_id": goal_id, "capability": capability, "tool": tool_name, "args": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        execution_id = f"read:{task_id}:{goal_id}:{digest}"
        artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, execution_id))
        refs = []
        for item in items[:32] if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            resource_id = item.get("post_id") or item.get("postId") or item.get("id")
            if resource_id:
                refs.append({"kind": "POST", "resource_id": str(resource_id)})
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            execution_id=execution_id,
            owner_task_id=task_id,
            owner_execution_id=execution_id,
            created_by_agent=capability,
            step_id=f"{goal_id}:read:{digest}",
            artifact_type=artifact_type,
            title=str(data.get("title") or tool_name or capability)[:500],
            summary=str(data.get("summary") or f"{count} search results")[:2000],
            status="SUCCESS",
            metadata={
                "goal_id": goal_id,
                "capability": capability,
                "tool_name": tool_name,
                "arguments": arguments,
                "result": dict(data),
                "resource_refs": refs,
            },
        )
        step = StepExecution(
            step_execution_id=str(uuid.uuid5(uuid.NAMESPACE_URL, execution_id + ":step")),
            execution_id=execution_id,
            step_id=artifact.step_id,
            goal_id=goal_id,
            capability=capability,
            tool_name=tool_name,
            arguments=arguments,
            status=StepStatus.COMPLETED,
            output_artifact=ArtifactHandle(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                summary=artifact.summary,
                resource_refs=refs,
            ),
            completed_at=datetime.now(UTC).isoformat(),
        )
        execution = PlanExecution(
            execution_id=execution_id,
            plan_id=f"read:{task_id}:{goal_id}:{capability}:{digest}",
            task_id=task_id,
            status=ExecutionStatus.COMPLETED,
            steps=[step],
            current_step_index=1,
            has_side_effects=False,
            completed_at=datetime.now(UTC).isoformat(),
        )
        persisted = await self._persist_terminal_fact(
            artifact=artifact,
            execution=execution,
            goal_id=goal_id,
            capability=capability,
            task_id=task_id,
            conversation_id=str(state.conversation_context.get("conversation_id") or ""),
            user_id=str(state.conversation_context.get("user_id") or ""),
            tenant_id=str(state.conversation_context.get("tenant_id") or ""),
            business_result={"count": count, "resource_refs": refs},
        )
        execution_states = list(state.context_snapshot.get("execution_states") or [])
        execution_states.append(_compact_loop_execution_state(execution))
        # Bound the snapshot: a full PlanExecution dump embeds step checkpoint
        # data (completed tool results, read bodies) and a long pipeline grew
        # the LLM context past the provider limit (observed: 1.1M tokens
        # requested).  Keep only the most recent entries, compacted.
        state.context_snapshot["execution_states"] = execution_states[-20:]
        artifacts = list(state.context_snapshot.get("artifacts") or [])
        artifacts.append(artifact.model_dump(mode="json"))
        state.context_snapshot["artifacts"] = artifacts
        return persisted

    async def _persist_terminal_fact(
        self,
        *,
        artifact: Artifact,
        execution: PlanExecution,
        goal_id: str,
        capability: str,
        task_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        business_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        artifact_store = getattr(self._container, "artifact_store", None)
        repository = self._execution_repository
        if artifact_store is None or repository is None:
            raise RuntimeError("RUNTIME_FACT_COMMIT_FAILED: durable store unavailable")
        artifact_store.create(artifact)
        repository.save(execution)
        if self._observation_store is not None:
            self._observation_store.save(ActionObservation(
                execution_id=execution.execution_id,
                task_id=task_id,
                conversation_id=conversation_id,
                goal_id=goal_id,
                capability=capability,
                status="COMPLETED",
                artifact_refs=[artifact.artifact_id],
                resource_refs=list(artifact.metadata.get("resource_refs") or []),
                business_result=dict(business_result),
                state="DONE",
            ))
        projector = getattr(self._task_provider, "persist_completion_projection", None)
        if callable(projector) and task_id and user_id and tenant_id and conversation_id:
            await projector(
                TaskScope(user_id=user_id, tenant_id=tenant_id, conversation_id=conversation_id),
                task_id=task_id,
                execution_id=execution.execution_id,
                status="COMPLETED",
                goal_id=goal_id,
                objective_id=str(getattr(execution, "objective_id", "") or "") or None,
                artifacts=[{
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "type": artifact.artifact_type,
                    "step_id": artifact.step_id,
                    "summary": artifact.summary,
                    "resource_id": artifact.resource_id,
                    "resource_type": artifact.resource_type,
                    "capability": capability,
                }],
            )
        return {
            "execution_id": execution.execution_id,
            "artifact_id": artifact.artifact_id,
            "task_id": task_id,
            "artifact_type": artifact.artifact_type,
        }

    @staticmethod
    def _agent_loop_result(
        result: AgentRunResult,
        *,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        nested = next(
            (
                item
                for item in reversed(
                    [*result.execution_results, *result.tool_results]
                )
                if item.get("execution_id") or item.get("task_id")
            ),
            {},
        )
        state = result.state
        task_id = str(
            nested.get("task_id")
            or getattr(getattr(state, "task", None), "task_id", "")
        )
        execution_id = nested.get("execution_id")
        content = result.content or result.question
        partial_results: dict[str, Any] = {
            "agent_loop": True,
            "iterations": result.iterations,
            "actions": result.actions,
            "observations": result.observations,
            "tool_results": result.tool_results,
            "execution_results": result.execution_results,
        }
        reasoning_failure = getattr(state, "reasoning_failure", None)
        if reasoning_failure:
            partial_results["reasoning_failure"] = dict(reasoning_failure)
        if result.root_error_code:
            partial_results["root_failure"] = {
                "error_code": result.root_error_code,
                "error_message": result.root_error_message,
                "goal_id": result.root_error_goal_id,
                "iteration": result.root_error_iteration,
            }
        timings = dict(getattr(state, "timings", None) or {})
        if timings:
            partial_results["timings"] = timings
        first_capability = _first_capability(state)
        if first_capability:
            partial_results["first_capability"] = first_capability
        return RuntimeResult(
            success=result.success,
            status=result.status.value,
            run_id=run_id,
            trace_id=trace_id,
            task_id=task_id,
            execution_id=str(execution_id) if execution_id else None,
            content=content,
            summary=result.content,
            started_execution=bool(execution_id),
            execution_path="agent_loop",
            error_code=result.error_code,
            error_message=result.error_message,
            partial_results=partial_results,
        )

    @staticmethod
    def _mapping_result(value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        return {"ok": bool(value), "value": value}

    @staticmethod
    def _failure_result(
        exc: Exception,
        *,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=run_id,
            trace_id=trace_id,
            execution_path="agent_loop",
            error_code=str(getattr(exc, "code", "RUNTIME_ADAPTER_FAILED")),
            error_message=str(exc),
        )

    @staticmethod
    def _coerce_session(
        session: SessionContext | Any | None,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        timezone: str,
    ) -> SessionContext | Any:
        if isinstance(session, SessionContext):
            return session
        if isinstance(session, Mapping):
            payload = dict(session)
            payload.setdefault("conversation_id", conversation_id)
            payload.setdefault("user_id", user_id)
            payload.setdefault("tenant_id", tenant_id)
            payload.setdefault("timezone", timezone)
            return SessionContext.model_validate(payload)
        if session is not None:
            return session
        return SessionContext(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone,
        )

    @staticmethod
    def _validate_session_scope(
        session: Any,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> None:
        for field, expected in (
            ("conversation_id", conversation_id),
            ("user_id", user_id),
            ("tenant_id", tenant_id),
        ):
            actual = getattr(session, field, expected)
            if actual != expected:
                raise TaskProviderError(
                    "SESSION_SCOPE_MISMATCH",
                    f"Session {field} does not match the authenticated request.",
                )


def _message_fingerprint(message: str) -> str:
    """Stable short fingerprint of a user message for duplicate suppression."""
    return hashlib.sha256(str(message or "").strip().encode("utf-8")).hexdigest()[:24]


def _first_capability(state: Any) -> str:
    """Resolve the first semantic capability the AgentLoop decided to execute.

    Used to project a meaningful activity on the 202 response: the frontend
    can immediately show "正在生成内容…" instead of "正在理解你的请求…" while
    the durable Execution is still queued.
    """

    goal_tree = getattr(state, "goal_tree", None)
    if goal_tree is None:
        return ""
    completed = {
        str(value)
        for value in (getattr(state, "completed_task_ids", ()) or ())
        if str(value)
    }
    for node in goal_tree.task_nodes:
        if str(getattr(node, "task_id", "")) not in completed:
            return str(getattr(node, "capability", "") or "")
    for goal in goal_tree.executable_goals():
        capabilities = [
            str(value)
            for value in (getattr(goal, "required_capabilities", ()) or ())
            if str(value)
        ]
        if capabilities:
            return capabilities[0]
    return ""


def _merge_parallel_runtime_results(
    results: Sequence[RuntimeResult],
    *,
    run_id: str,
    trace_id: str,
) -> RuntimeResult:
    """Combine child envelopes without hiding their ownership or timing."""

    execution_ids = [
        str(item.execution_id)
        for item in results
        if item.execution_id
    ]
    task_ids = [str(item.task_id) for item in results if item.task_id]
    statuses = {str(item.status).upper() for item in results}
    if "WAITING_HUMAN" in statuses or "WAITING_APPROVAL" in statuses:
        status = "WAITING_HUMAN"
    elif "FAILED" in statuses and all(
        value in {"COMPLETED", "FAILED", "CANCELLED"} for value in statuses
    ):
        status = "FAILED"
    elif statuses and statuses <= {"COMPLETED"}:
        status = "COMPLETED"
    else:
        # QUEUED/SUBMITTED/RUNNING means the logical turn accepted work; the
        # durable Execution/Observation path owns its eventual completion.
        status = "RUNNING"
    partial_results = {
        "parallel": True,
        "execution_ids": execution_ids,
        "task_ids": task_ids,
        "parallel_results": [
            {
                "run_id": item.run_id,
                "task_id": item.task_id,
                "execution_id": item.execution_id,
                "status": item.status,
                "success": item.success,
                "content": item.content,
                "partial_results": item.partial_results or {},
            }
            for item in results
        ],
        "nodes": [
            node
            for item in results
            for node in ((item.partial_results or {}).get("nodes") or [])
        ],
    }
    artifacts = [artifact for item in results for artifact in item.artifacts]
    steps = [step for item in results for step in item.steps]
    failures = [item for item in results if not item.success and item.error_message]
    summaries = [item.summary or item.content for item in results if item.summary or item.content]
    return RuntimeResult(
        success=status == "COMPLETED",
        status=status,
        run_id=run_id,
        task_id=task_ids[0] if task_ids else "",
        execution_id=execution_ids[0] if execution_ids else None,
        content=(
            "；".join(summaries[:3])
            or f"已同时开始处理 {len(results)} 项事情。"
        ),
        summary=(
            "；".join(summaries[:3])
            or f"已同时开始处理 {len(results)} 项事情。"
        ),
        started_execution=bool(execution_ids),
        execution_path="agent_loop",
        error_code=failures[0].error_code if failures else "",
        error_message=failures[0].error_message if failures else "",
        artifacts=artifacts,
        steps=steps,
        trace_id=trace_id,
        partial_results=partial_results,
    )


def _start_timings() -> dict[str, str]:
    """First-meaningful-feedback timing markers for one user turn."""

    return {"message_received_at": _now_timing()}


def _first_reasoning_step(plan: Any, registry: Any) -> str | None:
    """Return the first reasoning-backed capability in a plan, else None.

    Reasoning-backed capabilities (``is_llm_step``, e.g. ANALYZE_CONTENT_PATTERNS)
    must be produced inside AgentLoop via PRODUCE_RESULT; submitting them to the
    durable queue is an execution-semantics violation the Worker rejects.
    """
    if plan is None:
        return None
    get_cap = getattr(registry, "get", None)
    for step in (getattr(plan, "steps", ()) or ()):
        capability = str(getattr(step, "capability", "") or "")
        if not capability:
            continue
        if callable(get_cap):
            cap = get_cap(capability)
            if cap is not None and getattr(cap, "is_llm_step", False):
                return capability
        else:
            from greenbook_agent_core.capability.registry import CapabilityRegistry

            cap = CapabilityRegistry().get(capability)
            if cap is not None and getattr(cap, "is_llm_step", False):
                return capability
    return None


def _now_timing() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _attach_timings(result: Any, timings: dict[str, str]) -> None:
    """Attach turn timing markers to the result without changing its shape."""

    if result is None:
        return
    partial = dict(getattr(result, "partial_results", None) or {})
    partial["timings"] = dict(timings)
    result.partial_results = partial


def _incremental_plan(state: Any, plan: Any) -> Any:
    """Reduce a compiled plan to ONE durable semantic action.
    Structured mode selection (no text routing): every executable Goal that
    still has unsatisfied business state enters incremental execution. The
    plan is reduced to the deterministic current unsatisfied Goal's next
    not-yet-completed step, so each durable Execution carries exactly one
    semantic action and AgentLoop resumes with the real ActionObservation
    before deciding again.

    The plan_id is deterministic (task, goal, action, owned resource, run_at)
    so a replayed continuation submits the same action identity and the
    submission guard deduplicates it (§28/§29). A fully satisfied tree is
    untouched — AgentLoop FINISHes instead.
    """

    goal_tree = getattr(state, "goal_tree", None)
    if goal_tree is None or plan is None:
        return plan
    executable_goals = goal_tree.executable_goals()
    if not executable_goals:
        return plan
    facts_by_goal = _facts_from_execution_states(
        _execution_states_from_state(
            state,
            task_id=str(getattr(plan, "task_id", "") or ""),
        )
    )
    current_goal_id = select_unsatisfied_goal_id(goal_tree, facts_by_goal)
    if not current_goal_id:
        return plan
    steps = [
        step
        for step in (getattr(plan, "steps", ()) or ())
        if str(getattr(step, "goal_id", "")) == current_goal_id
    ]
    if not steps:
        return plan
    # AgentLoop's resume state owns "what was already done"; skip completed
    # steps so a resumed Goal advances to its next action instead of
    # re-running the completed first capability.
    completed = {
        str(value)
        for value in (
            getattr(state, "completed_task_ids", ())
            or getattr(getattr(state, "resume_context", None), "completed_step_ids", ())
            or ()
        )
        if str(value)
    }
    facts = facts_by_goal.get(current_goal_id, {})
    single = next(
        (
            step
            for step in steps
            if str(getattr(step, "step_id", "")) not in completed
            and str(getattr(step, "capability", "")).upper()
            not in {
                str(value).upper()                for value in facts.get("completed_capabilities", ())
            }
        ),
        None,
    )
    if single is None:
        single = steps[0]
    if hasattr(single, "model_copy"):
        single = single.model_copy(deep=True)
    if hasattr(single, "depends_on"):
        single.depends_on = []
    # Incremental executions carry exactly one step, so the Worker's
    # same-Execution upstream-artifact walk cannot see the DRAFT produced by
    # the dependency Goal.  Propagate the durable DRAFT identity from the
    # dependency's terminal observation into schedule steps — otherwise the
    # schedule tool receives an empty draft_id and fails VALIDATION
    # (design goal 0813 — the chain must complete without re-asking).
    capability = str(getattr(single, "capability", "")).upper()
    if capability in {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}:
        constraints = dict(getattr(single, "constraints", {}) or {})
        if not constraints.get("draft_id"):
            draft_id = str(facts.get("draft_id") or "") or _dependency_fact_value(
                goal_tree,
                current_goal_id,
                facts_by_goal,
                "draft_id",
            )
            if draft_id:
                constraints["draft_id"] = draft_id
                single.constraints = constraints
    elif capability == "GET_POST_DETAIL":
        # Same incremental-execution gap as draft_id: a concrete-read step is
        # its own Execution, so the Worker cannot resolve post_id from an
        # upstream SEARCH in the same Execution.  Inject a real post_id from
        # the dependency SEARCH Goal's durable POST references, skipping posts
        # this task already read — never inventing an identifier.
        constraints = dict(getattr(single, "constraints", {}) or {})
        if not constraints.get("post_id"):
            post_id = str(facts.get("post_id") or "") or _dependency_post_id(
                goal_tree,
                current_goal_id,
                facts_by_goal,
                _already_read_post_ids(facts_by_goal),
            )
            if post_id:
                constraints["post_id"] = post_id
                single.constraints = constraints
    run_at = str(
        (getattr(single, "constraints", {}) or {}).get("run_at")
        or facts.get("run_at")
        or ""
    )
    resource_id = str(
        (getattr(single, "constraints", {}) or {}).get("draft_id")
        or facts.get("draft_id")
        or ""
    )
    task_id = str(getattr(plan, "task_id", ""))
    plan_id = (
        f"inc:{task_id}:{current_goal_id}:{str(getattr(single, 'capability', ''))}"
        f":{resource_id}:{run_at}"
    )
    return TaskPlan(
        task_id=task_id,
        steps=[single],
        plan_source=INCREMENTAL_PLAN_SOURCE,
        plan_version=int(getattr(plan, "plan_version", 1) or 1),
        plan_id=plan_id,
    )


def _dependency_fact_value(
    goal_tree: Any,
    goal_id: str,
    facts_by_goal: Mapping[str, Mapping[str, Any]],
    key: str,
) -> str:
    """Walk the Goal dependencies (transitive) for the first durable fact value.

    Incremental executions submit one Goal at a time; a downstream Goal (e.g.
    SCHEDULE_PUBLISH) must still see the business resource produced by its
    dependency (e.g. the DRAFT from GENERATE_CONTENT) even though that
    artifact lives in another Execution.  facts are aggregated per Goal from
    terminal observations, so this is lineage propagation, not text routing.
    """

    seen: set[str] = set()
    queue = [str(goal_id)]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        value = str((facts_by_goal.get(current) or {}).get(key) or "")
        if value:
            return value
        if goal_tree is None:
            continue
        for goal in goal_tree.all_goals():
            if str(getattr(goal, "goal_id", "")) == current:
                queue.extend(
                    str(dependency)
                    for dependency in (getattr(goal, "dependencies", ()) or ())
                )
                break
    return ""


def _already_read_post_ids(facts_by_goal: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """Posts already consumed by completed GET_POST_DETAIL goals."""

    return {
        str((facts_by_goal.get(goal_id) or {}).get("post_id") or "")
        for goal_id in facts_by_goal
        if (facts_by_goal.get(goal_id) or {}).get("post_id")
    }


def _dependency_post_id(
    goal_tree: Any,
    goal_id: str,
    facts_by_goal: Mapping[str, Mapping[str, Any]],
    already_read: set[str],
) -> str:
    """Resolve the next unread real post_id from a dependency SEARCH Goal.

    The dependency Goal's terminal observation carries the POST resource
    references the search returned (``post_ids``).  This walks the Goal
    dependencies transitively and returns the first identifier the task has
    not already read, so repeated incremental continuations consume distinct
    posts instead of re-reading the same one.
    """

    seen: set[str] = set()
    queue = [str(goal_id)]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for post_id in ((facts_by_goal.get(current) or {}).get("post_ids") or []):
            if post_id and str(post_id) not in already_read:
                return str(post_id)
        if goal_tree is None:
            continue
        for goal in goal_tree.all_goals():
            if str(getattr(goal, "goal_id", "")) == current:
                queue.extend(
                    str(dependency)
                    for dependency in (getattr(goal, "dependencies", ()) or ())
                )
                break
    return ""


def _inject_reasoning_context(state: Any, plan: Any) -> Any:
    """Inject reasoning-backed dependency results into the current step.

    A downstream Goal (for example GENERATE_CONTENT) consumes the reasoning
    summary its dependency produced.  The summary is read from every durable
    fact source the continuation already holds — the current AgentLoop's own
    reasoning results, the reasoning execution evidence (plan_id encodes
    ``reasoning:{task}:{goal}:{capability}``), and the persisted
    ANALYSIS_REPORT artifact — so it survives the separate continuations of a
    real multi-step pipeline.  This is lineage propagation, never a fixed
    SEARCH->SUMMARY->GENERATE workflow.
    """

    if plan is None or not getattr(plan, "steps", None):
        return plan
    step = plan.steps[0]
    goal_id = str(getattr(step, "goal_id", "") or "")
    goal_tree = getattr(state, "goal_tree", None)
    dependency_ids: list[str] = []
    if goal_tree is not None:
        for goal in goal_tree.all_goals():
            if str(getattr(goal, "goal_id", "")) == goal_id:
                dependency_ids = list(getattr(goal, "dependencies", ()) or ())
                break
    reasoning_by_goal: dict[str, str] = {}
    # 1) Reasoning results produced in the current AgentLoop run.
    for result in (getattr(state, "execution_results", ()) or ()):
        if not isinstance(result, Mapping):
            continue
        if result.get("status") != "COMPLETED" or not result.get("reasoning_result"):
            continue
        result_goal = str(result.get("goal_id") or "")
        summary = (result.get("reasoning_result") or {}).get("summary") or ""
        if result_goal and summary:
            reasoning_by_goal.setdefault(result_goal, str(summary))
    # 2) Durable reasoning execution evidence carried by the continuation
    #    context (plan_id encodes the goal; the completed step carries the
    #    output artifact summary).
    snapshot = getattr(state, "context_snapshot", None) or {}
    if isinstance(snapshot, Mapping):
        for item in (snapshot.get("execution_states") or []):
            if not isinstance(item, Mapping):
                continue
            plan_id = str(item.get("plan_id") or "")
            if not plan_id.startswith("reasoning:"):
                continue
            parts = plan_id.split(":")
            if len(parts) < 4:
                continue
            reasoning_goal = parts[-2]
            for step_item in (item.get("steps") or []):
                output = step_item.get("output_artifact") if isinstance(step_item, Mapping) else None
                summary = (
                    str(output.get("summary") or "")
                    if isinstance(output, Mapping)
                    else ""
                )
                if reasoning_goal and summary:
                    reasoning_by_goal.setdefault(reasoning_goal, summary)
        # 3) Persisted ANALYSIS_REPORT artifacts (goal attribution from step_id,
        #    for example "g3:reasoning" -> g3).
        for artifact in (snapshot.get("artifacts") or []):
            if not isinstance(artifact, Mapping):
                continue
            if str(artifact.get("artifact_type") or "").upper() != "ANALYSIS_REPORT":
                continue
            summary = str(artifact.get("summary") or "")
            step_id = str(artifact.get("step_id") or "")
            goal = (
                str(artifact.get("goal_id") or "")
                or (step_id.split(":", 1)[0] if ":" in step_id else "")
            )
            if goal and summary:
                reasoning_by_goal.setdefault(goal, summary)
    summaries = [
        reasoning_by_goal[dependency_id]
        for dependency_id in dependency_ids
        if reasoning_by_goal.get(dependency_id)
    ]
    if not summaries:
        return plan
    constraints = dict(getattr(step, "constraints", {}) or {})
    summary_text = "\n".join(f"- {summary}" for summary in summaries)
    if "summary" not in constraints:
        constraints["summary"] = summary_text
    instruction = str(constraints.get("instruction") or "").strip()
    topic = _goal_topic(state, dependency_ids)
    topic_note = f"主题：{topic}。" if topic else ""
    if instruction:
        constraints["instruction"] = (
            f"{instruction}\n\n{topic_note}\n已获取的相关内容观点摘要（供写作参考）：\n{summary_text}"
        )
    else:
        constraints["instruction"] = (
            f"{topic_note}\n根据以下观点摘要撰写一篇相关文章：\n{summary_text}"
        )
    step.constraints = constraints
    return plan


def _goal_topic(state: Any, dependency_ids: Sequence[str]) -> str:
    """Resolve the user's topic from the Goal tree / Command for downstream use."""

    command = getattr(state, "command", None)
    if command is not None:
        for source in (
            getattr(command, "entities", None),
            getattr(command, "parameters", None),
        ):
            if isinstance(source, Mapping):
                topic = source.get("topic") or source.get("search_keyword") or source.get("keyword")
                if topic not in (None, ""):
                    return str(topic)
    goal_tree = getattr(state, "goal_tree", None)
    if goal_tree is not None:
        for goal in goal_tree.all_goals():
            if str(getattr(goal, "goal_id", "")) not in dependency_ids:
                continue
            target = getattr(goal, "target", {}) or {}
            if isinstance(target, Mapping):
                topic = target.get("keyword") or target.get("topic")
                if topic not in (None, ""):
                    return str(topic)
    return ""


def _execution_states_from_state(
    state: Any,
    *,
    task_id: str = "",
) -> list[dict[str, Any]]:
    snapshot = getattr(state, "context_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return []
    values = [
        item
        for item in (snapshot.get("execution_states", []) or [])
        if isinstance(item, Mapping)
    ]
    owner_task_id = task_id or str(
        getattr(getattr(state, "current_task", None), "task_id", "") or ""
    )
    if not owner_task_id:
        return [dict(item) for item in values]
    # Execution facts are owned by a durable Task. Missing ownership is not
    # safe evidence for a write in this Task, so it is excluded rather than
    # falling back to a sibling's draft.
    return [
        dict(item)
        for item in values
        if str(item.get("task_id") or "") == owner_task_id
    ]


def _facts_from_execution_states(
    execution_states: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate per-Goal durable business facts from terminal executions.

    Reads both flattened entries (goal_id + capability at the top level, as
    emitted by observation evidence) and the embedded steps of projected
    executions, so a durable reasoning execution (goal_id/capability on its
    completed step) contributes its completed capability like any other.
    """

    facts: dict[str, dict[str, Any]] = {}
    for item in execution_states:
        goal_id = str(item.get("goal_id") or "")
        entry = facts.setdefault(
            goal_id,
            {
                "draft_id": "",
                "schedule_id": "",
                "post_id": "",
                "post_ids": [],
                "run_at": "",
                "status": "",
                "completed_capabilities": [],
                "artifact_types": [],
            },
        )
        for key in ("draft_id", "schedule_id", "post_id", "run_at"):
            value = item.get(key)
            if value not in (None, ""):
                entry[str(key)] = str(value)
        for post_id in (item.get("post_ids") or []):
            if post_id and str(post_id) not in entry["post_ids"]:
                entry["post_ids"].append(str(post_id))
        status = str(item.get("status") or "")
        if status:
            entry["status"] = status
        capability = str(item.get("capability") or "")
        if status.upper() == "COMPLETED" and capability:
            completed_capabilities = entry.setdefault("completed_capabilities", [])
            if capability not in completed_capabilities:
                completed_capabilities.append(capability)
        artifact_type = str(
            item.get("artifact_type")
            or item.get("output_artifact_type")
            or _artifact_type_for_capability(capability)
            or ""
        )
        if artifact_type:
            types = entry.setdefault("artifact_types", [])
            if artifact_type not in types:
                types.append(artifact_type)
        for step in (item.get("steps") or []):
            if not isinstance(step, Mapping):
                continue
            step_goal = str(step.get("goal_id") or "")
            step_capability = str(step.get("capability") or "")
            step_status = str(step.get("status") or "").upper()
            if not step_goal or not step_capability:
                continue
            step_entry = facts.setdefault(
                step_goal,
                {
                    "draft_id": "",
                    "schedule_id": "",
                    "post_id": "",
                    "post_ids": [],
                    "run_at": "",
                    "status": "",
                    "completed_capabilities": [],
                    "artifact_types": [],
                },
            )
            if step_status == "COMPLETED":
                completed = step_entry.setdefault("completed_capabilities", [])
                if step_capability not in completed:
                    completed.append(step_capability)
                output = step.get("output_artifact") or {}
                output_type = str(
                    output.get("artifact_type")
                    if isinstance(output, Mapping)
                    else ""
                )
                if not output_type:
                    output_type = _artifact_type_for_capability(step_capability)
                if output_type:
                    types = step_entry.setdefault("artifact_types", [])
                    if output_type not in types:
                        types.append(output_type)
                # ContextBuilder projects terminal Execution steps rather
                # than the Task's resource index. Preserve the business
                # resource identity from that durable output so publication
                # Goals remain satisfied after a restart/refresh.
                if isinstance(output, Mapping):
                    resource_id = str(output.get("resource_id") or "")
                    normalized_type = output_type.upper()
                    if resource_id and normalized_type in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
                        step_entry["draft_id"] = resource_id
                    elif resource_id and normalized_type in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
                        step_entry["schedule_id"] = resource_id
                    elif resource_id and normalized_type in {"POST", "PUBLISHED_POST"}:
                        step_entry["post_id"] = resource_id
                    # SEARCH_RESULT outputs carry the referenced posts in
                    # resource_refs (a list), not a single resource_id.  Without
                    # this extraction the follow-up GET_POST_DETAIL step cannot
                    # resolve a post_id and the loop degrades into repeated
                    # searches (observed live: first task looped 20+ iterations
                    # on community.search_public_posts for GET_POST_DETAIL).
                    for ref in (output.get("resource_refs") or []):
                        if not isinstance(ref, Mapping):
                            continue
                        ref_kind = str(
                            ref.get("kind")
                            or ref.get("resource_type")
                            or ref.get("resource_kind")
                            or ""
                        ).upper()
                        ref_id = str(ref.get("resource_id") or "")
                        if not ref_id:
                            continue
                        if ref_kind in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
                            step_entry["draft_id"] = ref_id
                        elif ref_kind in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
                            step_entry["schedule_id"] = ref_id
                        elif ref_kind == "POST" and ref_id not in step_entry["post_ids"]:
                            step_entry["post_ids"].append(ref_id)
        # A reasoning-backed result is persisted as a durable execution with a
        # deterministic plan_id ``reasoning:{task}:{goal}:{capability}``.  The
        # execution_step table does not persist goal_id, so derive the goal
        # attribution from the plan id to keep the Goal satisfied across
        # continuations/restarts.
        plan_id = str(item.get("plan_id") or "")
        if plan_id.startswith("reasoning:"):
            parts = plan_id.split(":")
            if len(parts) >= 4:
                reasoning_goal = parts[-2]
                reasoning_capability = parts[-1]
                if reasoning_goal and reasoning_capability:
                    reasoning_entry = facts.setdefault(
                        reasoning_goal,
                        {
                            "draft_id": "",
                            "schedule_id": "",
                            "post_id": "",
                            "post_ids": [],
                            "run_at": "",
                            "status": "",
                            "completed_capabilities": [],
                            "artifact_types": [],
                        },
                    )
                    reasoning_entry["status"] = "COMPLETED"
                    completed = reasoning_entry.setdefault("completed_capabilities", [])
                    if reasoning_capability not in completed:
                        completed.append(reasoning_capability)
                    types = reasoning_entry.setdefault("artifact_types", [])
                    if "ANALYSIS_REPORT" not in types:
                        types.append("ANALYSIS_REPORT")
    return facts


def _find_incremental_submission(repository: Any, plan: Any) -> dict[str, Any] | None:
    """Deduplicate a replayed continuation's durable action submission.

    Returns an already-submitted/completed execution envelope for a
    deterministic plan_id, or None when a fresh submission is allowed
    (failed/cancelled actions may retry).
    """

    plan_id = str(getattr(plan, "plan_id", "") or "")
    if repository is None or not plan_id:
        return None
    list_all = getattr(repository, "list_all", None)
    if not callable(list_all):
        return None
    for execution in list_all():
        if str(getattr(execution, "plan_id", "")) != plan_id:
            continue
        status = str(
            getattr(getattr(execution, "status", ""), "value", getattr(execution, "status", ""))
        ).upper()
        if status == "COMPLETED":
            # ``list_all`` may return lightweight execution summaries. Load
            # the canonical completed execution snapshot before projecting
            # resources from its output artifact/checkpoint facts.
            full_execution = execution
            for loader_name in ("find_by_id", "get_by_id", "get"):
                loader = getattr(repository, loader_name, None)
                if not callable(loader):
                    continue
                try:
                    candidate = loader(str(execution.execution_id))
                    if candidate is not None:
                        full_execution = candidate
                        break
                except Exception:  # projection must remain best-effort
                    continue
            resource_refs: list[dict[str, Any]] = []
            draft_id = ""
            schedule_id = ""
            for step in getattr(full_execution, "steps", ()) or ():
                artifact = getattr(step, "output_artifact", None)
                if artifact is not None:
                    kind = str(
                        getattr(artifact, "resource_type", None)
                        or getattr(artifact, "artifact_type", None)
                        or ""
                    ).upper()
                    rid = str(getattr(artifact, "resource_id", "") or "")
                    if rid:
                        resource_refs.append({"kind": kind, "resource_id": rid})
                        if kind == "DRAFT" and not draft_id:
                            draft_id = rid
                        if kind == "SCHEDULE" and not schedule_id:
                            schedule_id = rid
                checkpoint = dict(getattr(step, "checkpoint_data", {}) or {})
                completed = checkpoint.get("completed_tool_result") or {}
                data = completed.get("data") if isinstance(completed, Mapping) else None
                if isinstance(data, Mapping):
                    draft_id = draft_id or str(data.get("draft_id") or "")
                    schedule_id = schedule_id or str(data.get("schedule_id") or "")
                raw_refs = []
                if isinstance(completed, Mapping):
                    raw_refs.extend(completed.get("resource_refs") or [])
                if isinstance(data, Mapping):
                    raw_refs.extend(data.get("resource_refs") or [])
                for ref in raw_refs:
                    if isinstance(ref, Mapping) and ref.get("resource_id"):
                        kind = str(ref.get("kind") or ref.get("resource_type") or "").upper()
                        rid = str(ref["resource_id"])
                        resource_refs.append({"kind": kind, "resource_id": rid})
                        if kind == "DRAFT" and not draft_id:
                            draft_id = rid
                        if kind == "SCHEDULE" and not schedule_id:
                            schedule_id = rid
            return {
                "execution_id": str(execution.execution_id),
                "status": "COMPLETED",
                "ok": True,
                "success": True,
                "queued": False,
                "deduplicated": True,
                "draft_id": draft_id or None,
                "schedule_id": schedule_id or None,
                "resource_refs": resource_refs,
                "message": "Action already completed; deduplicated.",
            }
        if status in {"QUEUED", "RUNNING", "SUBMITTED", "PENDING", "READY", ""}:
            return {
                "execution_id": str(execution.execution_id),
                "status": "QUEUED",
                "ok": True,
                "queued": True,
                "deduplicated": True,
                "message": "Action already submitted; deduplicated.",
            }
        # FAILED / CANCELLED: allow a fresh retry submission.
        return None
    return None


def _command_from_tree(goal_tree: GoalTree) -> Command:
    """Build a minimal Command that preserves Goal semantics for a resume.

    This is not intent re-interpretation: the GoalTree is already the
    structured result of Command understanding, and AgentLoop consumes the
    Command only as an interface envelope during continuation.
    """

    root = goal_tree.root_goal
    description = str(root.description or root.goal_type or "Goal execution")
    query_like = str(root.goal_type).strip().upper() in {"QUERY", "RESEARCH", "ANALYZE"}
    return Command(
        type=CommandType.QUERY if query_like else CommandType.CREATE,
        goal=description,
        objective=description,
        required_capabilities=list(root.required_capabilities),
        raw_input=description,
    )


_CREATE_TASK_STRUCTURAL_FIELDS = frozenset({
    "description",
    "goal",
    "goal_category",
    "required_capabilities",
    "constraints",
    "temporal_constraint",
    "publication_intent",
    "semantic_action",
    "semantic_operation",
    "target",
    "resource_target",
})
_CREATE_TASK_TEMPORAL_ALIASES = frozenset({
    "run_at",
    "publish_at",
    "scheduled_at",
    "publish_time",
    "schedule_time",
    "time",
    "datetime",
})
_CREATE_TASK_PUBLICATION_ALIASES = frozenset({
    "publication_intent",
    "publication_mode",
    "publish_mode",
    "publish_intent",
})


def _has_owned_value(value: Any) -> bool:
    """Return whether a per-Task fact is meaningful, preserving ``False``."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, Sequence)):
        return bool(value)
    return True


def _create_task_goal_fields(desired: Mapping[str, Any]) -> dict[str, Any]:
    """Project only one CREATE_TASK delta's facts into its root Goal.

    TaskDelta is the isolation boundary for one independent user outcome.  Do
    not make this helper fall back to request-wide ``Command`` values: a
    missing time or topic must yield a clarification, never borrow a sibling's
    value.  The projection also keeps temporal/publication/action fields typed
    on ``Goal`` instead of leaving conflicting copies in ``constraints``.
    """

    owned_constraints: list[dict[str, Any]] = []
    temporal: dict[str, Any] = {}
    publication_intent = str(desired.get("publication_intent") or "").strip()
    semantic_operation = str(
        desired.get("semantic_action")
        or desired.get("semantic_operation")
        or ""
    ).strip()

    raw_temporal = desired.get("temporal_constraint")
    if isinstance(raw_temporal, Mapping):
        temporal.update({
            str(key): value
            for key, value in raw_temporal.items()
            if _has_owned_value(value)
        })
    elif _has_owned_value(raw_temporal):
        temporal["run_at"] = raw_temporal

    raw_constraints = desired.get("constraints")
    raw_items: list[Mapping[str, Any]] = []
    if isinstance(raw_constraints, Mapping):
        raw_items.append(raw_constraints)
    elif isinstance(raw_constraints, Sequence) and not isinstance(
        raw_constraints, (str, bytes, bytearray)
    ):
        raw_items.extend(
            item for item in raw_constraints if isinstance(item, Mapping)
        )

    # Preserve arbitrary task-specific instruction/style/query facts, but
    # normalise known typed fields out of constraints.  ``type``/``value`` is
    # the legacy structured-constraint spelling and is handled as well.
    for raw_item in raw_items:
        item = dict(raw_item)
        typed_name = str(item.get("type") or "").strip().lower()
        if typed_name:
            typed_value = item.get("value", item.get("values", item))
            if typed_name in _CREATE_TASK_TEMPORAL_ALIASES and _has_owned_value(typed_value):
                temporal.setdefault("run_at", typed_value)
                continue
            if typed_name in _CREATE_TASK_PUBLICATION_ALIASES and _has_owned_value(typed_value):
                publication_intent = publication_intent or str(typed_value).strip()
                continue
            if typed_name in {"semantic_action", "semantic_operation"} and _has_owned_value(typed_value):
                semantic_operation = semantic_operation or str(typed_value).strip()
                continue

        kept: dict[str, Any] = {}
        for key, value in item.items():
            normalized = str(key).strip().lower()
            if not normalized or not _has_owned_value(value):
                continue
            if normalized in _CREATE_TASK_TEMPORAL_ALIASES:
                temporal.setdefault("run_at", value)
            elif normalized in {"temporal_base", "timezone"}:
                temporal.setdefault(normalized, value)
            elif normalized in _CREATE_TASK_PUBLICATION_ALIASES:
                publication_intent = publication_intent or str(value).strip()
            elif normalized in {"semantic_action", "semantic_operation"}:
                semantic_operation = semantic_operation or str(value).strip()
            elif normalized not in _CREATE_TASK_STRUCTURAL_FIELDS:
                kept[str(key)] = value
        if kept:
            owned_constraints.append(kept)

    direct_constraints: dict[str, Any] = {}
    for key, value in desired.items():
        normalized = str(key).strip().lower()
        if not normalized or not _has_owned_value(value):
            continue
        if normalized in _CREATE_TASK_STRUCTURAL_FIELDS:
            continue
        if normalized in _CREATE_TASK_TEMPORAL_ALIASES:
            # A direct delta field is more specific than its nested legacy
            # constraints representation.
            temporal["run_at"] = value
        elif normalized in {"temporal_base", "timezone"}:
            temporal[normalized] = value
        elif normalized in _CREATE_TASK_PUBLICATION_ALIASES:
            publication_intent = str(value).strip()
        else:
            direct_constraints[str(key)] = value
    if direct_constraints:
        owned_constraints.append(direct_constraints)

    raw_target = desired.get("resource_target") or desired.get("target")
    target = dict(raw_target) if isinstance(raw_target, Mapping) else {}
    return {
        "constraints": owned_constraints,
        "temporal_constraint": temporal,
        "publication_intent": publication_intent,
        "semantic_operation": semantic_operation,
        "target": target,
    }


def _command_scoped_to_goal_tree(command: Command, goal_tree: GoalTree) -> Command:
    """Return a Task-owned command envelope for a TASK_DELTA GoalTree.

    The original command is intentionally retained for regular single-task
    GoalTrees.  A TASK_DELTA tree, by contrast, was split from an aggregate
    user turn and must not carry its siblings' global constraints into a
    concurrent AgentLoop.
    """

    if str(getattr(goal_tree, "source", "") or "").upper() != "TASK_DELTA":
        return command
    executable = goal_tree.executable_goals()
    active_goal = executable[0] if len(executable) == 1 else goal_tree.root_goal
    if active_goal is None:
        return command
    constraints: dict[str, Any] = {}
    for item in getattr(active_goal, "constraints", ()) or ():
        if not isinstance(item, Mapping):
            continue
        typed_name = str(item.get("type") or "").strip().lower()
        if typed_name:
            constraints[typed_name] = item.get("value", item.get("values", item))
        else:
            constraints.update(dict(item))
    temporal = getattr(active_goal, "temporal_constraint", {}) or {}
    if isinstance(temporal, Mapping):
        constraints.update({
            str(key): value
            for key, value in temporal.items()
            if _has_owned_value(value)
        })
    if active_goal.publication_intent:
        constraints.setdefault("publication_intent", active_goal.publication_intent)
    capabilities = list(dict.fromkeys(
        capability
        for goal in goal_tree.all_goals()
        for capability in (getattr(goal, "required_capabilities", ()) or ())
        if str(capability).strip()
    ))
    target = dict(active_goal.target) if isinstance(active_goal.target, Mapping) else {}
    description = str(active_goal.description or command.requested_goal or "").strip()
    return command.model_copy(update={
        "goal": description,
        "objective": description,
        "first_action": "",
        "task_changes": [],
        "target": None,
        "parameters": {},
        "entities": {},
        "constraints": constraints,
        "semantic_operation": str(active_goal.semantic_operation or ""),
        "references": [],
        "required_capabilities": capabilities,
        "raw_input": description,
        "resolved_target": target or None,
    })


def _command_understanding(command: Any) -> dict[str, Any]:
    """Extract the user-visible task list from an understood Command.

    Emitted as the first business activity so the user can verify the agent
    understood the request (number of tasks, what each does, when it
    publishes) before it keeps executing (design goal 0813).
    """

    tasks: list[dict[str, Any]] = []
    for delta in (getattr(command, "task_changes", ()) or ()):
        if str(getattr(delta, "operation", "") or "") != "CREATE_TASK":
            continue
        changes = dict(getattr(delta, "desired_changes", {}) or {})
        constraints = dict(changes.get("constraints") or {})
        capabilities = {
            str(item).upper()
            for item in (changes.get("required_capabilities") or [])
        }
        tasks.append({
            "description": str(changes.get("description") or ""),
            "publish_at": str(constraints.get("run_at") or "") or None,
            "requires_search": "SEARCH_COMMUNITY" in capabilities,
        })
    if not tasks:
        capabilities = {
            str(item).upper()
            for item in (getattr(command, "required_capabilities", ()) or [])
        }
        constraints = dict(getattr(command, "constraints", {}) or {})
        tasks.append({
            "description": str(
                getattr(command, "goal", "")
                or getattr(command, "raw_input", "")
            ),
            "publish_at": str(constraints.get("run_at") or "") or None,
            "requires_search": "SEARCH_COMMUNITY" in capabilities,
        })
    return {
        "summary": f"我理解你安排了 {len(tasks)} 项内容",
        "tasks": tasks,
    }


def _artifact_type_for_capability(capability: str) -> str:
    """Map a completed capability to its canonical output artifact type.

    Observation-projected execution states carry capability/status but often
    not the output artifact type.  Goal satisfaction for capability goals
    requires a non-empty ``artifact_types`` fact, so the facts aggregator
    derives the artifact type from the completed capability when the entry
    does not declare one — otherwise a completed capability goal is never
    satisfied and AgentLoop resubmits it forever.
    """

    normalized = str(capability or "").strip().upper()
    return {
        "SEARCH_COMMUNITY": "SEARCH_RESULT",
        "GET_POST_DETAIL": "POST",
        "ANALYZE_CONTENT_PATTERNS": "ANALYSIS_REPORT",
        "GENERATE_CONTENT": "DRAFT",
        "SCHEDULE_PUBLISH": "SCHEDULE",
        "PUBLISH_NOW": "POST",
        "ANALYZE_PERFORMANCE": "ANALYSIS_REPORT",
        "VALIDATE_QUALITY": "VALIDATION_REPORT",
        "MANAGE_SCHEDULE": "SCHEDULE",
        "CANCEL_SCHEDULE": "SCHEDULE",
        "REVISE_DRAFT": "DRAFT",
    }.get(normalized, "")


def _compact_loop_execution_state(execution: Any) -> dict[str, Any]:
    """Compact projection of an in-loop tool Execution for the context snapshot.

    A full ``PlanExecution.model_dump()`` embeds step checkpoint data —
    ``completed_tool_result`` carries the whole tool response (e.g. the full
    body of every post GET_POST_DETAIL read).  Projecting that verbatim into
    ``context_snapshot.execution_states`` grew the LLM context past the
    provider limit (observed: 1.1M tokens requested).  Keep only the fields
    the continuation facts need: identity, goal/capability, status and the
    compact step outputs.
    """

    steps = list(getattr(execution, "steps", ()) or ())
    projected_steps = []
    for step in steps:
        output = getattr(step, "output_artifact", None)
        output_payload = {}
        if output is not None:
            if hasattr(output, "model_dump"):
                output_payload = output.model_dump(mode="json")
            else:
                output_payload = dict(output)
            if isinstance(output_payload, dict):
                summary = str(output_payload.get("summary") or "")
                if len(summary) > 500:
                    output_payload["summary"] = summary[:500] + "…[truncated]"
        projected_steps.append({
            "step_id": str(getattr(step, "step_id", "") or ""),
            "goal_id": str(getattr(step, "goal_id", "") or ""),
            "capability": str(getattr(step, "capability", "") or ""),
            "status": str(getattr(step, "status", "") or ""),
            "output_artifact": output_payload,
        })
    first = steps[0] if steps else None
    first_output = getattr(first, "output_artifact", None) if first is not None else None
    return {
        "execution_id": str(getattr(execution, "execution_id", "") or ""),
        "plan_id": str(getattr(execution, "plan_id", "") or ""),
        "task_id": str(getattr(execution, "task_id", "") or ""),
        "status": str(getattr(execution, "status", "") or ""),
        "goal_id": str(getattr(first, "goal_id", "") or "") if first is not None else "",
        "capability": str(getattr(first, "capability", "") or "") if first is not None else "",
        "artifact_type": (
            str(getattr(first_output, "artifact_type", "") or "")
            if first_output is not None
            else ""
        ),
        "steps": projected_steps,
    }


def _observation_post_id(observation: ActionObservation) -> str:
    """Resolve the post a GET_POST_DETAIL observation actually read."""

    for ref in observation.resource_refs:
        if str(ref.get("resource_type") or ref.get("kind") or "").upper() == "POST":
            value = str(ref.get("resource_id") or "")
            if value:
                return value
    result = observation.business_result or {}
    if isinstance(result, Mapping):
        value = str(result.get("post_id") or "")
        if value:
            return value
    return ""


def _merge_observation_evidence(context: Any, observation: ActionObservation) -> Any:
    """Inject durable business evidence into the context snapshot.

    AgentLoop observes artifacts/execution_states from the context snapshot;
    the evidence here comes from the terminal Execution result, never from
    the LLM plan or the user message.
    """

    if context is None:
        return context
    artifacts = list(getattr(context, "artifacts", []) or [])
    execution_states = list(getattr(context, "execution_states", []) or [])
    if observation.draft_id:
        artifacts.append({
            "artifact_id": next(iter(observation.artifact_refs), ""),
            "resource_type": "DRAFT",
            "resource_id": observation.draft_id,
            "title": str(
                observation.business_result.get("title")
                or observation.business_result.get("summary")
                or ""
            ),
            "status": "DRAFT",
            "step_id": observation.capability,
        })
        context.active_draft_id = observation.draft_id
    if observation.schedule_id:
        artifacts.append({
            "artifact_id": "",
            "resource_type": "SCHEDULE",
            "resource_id": observation.schedule_id,
            "status": "SCHEDULED",
            "step_id": observation.capability,
        })
        context.active_schedule_id = observation.schedule_id
    for item in observation.resource_refs:
        resource_id = str(item.get("resource_id") or "")
        resource_type = str(item.get("resource_type") or "")
        if resource_id and not any(
            existing.get("resource_id") == resource_id
            for existing in artifacts
        ):
            artifacts.append({
                "artifact_id": str(item.get("artifact_id") or ""),
                "resource_type": resource_type,
                "resource_id": resource_id,
                "step_id": str(item.get("step_id") or ""),
                "status": "DRAFT" if resource_type == "DRAFT" else "COMPLETED",
            })
    execution_states.append({
        "execution_id": observation.execution_id,
        "goal_id": observation.goal_id,
        "task_id": observation.task_id,
        "capability": observation.capability,
        "status": observation.status,
        "draft_id": observation.draft_id,
        "schedule_id": observation.schedule_id,
        # The post the observation read (GET_POST_DETAIL) and the posts a
        # SEARCH observation returned: incremental executions must be able to
        # resolve real post_id values for concrete-read steps without
        # inventing identifiers (design goal 0813 — grounded, never fake).
        "post_id": _observation_post_id(observation),
        "post_ids": [
            str(ref.get("resource_id") or "")
            for ref in observation.resource_refs
            if str(ref.get("resource_type") or ref.get("kind") or "").upper() == "POST"
            and ref.get("resource_id")
        ],
        "error": observation.error,
        "observed_at": observation.observed_at,
    })
    context.artifacts = artifacts
    # Bound the snapshot the same way as the in-loop projection: long pipelines
    # must not grow the LLM context without limit.
    context.execution_states = execution_states[-20:]
    context = _merge_goal_states(context, observation)
    return context


def _merge_goal_states(context: Any, observation: ActionObservation) -> Any:
    """Project per-Goal satisfaction state from durable business facts.

    AgentLoop observes this via the conversation context and decides the next
    semantic action for the current unsatisfied Goal — the LLM sees which
    Goals still need what, instead of re-deriving it from the user message.
    """

    payload = observation.payload.get("goal_tree") or {}
    if not payload:
        return context
    try:
        goal_tree = GoalTree.model_validate(payload)
    except Exception:
        return context
    execution_states = list(getattr(context, "execution_states", []) or [])
    facts_by_goal = _facts_from_execution_states(execution_states)
    context.unfinished_goals = [
        state
        for state in goal_states(goal_tree, facts_by_goal)
        if not bool(state.get("satisfied"))
    ]
    return context


def _completed_step_id(goal_tree: GoalTree, observation: ActionObservation) -> str:
    """Resolve the completed TaskNode for the observed capability.

    Falls back to the GoalCompiler step_id convention (``goal_id:index``)
    when the GoalTree carries no explicit TaskNodes, so a resumed Goal still
    advances past its completed first capability.
    """

    for node in goal_tree.task_nodes:
        if (
            str(getattr(node, "capability", "")) == observation.capability
            and str(getattr(node, "goal_id", "")) == observation.goal_id
        ):
            return str(getattr(node, "task_id", ""))
    goal = next(
        (
            item
            for item in goal_tree.executable_goals()
            if str(item.goal_id) == observation.goal_id
        ),
        None,
    )
    if goal is not None:
        capabilities = [
            str(value)
            for value in (getattr(goal, "required_capabilities", ()) or ())
            if str(value)
        ]
        if observation.capability in capabilities:
            return (
                f"{goal.goal_id}:{capabilities.index(observation.capability) + 1}"
            )
    return ""


def _write_goal(command: Any, session: Any) -> str:
    if command is not None:
        return str(getattr(command, "requested_goal", "") or "")
    # Resume/continuation has no command; fall back to the session's objective.
    return str(getattr(session, "goal", "") or getattr(session, "objective", "") or "")


def _fast_path_goal_category(capability: str) -> str:
    """Map a write capability to a goal category the CapabilityMapper accepts.

    The mapper rejects unknown categories with NO_CAPABILITY, so a single write
    must use a real category instead of a made-up "SINGLE" value.
    """
    normalized = str(capability or "").upper()
    if normalized in {"GENERATE_CONTENT", "MANAGE_DRAFT", "DELETE_DRAFT", "DELETE_POST", "GET_DRAFT"}:
        return "CREATE_CONTENT"
    if normalized in {
        "SCHEDULE_PUBLISH",
        "MANAGE_SCHEDULE",
        "CANCEL_SCHEDULE",
        "PUBLISH_NOW",
        "GET_SCHEDULE_STATUS",
    }:
        return "PUBLISH_CONTENT"
    return "COMPOSITE"


def _fast_path_stable_key(
    conversation_id: str,
    task_id: str,
    semantic_action: str,
    arguments: dict[str, Any],
    objective_id: str = "",
) -> str:
    """Stable idempotency key for one logical write operation."""
    import hashlib
    import json

    material = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{conversation_id}:{task_id}:{objective_id or 'task'}:{semantic_action}:{digest}"


def _ensure_runtime_result(value: Any) -> RuntimeResult:
    """Coerce a submission result into a typed RuntimeResult."""
    if isinstance(value, RuntimeResult):
        return value
    if isinstance(value, Mapping):
        return RuntimeResult(
            success=bool(value.get("ok", True)),
            status=str(value.get("status") or ("COMPLETED" if value.get("ok", True) else "FAILED")),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            execution_id=value.get("execution_id"),
            execution_path="fast_path",
            content=str(value.get("user_message") or value.get("message") or ""),
            summary=str(value.get("message") or ""),
            error_code=str(value.get("code") or ""),
            error_message=str(value.get("error") or ""),
        )
    return RuntimeResult(
        success=False,
        status="FAILED",
        execution_path="fast_path",
        error_code="FAST_WRITE_INVALID_SUBMISSION",
        error_message="Fast Path write submission returned an invalid result.",
    )


def _fast_path_target(command: Command) -> dict[str, Any] | None:
    """Project a compact business target for the durable ExecutionInput."""
    target = None
    if command is not None:
        target = command.resolved_target or (
            command.target.model_dump(mode="json") if command.target is not None else None
        )
    if not isinstance(target, Mapping):
        return None
    return {
        "resource_id": target.get("resource_id") or target.get("id"),
        "resource_kind": str(target.get("kind") or target.get("resource_kind") or "TASK").upper(),
        "task_id": target.get("task_id"),
    }


def _fast_path_target_context(command: Command, task_id: str) -> TargetContext | None:
    """Build the task-scoped TargetContext from the resolved target."""
    target = None
    if command is not None:
        target = command.resolved_target or (
            command.target.model_dump(mode="json") if command.target is not None else None
        )
    if not isinstance(target, Mapping):
        return None
    return TargetContext(
        task_id=str(target.get("task_id") or task_id),
        resource_id=target.get("resource_id") or target.get("id"),
        resource_kind=str(target.get("kind") or target.get("resource_kind") or "TASK").upper(),
    )


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _append_delta_goal(tree: GoalTree, delta: TaskDelta) -> GoalTree:
    """Append one new Goal from the delta to the existing tree (no LLM)."""
    description = str(
        delta.desired_changes.get("description")
        or delta.desired_changes.get("goal")
        or ""
    ).strip()
    if not description:
        raise TaskManagerError("ADD_GOAL requires a goal description.")
    goal_id = str(delta.desired_changes.get("goal_id") or f"delta-goal-{uuid.uuid4().hex[:8]}")
    dependencies: list[str] = []
    for ref in delta.dependency_reference:
        goal_id_ref = str(ref.get("goal_id") or "")
        if goal_id_ref:
            dependencies.append(goal_id_ref)
    temporal = delta.desired_changes.get("temporal_constraint") or {}
    publication = str(delta.desired_changes.get("publication_intent") or "")
    new_goal = Goal(
        goal_id=goal_id,
        description=description,
        goal_type="TASK",
        parent_goal=tree.root_goal.goal_id if tree.root_goal else None,
        required_capabilities=list(
            dict.fromkeys(delta.desired_changes.get("required_capabilities") or [])
        ),
        dependencies=dependencies,
        constraints=[dict(delta.desired_changes)] if delta.desired_changes else [],
        temporal_constraint=dict(temporal) if isinstance(temporal, Mapping) else {},
        publication_intent=publication,
        semantic_operation=str(delta.desired_changes.get("semantic_operation") or ""),
    )
    if tree.root_goal is not None:
        tree.root_goal.children.append(new_goal)
    else:
        raise TaskManagerError("ADD_GOAL requires an existing root Goal.")
    return tree


def _semantic_action_for_delta(delta: TaskDelta) -> str:
    """Return one declared canonical business action, never an inferred one."""

    desired = delta.desired_changes or {}
    raw = str(
        desired.get("semantic_action")
        or desired.get("semantic_operation")
        or ""
    ).strip().upper()
    # The interpreter/provider contract names the capability
    # ``SCHEDULE_PUBLISH`` while the existing ActionLoop tool action is
    # ``CREATE_SCHEDULE``.  This is enum canonicalization at the deterministic
    # boundary, not a change to the requested publication meaning.  Keeping
    # one runtime spelling is required so a cross-turn schedule mutation is
    # materialized as a new Objective instead of being silently ignored.
    if raw == "SCHEDULE_PUBLISH":
        raw = SemanticAction.CREATE_SCHEDULE.value
    return raw if raw in _SEMANTIC_ACTION_CAPABILITIES else ""


def _is_user_triggered_objective_retry(delta: TaskDelta) -> bool:
    """Return true only for the structured user-retry intent marker."""
    return is_failed_objective_retry(
        delta,
        dict(getattr(delta, "target_reference", None) or {}),
    )


def _objective_has_unreconciled_execution(task: Any, objective: Any) -> bool:
    """Do not create a blind user retry over an unknown write result."""

    objective_id = str(getattr(objective, "objective_id", "") or "")
    refs = [
        ref for ref in (getattr(task, "execution_refs", ()) or ())
        if str(getattr(ref, "goal_id", "") or "") == objective_id
    ]
    if not refs and len(list(getattr(task, "objectives", ()) or ())) == 1:
        refs = list(getattr(task, "execution_refs", ()) or ())
    statuses = {
        str(getattr(ref, "status", "") or "").upper()
        for ref in refs
    }
    return bool(statuses.intersection({
        "RESULT_UNKNOWN",
        "VERIFYING_RESULT",
        "WAITING_EXTERNAL",
        "SUBMITTED",
        "QUEUED",
        "RUNNING",
        "PROCESSING",
        "IN_PROGRESS",
        "WAITING_APPROVAL",
        "WAITING_HUMAN",
        "PAUSED",
    }))


def _task_status_value(task: Any) -> str:
    return str(
        getattr(getattr(task, "status", None), "value", None)
        or getattr(task, "status", "")
        or ""
    ).upper()


async def _conversation_has_exhausted_reconciliation(
    store: Any | None,
    conversation_id: str,
) -> bool:
    if store is None or not conversation_id:
        return False
    finder = getattr(store, "find_reconciliation_needed", None)
    if not callable(finder):
        return False
    try:
        value = finder(now="", limit=500)
    except TypeError:
        try:
            value = finder(limit=500)
        except TypeError:
            value = finder()
    values = await value if inspect.isawaitable(value) else value
    return any(
        str(getattr(operation, "conversation_id", "") or "") == str(conversation_id)
        and is_reconciliation_exhausted(operation)
        for operation in (values or ())
    )


def _retry_resource_refs(task: Any, objective: Any) -> list[TaskResourceRef]:
    """Return only the predecessor Objective's exact durable resources."""

    objective_id = str(getattr(objective, "objective_id", "") or "")
    related = {
        str(value)
        for value in (getattr(objective, "related_resource_ids", ()) or ())
        if value
    }
    result: list[TaskResourceRef] = []
    for raw in (getattr(task, "resource_index", ()) or ()):
        if isinstance(raw, Mapping):
            resource = TaskResourceRef.model_validate(raw)
        elif isinstance(raw, TaskResourceRef):
            resource = raw
        else:
            resource = TaskResourceRef(
                resource_id=str(getattr(raw, "resource_id", "") or ""),
                resource_kind=str(getattr(raw, "resource_kind", "") or ""),
                objective_id=str(getattr(raw, "objective_id", "") or "") or None,
                title=getattr(raw, "title", None),
                status=getattr(raw, "status", None),
                scheduled_at=getattr(raw, "scheduled_at", None),
            )
        resource_id = str(resource.resource_id or "")
        owner = str(resource.objective_id or "")
        if not resource_id or (owner and owner != objective_id):
            continue
        if owner == objective_id or resource_id in related:
            result.append(resource.model_copy(deep=True))
    return result


def _retry_remaining_capabilities(
    objective: Any,
    *,
    resource_kinds: set[str],
    user_changes_resource: bool,
    has_schedule_changes: bool,
) -> list[str]:
    """Keep only the failed Objective's unmet outcome capabilities."""

    old = [
        str(value).upper()
        for value in (getattr(objective, "required_capabilities", ()) or ())
        if str(value).strip()
    ]
    if not old:
        intent = str(getattr(objective, "intent", "") or "").upper()
        old = [
            {
                "CREATE_DRAFT": "GENERATE_CONTENT",
                "GENERATE_CONTENT": "GENERATE_CONTENT",
                "CREATE_SCHEDULE": "SCHEDULE_PUBLISH",
                "SCHEDULE_PUBLISH": "SCHEDULE_PUBLISH",
                "PUBLISH_NOW": "PUBLISH_NOW",
            }.get(intent, "")
        ]
        old = [value for value in old if value]

    remaining: list[str] = []
    for capability in old:
        if capability in {"GENERATE_CONTENT", "CREATE_DRAFT"} and "DRAFT" in resource_kinds:
            continue
        if capability == "SCHEDULE_PUBLISH" and "SCHEDULE" in resource_kinds:
            if has_schedule_changes and "MANAGE_SCHEDULE" not in remaining:
                remaining.append("MANAGE_SCHEDULE")
            continue
        if capability == "PUBLISH_NOW" and "POST" in resource_kinds:
            continue
        if capability not in remaining:
            remaining.append(capability)

    if (
        "SCHEDULE_PUBLISH" in remaining
        and "DRAFT" not in resource_kinds
        and "GENERATE_CONTENT" not in remaining
    ):
        # Preserve the original create-before-schedule outcome when the failed
        # Objective never produced its Draft.
        remaining.insert(0, "GENERATE_CONTENT")
    if (
        user_changes_resource
        and "DRAFT" in resource_kinds
        and "MANAGE_DRAFT" not in remaining
    ):
        remaining.insert(0, "MANAGE_DRAFT")
    return list(dict.fromkeys(remaining))


def _append_business_action_goal(tree: GoalTree, delta: TaskDelta) -> GoalTree:
    """Append a side-effect intent without destroying the Task/Goal anchor.

    A user may ask to cancel a publication while retaining its draft, or patch
    an already-completed article.  Those are new business operations against
    existing resources, not cancellation/replacement of the historical Goal.
    The durable action Goal makes that distinction visible to the existing
    compiler, AgentLoop, Worker, and reconciliation path.
    """

    semantic_action = _semantic_action_for_delta(delta)
    capability = _SEMANTIC_ACTION_CAPABILITIES[semantic_action]
    desired = dict(delta.desired_changes or {})
    temporal = desired.get("temporal_constraint")
    if not isinstance(temporal, Mapping):
        temporal = {"run_at": str(desired["run_at"])} if desired.get("run_at") else {}
    # ``resource_target`` is produced only after a Task-owned binding has
    # been checked.  Prefer it over the user's prose/Goal reference so an
    # action such as UPDATE_DRAFT crosses the Worker boundary with the exact
    # durable business id rather than a label that could be reinterpreted.
    target = desired.get("resource_target") or desired.get("target") or delta.target_reference
    target = dict(target) if isinstance(target, Mapping) else {}
    description = str(
        desired.get("description")
        or desired.get("instruction")
        or semantic_action.replace("_", " ").title()
    ).strip()
    action_goal = Goal(
        goal_id=f"operation-goal-{uuid.uuid4().hex[:12]}",
        description=description,
        goal_type="BUSINESS_OPERATION",
        parent_goal=tree.root_goal.goal_id,
        required_capabilities=[capability],
        constraints=[desired],
        semantic_operation=semantic_action,
        target=target,
        temporal_constraint=dict(temporal),
    )
    tree.root_goal.children.append(action_goal)
    return tree


class TaskDeltaGroundingError(TaskManagerError):
    """A TaskDelta Goal reference is stale, invalid, or ambiguous."""


def _has_delta_fields(values: Mapping[str, Any]) -> bool:
    """Return whether a mutation carries at least one meaningful field."""

    for value in values.values():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        if value:
            return True
    return False


def _delta_is_meaningful(delta: TaskDelta) -> bool:
    """Return whether a TaskDelta carries a real, actionable mutation.

    A reasoning model occasionally echoes a full independent request as an
    empty CREATE_TASK delta (no required_capabilities) or a bare UPDATE_GOAL
    (no desired fields, no target).  Executing such deltas would either build
    an empty Goal (AgentLoop flails) or bounce back to clarification forever;
    execute() therefore routes the message back to the fresh-request path when
    every declared change is meaningless.
    """

    operation = delta.operation
    if operation in {TaskDeltaOperation.NO_CHANGE, TaskDeltaOperation.ASK_USER}:
        return False
    if operation == TaskDeltaOperation.CREATE_TASK:
        return bool(delta.desired_changes.get("required_capabilities"))
    if operation in {
        TaskDeltaOperation.UPDATE_GOAL,
        TaskDeltaOperation.ADD_GOAL,
    }:
        return bool(delta.target_reference) or _has_delta_fields(delta.desired_changes)
    if operation in {
        TaskDeltaOperation.CANCEL_GOAL,
        TaskDeltaOperation.CANCEL_TASK,
        TaskDeltaOperation.CONTINUE_TASK,
    }:
        return bool(delta.target_reference)
    return False


def _goal_reference_matches(
    tree: GoalTree | None,
    *,
    goal_id: str,
    label: str,
) -> bool:
    if tree is None:
        return False
    if goal_id:
        return any(goal.goal_id == goal_id for goal in tree.all_goals())
    if not label:
        return False
    normalized = " ".join(label.casefold().split())
    exact = [
        goal
        for goal in tree.all_goals()
        if normalized == " ".join(goal.description.casefold().split())
    ]
    candidates = exact or [
        goal
        for goal in tree.all_goals()
        if normalized in " ".join(goal.description.casefold().split())
    ]
    return len(candidates) == 1


def _resolve_delta_goal(tree: GoalTree, delta: TaskDelta) -> Goal:
    """Resolve an UPDATE_GOAL reference without guessing a Goal.

    The outer TargetResolver already grounded the mutation to one owning Task;
    inside that Task's tree a deterministic reference (ordinal "第三篇",
    recency "刚刚那篇", ACTIVE) resolves to the single remaining candidate
    when the tree is a single-Goal task (the common delta shape).
    """

    reference = delta.target_reference or {}
    goal_id = str(reference.get("goal_id") or reference.get("id") or "").strip()
    label = str(
        reference.get("label")
        or reference.get("description")
        or reference.get("name")
        or ""
    ).strip()
    reference_type = str(reference.get("reference_type") or "").upper()
    ordinal = _delta_ordinal(reference, label)
    targets = tree.all_goals()
    if goal_id:
        matches = [goal for goal in targets if goal.goal_id == goal_id]
    elif label:
        from greenbook_agent_core.command.target import _normalized_label

        normalized = " ".join(_normalized_label(label).casefold().split())
        exact = [
            goal
            for goal in targets
            if normalized == " ".join(goal.description.casefold().split())
        ]
        # Preserve useful label references, but only when they identify one
        # Goal.  A broad substring is not permission to choose the first one.
        matches = exact or [
            goal
            for goal in targets
            if normalized in " ".join(goal.description.casefold().split())
        ]
    elif ordinal is not None or reference_type in {"ACTIVE", "RECENT", "LATEST"}:
        # Ordinal/recency citations were grounded to this Task by the outer
        # resolver; inside a single-Goal tree they name that Goal.
        matches = [targets[0]] if len(targets) == 1 else []
        if not matches and ordinal == 1 and targets:
            matches = [targets[0]]
    elif len(targets) == 1:
        # Bare mutation ("发布时间改成五分钟之后"): the outer resolver already
        # grounded it to this owning Task; its single Goal is the target.
        matches = [targets[0]]
    else:
        matches = []
    if len(matches) != 1:
        target = goal_id or label or "<missing reference>"
        raise TaskDeltaGroundingError(
            f"UPDATE_GOAL requires one grounded Goal; got {len(matches)} for {target}."
        )
    return matches[0]


def _delta_ordinal(reference: Mapping[str, Any], label: str) -> int | None:
    import re

    try:
        raw = reference.get("ordinal")
        if raw not in (None, ""):
            return int(raw)
    except (TypeError, ValueError):
        pass
    text = (label or "").strip().casefold()
    if not text:
        return None
    match = re.search(r"第\s*([0-9]+|[一二两三四五六七八九十])\s*(?:篇|个|条|项)?", text)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return numerals.get(token)


def _patch_delta_goal(tree: GoalTree, delta: TaskDelta) -> GoalTree:
    """Patch one Goal's desired state from the delta."""
    target = _resolve_delta_goal(tree, delta)
    desired = delta.desired_changes or {}
    for field in ("description", "semantic_operation", "publication_intent"):
        if desired.get(field):
            setattr(target, field, str(desired[field]))
    temporal = desired.get("temporal_constraint")
    if isinstance(temporal, Mapping):
        target.temporal_constraint = dict(temporal)
    elif isinstance(temporal, str) and temporal:
        target.temporal_constraint = {"run_at": temporal}
    elif desired.get("run_at"):
        target.temporal_constraint = {"run_at": str(desired["run_at"])}
    if target.temporal_constraint:
        # Keep one source of truth: drop any stale run_at the delta-created
        # Goal carried in ``constraints`` so patch and creation agree.
        target.constraints = [
            {key: value for key, value in item.items() if key != "run_at"}
            if isinstance(item, Mapping)
            else item
            for item in (getattr(target, "constraints", ()) or ())
        ]
    if desired.get("required_capabilities"):
        target.required_capabilities = list(
            dict.fromkeys(desired["required_capabilities"])
        )
    return tree


def _cancel_delta_goal(tree: GoalTree, delta: TaskDelta) -> GoalTree:
    """Remove an unexecuted logical Goal from the tree.

    Canonical business actions (for example ``CANCEL_SCHEDULE``) are
    intercepted before this helper and become durable action Goals instead.
    This helper therefore never claims that an external business side effect
    has happened.
    """
    reference = delta.target_reference or {}
    goal_id = str(reference.get("goal_id") or "")
    label = str(reference.get("label") or reference.get("description") or "").strip()

    def strip(root: Goal) -> bool:
        removed = False
        kept: list[Goal] = []
        for child in root.children:
            if goal_id and child.goal_id == goal_id:
                removed = True
                continue
            if label and label in child.description:
                removed = True
                continue
            if strip(child):
                removed = True
            kept.append(child)
        root.children = kept
        return removed

    if tree.root_goal is not None:
        if (goal_id and tree.root_goal.goal_id == goal_id) or (
            label and label in tree.root_goal.description
        ):
            raise TaskManagerError(
                "CANCEL_GOAL cannot remove the root Goal; cancel the Task instead."
            )
        strip(tree.root_goal)
    return tree


__all__ = ["ConversationRuntimeAdapter"]


def _goal_run_at(goal: Any) -> str:
    """Extract the Goal's scheduled run_at (temporal_constraint or constraints)."""
    temporal = getattr(goal, "temporal_constraint", None)
    if isinstance(temporal, Mapping) and temporal.get("run_at"):
        return str(temporal["run_at"])
    for item in getattr(goal, "constraints", ()) or ():
        if isinstance(item, Mapping) and item.get("run_at"):
            return str(item["run_at"])
    return ""


def _first_goal_run_at(goal: Any | None) -> str:
    return _goal_run_at(goal) if goal is not None else ""


def _task_draft_title(task: Any) -> str:
    """Surface the owned Draft title so "Java 那篇" can match by content."""
    drafts: list[str] = []
    for ref in (getattr(task, "resource_index", ()) or ()):
        kind = str(
            getattr(ref, "resource_kind", "") or getattr(ref, "kind", "") or ""
        ).upper()
        if kind == "DRAFT":
            drafts.append(str(getattr(ref, "title", "") or ""))
    return drafts[0] if len(drafts) == 1 else ""


def _task_owned_resource_ids(task: Any) -> dict[str, list[str]]:
    """Return the Task's own durable resource bindings, grouped by kind."""

    result: dict[str, list[str]] = {"DRAFT": [], "SCHEDULE": [], "POST": []}
    for ref in (getattr(task, "resource_index", ()) or ()):
        if isinstance(ref, Mapping):
            kind = str(ref.get("resource_kind") or ref.get("kind") or "").upper()
            resource_id = str(ref.get("resource_id") or ref.get("id") or "")
        else:
            kind = str(
                getattr(ref, "resource_kind", "") or getattr(ref, "kind", "") or ""
            ).upper()
            resource_id = str(
                getattr(ref, "resource_id", "") or getattr(ref, "id", "") or ""
            )
        if kind in result and resource_id and resource_id not in result[kind]:
            result[kind].append(resource_id)
    return result


def _task_resource_targets(task: Any) -> list[dict[str, str]]:
    """Project durable Task bindings for strong resource-id resolution."""

    targets: list[dict[str, str]] = []
    task_id = str(getattr(task, "task_id", "") or "")
    for ref in getattr(task, "resource_index", ()) or ():
        if isinstance(ref, Mapping):
            kind = str(ref.get("resource_kind") or ref.get("kind") or "").upper()
            resource_id = str(ref.get("resource_id") or ref.get("id") or "")
            objective_id = str(ref.get("objective_id") or "")
            title = str(ref.get("title") or "")
            status = str(ref.get("status") or "")
            scheduled_at = str(ref.get("scheduled_at") or ref.get("run_at") or "")
        else:
            kind = str(
                getattr(ref, "resource_kind", "") or getattr(ref, "kind", "") or ""
            ).upper()
            resource_id = str(
                getattr(ref, "resource_id", "") or getattr(ref, "id", "") or ""
            )
            objective_id = str(getattr(ref, "objective_id", "") or "")
            title = str(getattr(ref, "title", "") or "")
            status = str(getattr(ref, "status", "") or "")
            scheduled_at = str(
                getattr(ref, "scheduled_at", "")
                or getattr(ref, "run_at", "")
                or ""
            )
        if not kind or not resource_id:
            continue
        targets.append({
            "kind": kind,
            "resource_id": resource_id,
            "task_id": task_id,
            "objective_id": objective_id,
            "title": title,
            "status": status,
            "scheduled_at": scheduled_at,
        })
    return targets


def _delta_resource_kind(delta: TaskDelta) -> str:
    """Return the typed resource kind carried by a canonical delta."""

    desired = dict(delta.desired_changes or {})
    action = _semantic_action_for_delta(delta)
    requirement = _SEMANTIC_ACTION_RESOURCE_REQUIREMENTS.get(action)
    if requirement is not None:
        return requirement[0]
    for source in (
        desired.get("resource_target"),
        desired.get("target"),
        delta.target_reference,
    ):
        if not isinstance(source, Mapping):
            continue
        value = str(source.get("kind") or source.get("resource_kind") or "")
        if value:
            return value.upper()
    return ""


def _delta_resource_id(delta: TaskDelta) -> str:
    """Extract only an explicit business resource id from a delta."""

    desired = dict(delta.desired_changes or {})
    action = _semantic_action_for_delta(delta)
    requirement = _SEMANTIC_ACTION_RESOURCE_REQUIREMENTS.get(action)
    fields = [requirement[1]] if requirement is not None else []
    fields.extend(("resource_id", "draft_id", "schedule_id", "post_id"))
    for source in (
        desired,
        desired.get("resource_target"),
        desired.get("target"),
        delta.target_reference,
    ):
        if not isinstance(source, Mapping):
            continue
        for field in fields:
            value = source.get(field)
            if value not in (None, ""):
                return str(value)
        target_kind = str(
            source.get("kind") or source.get("resource_kind") or ""
        ).upper()
        if target_kind in {"DRAFT", "SCHEDULE", "POST"} and source.get("id"):
            return str(source["id"])
    return ""


def _task_resource_owner_objective_id(
    task: Any,
    resource_id: str,
    resource_kind: str = "",
) -> str:
    """Return one persisted Objective owner, never a recency fallback."""

    rid = str(resource_id or "")
    kind = str(resource_kind or "").upper()
    indexed_owners: set[str] = set()
    objective_owners: set[str] = set()
    for ref in getattr(task, "resource_index", ()) or ():
        if isinstance(ref, Mapping):
            ref_id = str(ref.get("resource_id") or ref.get("id") or "")
            ref_kind = str(ref.get("resource_kind") or ref.get("kind") or "").upper()
            owner = str(ref.get("objective_id") or "")
        else:
            ref_id = str(
                getattr(ref, "resource_id", "") or getattr(ref, "id", "") or ""
            )
            ref_kind = str(
                getattr(ref, "resource_kind", "") or getattr(ref, "kind", "") or ""
            ).upper()
            owner = str(getattr(ref, "objective_id", "") or "")
        if ref_id == rid and (not kind or ref_kind == kind) and owner:
            # The typed durable ResourceBinding is the primary ownership
            # authority.  A later mutation Objective may also list the same
            # resource in ``related_resource_ids`` as its result; that is
            # lineage evidence, not a second physical owner.
            indexed_owners.add(owner)
    for objective in getattr(task, "objectives", ()) or ():
        objective_id = str(getattr(objective, "objective_id", "") or "")
        if not objective_id:
            continue
        if rid in {
            str(value) for value in (getattr(objective, "related_resource_ids", ()) or ())
        }:
            objective_owners.add(objective_id)
    if indexed_owners:
        return next(iter(indexed_owners)) if len(indexed_owners) == 1 else ""
    return next(iter(objective_owners)) if len(objective_owners) == 1 else ""


def _task_objective_owned_resource_ids(
    task: Any,
    objective_id: str,
    resource_kind: str,
) -> list[str]:
    """Intersect Objective ownership with typed Task ResourceBindings."""

    objective_id = str(objective_id or "")
    resource_kind = str(resource_kind or "").upper()
    if not objective_id:
        return []
    objective = next(
        (
            item for item in (getattr(task, "objectives", ()) or ())
            if str(getattr(item, "objective_id", "")) == objective_id
        ),
        None,
    )
    related = {
        str(value)
        for value in (getattr(objective, "related_resource_ids", ()) or ())
    } if objective is not None else set()
    # A later cross-turn mutation Objective owns the execution it initiated,
    # while its ``target_objective_id`` preserves the historical Objective it
    # operates on.  Follow that existing Objective lineage so a natural
    # reference to the article can still reach its persisted Schedule/Draft
    # without a latest/first-resource fallback or a new global graph.
    lineage = {objective_id}
    changed = True
    while changed:
        changed = False
        for candidate in getattr(task, "objectives", ()) or ():
            candidate_id = str(getattr(candidate, "objective_id", "") or "")
            constraints = dict(getattr(candidate, "constraints", {}) or {})
            parent_id = str(constraints.get("target_objective_id") or "")
            if candidate_id and parent_id in lineage and candidate_id not in lineage:
                lineage.add(candidate_id)
                related.update(
                    str(value)
                    for value in (getattr(candidate, "related_resource_ids", ()) or ())
                )
                changed = True
    result: list[str] = []
    for ref in getattr(task, "resource_index", ()) or ():
        if isinstance(ref, Mapping):
            rid = str(ref.get("resource_id") or ref.get("id") or "")
            kind = str(ref.get("resource_kind") or ref.get("kind") or "").upper()
            owner = str(ref.get("objective_id") or "")
        else:
            rid = str(getattr(ref, "resource_id", "") or getattr(ref, "id", "") or "")
            kind = str(getattr(ref, "resource_kind", "") or getattr(ref, "kind", "") or "").upper()
            owner = str(getattr(ref, "objective_id", "") or "")
        if not rid or kind != resource_kind:
            continue
        if (rid in related or owner in lineage) and rid not in result:
            result.append(rid)
    return result


_SEMANTIC_ACTION_RESOURCE_REQUIREMENTS: dict[str, tuple[str, str]] = {
    SemanticAction.GET_DRAFT.value: ("DRAFT", "draft_id"),
    SemanticAction.UPDATE_DRAFT.value: ("DRAFT", "draft_id"),
    SemanticAction.DELETE_DRAFT.value: ("DRAFT", "draft_id"),
    SemanticAction.DELETE_POST.value: ("POST", "post_id"),
    SemanticAction.CREATE_SCHEDULE.value: ("DRAFT", "draft_id"),
    SemanticAction.PUBLISH_NOW.value: ("DRAFT", "draft_id"),
    SemanticAction.GET_SCHEDULE.value: ("SCHEDULE", "schedule_id"),
    SemanticAction.UPDATE_SCHEDULE.value: ("SCHEDULE", "schedule_id"),
    SemanticAction.CANCEL_SCHEDULE.value: ("SCHEDULE", "schedule_id"),
}


def _bind_semantic_action_resource(delta: TaskDelta, task: Any) -> TaskDelta:
    """Attach one verified Task-owned resource to a business action Delta.

    Target resolution establishes the owning durable Task.  That is a
    necessary but insufficient guard for a write: a Task can eventually own
    multiple historical drafts or schedules.  When the resolver has selected
    an Objective, the action may use only that Objective's persisted
    ResourceBinding; otherwise the action may proceed only with one unambiguous
    Task-owned resource.  This converts a successful target match into a
    stable Tool argument without using conversation recency or a sibling
    Task's active resource.
    """

    semantic_action = _semantic_action_for_delta(delta)
    requirement = _SEMANTIC_ACTION_RESOURCE_REQUIREMENTS.get(semantic_action)
    if requirement is None:
        return delta

    resource_kind, field_name = requirement
    desired = dict(delta.desired_changes or {})
    candidate_ids: set[str] = set()
    objective_id = str(
        desired.get("objective_id")
        or desired.get("target_objective_id")
        or (delta.target_reference or {}).get("objective_id")
        or (delta.target_reference or {}).get("target_objective_id")
        or ""
    )

    direct_value = desired.get(field_name)
    if direct_value not in (None, ""):
        candidate_ids.add(str(direct_value))

    for raw_target in (
        desired.get("resource_target"),
        desired.get("target"),
        delta.target_reference,
    ):
        if not isinstance(raw_target, Mapping):
            continue
        value = raw_target.get(field_name) or raw_target.get("resource_id")
        target_kind = str(
            raw_target.get("kind") or raw_target.get("resource_kind") or ""
        ).upper()
        # ``id`` is a resource id only when its kind says so.  A Goal id or a
        # Task id in a target reference must never be mistaken for a Draft or
        # Schedule id.
        if value in (None, "") and target_kind == resource_kind:
            value = raw_target.get("id")
        if value not in (None, ""):
            candidate_ids.add(str(value))

    if len(candidate_ids) > 1:
        raise TaskDeltaGroundingError(
            "The business action carries more than one resource identifier."
        )

    # A resolver may carry the correct typed resource together with the
    # Objective that supplied its human label.  That Objective is not always
    # the resource owner (for example, a schedule named by the draft title
    # can resolve through a completed UPDATE_DRAFT Objective).  When the
    # resource binding itself identifies exactly one persisted owner, repair
    # only that owner link.  Never infer from recency or from a sibling's
    # active resource; an unowned or multiply-owned id still fails closed.
    if candidate_ids:
        candidate_id = next(iter(candidate_ids))
        owner_objective_id = _task_resource_owner_objective_id(
            task,
            candidate_id,
            resource_kind,
        )
        if not objective_id:
            objective_id = owner_objective_id
        elif owner_objective_id and owner_objective_id != objective_id:
            objective_id = owner_objective_id
        if owner_objective_id:
            # The typed ResourceBinding is authoritative for both sides of
            # the lineage.  Keeping a label-derived target_objective_id here
            # would allocate a valid new mutation while pointing its
            # predecessor link at a sibling Draft objective; the ActionLoop
            # ownership guard must then (correctly) fail closed.
            desired["target_objective_id"] = owner_objective_id
    objective_owned_ids = _task_objective_owned_resource_ids(
        task,
        objective_id,
        resource_kind,
    ) if objective_id else []
    owned_ids = (
        objective_owned_ids
        if objective_id
        else _task_owned_resource_ids(task)[resource_kind]
    )
    if candidate_ids:
        resource_id = next(iter(candidate_ids))
        if resource_id not in owned_ids:
            raise TaskDeltaGroundingError(
                "The requested resource is not bound to the resolved Objective/Task."
            )
    elif len(owned_ids) == 1:
        resource_id = owned_ids[0]
    else:
        # Zero means the Task has no verified resource to mutate; many means
        # the Task's historical bindings are ambiguous.  Both need a user
        # clarification rather than a latest-resource guess.
        raise TaskDeltaGroundingError(
            "The resolved Task does not have one unambiguous resource for this action."
        )

    task_id = str(getattr(task, "task_id", "") or "")
    desired[field_name] = resource_id
    if objective_id:
        desired["objective_id"] = objective_id
    existing_target = desired.get("resource_target")
    target = dict(existing_target) if isinstance(existing_target, Mapping) else {}
    target.update({
        "kind": resource_kind,
        "resource_id": resource_id,
        "id": resource_id,
        "task_id": task_id,
        field_name: resource_id,
    })
    if objective_id:
        target["objective_id"] = objective_id
        target["target_objective_id"] = objective_id
    desired["resource_target"] = target
    # Propagate the same resolved identity through the mutation reference.
    # ActionLoop's durable mutation key reads this field; leaving it as a
    # natural-language label would drop the resource dimension before the
    # submission/idempotency boundary.
    reference = dict(delta.target_reference or {})
    reference.update({
        "kind": resource_kind,
        "resource_id": resource_id,
        "id": resource_id,
        "task_id": task_id,
        field_name: resource_id,
    })
    if objective_id:
        reference["objective_id"] = objective_id
        reference["target_objective_id"] = objective_id
    return delta.model_copy(
        update={"desired_changes": desired, "target_reference": reference}
    )


def _session_scoped_to_task(session: SessionContext | Any, task: Any) -> SessionContext | Any:
    """Clone a session and expose only resources owned by ``task``.

    The clone is important when several Task deltas resume concurrently.  It
    makes an absent/ambiguous binding fail at the Tool boundary rather than
    borrowing a globally active resource owned by another Task.
    """

    copy_method = getattr(session, "model_copy", None)
    if not callable(copy_method):
        # Test doubles and legacy callers may not be a mutable SessionContext.
        # Do not pretend they are safely scoped; production requests are
        # coerced to SessionContext before reaching this path.
        return session
    scoped = copy_method(deep=True)
    task_id = str(getattr(task, "task_id", "") or "")
    if task_id:
        scoped.active_task_id = task_id
    owned = _task_owned_resource_ids(task)
    kind_fields = {
        "DRAFT": "active_draft_id",
        "SCHEDULE": "active_schedule_id",
        "POST": "active_post_id",
    }
    for kind, field in kind_fields.items():
        values = owned[kind]
        # Zero and many are both intentionally unbound: a Tool then requests
        # an explicit target instead of selecting a sibling/conversation item.
        setattr(scoped, field, values[0] if len(values) == 1 else None)

    recent = getattr(scoped, "recent_entities", None)
    if isinstance(recent, list):
        scoped.recent_entities = [
            entity
            for entity in recent
            if str(getattr(entity, "kind", "")).upper() not in kind_fields
            or str(getattr(entity, "entity_id", ""))
            in owned[str(getattr(entity, "kind", "")).upper()]
        ]
    return scoped


def _context_scoped_to_task(context: ContextSnapshot | Any, task: Any) -> ContextSnapshot | Any:
    """Filter a decision context to one Task's resources and executions."""

    copy_method = getattr(context, "model_copy", None)
    if not callable(copy_method):
        return context
    scoped = copy_method(deep=True)
    task_id = str(getattr(task, "task_id", "") or "")
    owned = _task_owned_resource_ids(task)
    if task_id:
        scoped.active_task_id = task_id
    scoped.active_draft_id = owned["DRAFT"][0] if len(owned["DRAFT"]) == 1 else None
    scoped.active_schedule_id = owned["SCHEDULE"][0] if len(owned["SCHEDULE"]) == 1 else None
    scoped.active_post_id = owned["POST"][0] if len(owned["POST"]) == 1 else None

    for field in ("active_tasks", "artifacts", "execution_states", "available_resources"):
        values = getattr(scoped, field, None)
        if isinstance(values, list):
            setattr(
                scoped,
                field,
                [
                    value for value in values
                    if isinstance(value, Mapping) and str(value.get("task_id") or "") == task_id
                ],
            )
    candidates = getattr(scoped, "target_candidates", None)
    if isinstance(candidates, list):
        scoped.target_candidates = [
            value for value in candidates
            if isinstance(value, Mapping)
            and (
                str(value.get("task_id") or "") == task_id
                or (
                    str(value.get("kind") or "").upper() == "TASK"
                    and str(value.get("id") or "") == task_id
                )
            )
        ]
    return scoped


def _delta_reference_is_blank(delta: Any) -> bool:
    """True when the delta carries no target reference at all.

    A bare mutation ("发布时间改成五分钟之后") leaves the reference empty;
    the adapter then falls back to the most recently active Task.  A supplied
    but ungroundable reference must still produce a clarification.
    """
    reference = getattr(delta, "target_reference", None) or {}
    if not isinstance(reference, Mapping):
        return False
    for key in ("goal_id", "id", "task_id", "label", "description", "name",
                "ordinal", "reference_type"):
        value = reference.get(key)
        if value not in (None, ""):
            return False
    return True


def _parse_updated_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
