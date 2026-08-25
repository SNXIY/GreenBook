"""Observe/Reason/Act/Reflect loop for the GreenBook Agent Intelligence layer."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from greenbook_contracts.events import EVENT_ACTION_COMPLETED, EVENT_SEMANTIC_ACTION
from greenbook_contracts.tool_contract import ToolMetadata, ToolRegistry
from pydantic import ValidationError

from greenbook_agent_core.command.models import Command
from greenbook_agent_core.execution.observation import (
    available_read_fallbacks,
    observation_evidence,
)
from greenbook_agent_core.execution.runtime.invocation_context import ToolInvocationContext
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.goal.satisfaction import (
    dependencies_satisfied,
    goal_is_satisfied,
    goal_states,
    publication_intent_of,
)
from greenbook_agent_core.llm_compat import (
    STRUCTURED_OUTPUT_RETRY_MAX_TOKENS,
    extract_top_level_json,
    structured_call,
    structured_provider_options,
)
from greenbook_agent_core.planning.dynamic import (
    DynamicPlanner,
    PlanningDecision,
    PlanningDecisionType,
)
from greenbook_agent_core.toolruntime.policy import (
    ToolExecutionMode,
    ToolPolicyGate,
)

from .actions import AgentAction, AgentActionType, AgentRunResult, Reflection
from .recovery import RecoveryKind, ResumeContext
from .selector import (
    ToolSelectionError,
    ToolSelector,
    validate_arguments_against_schema,
)
from .state import AgentState, AgentStatus, Observation

logger = logging.getLogger(__name__)


class AgentLoopError(RuntimeError):
    """Raised for an invalid AgentLoop composition or model response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StructuredOutputError(AgentLoopError):
    """Controlled failure after bounded structured-output recovery is exhausted.

    This is a reasoning failure, never a business failure.  The Run may end in a
    controlled FAILED/NEEDS_ATTENTION state with a user-safe message, while the
    technical detail (repair attempt, validation summary) travels separately so
    raw model output never reaches the user.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        technical: Mapping[str, Any],
    ) -> None:
        super().__init__(code, message)
        self.technical = dict(technical)


# Bounded structured-output recovery budget.  A single bad model serialization
# must never kill the whole Run: normalize, repair once, retry the reason once,
# then fail in a controlled way (design target 0813, §15/§18).
MAX_STRUCTURED_OUTPUT_REPAIRS = 1
MAX_REASON_RETRIES = 1

# A read action is a duplicate (and rejected) once it has been executed and
# returned a non-empty SUCCESS for the same scope.  Consecutive identical
# read attempts after rejection escalate to LOOP_DETECTED.
MAX_DUPLICATE_READ_ATTEMPTS = 2
# Three observations of the same unresolved business state are enough to
# prove that a plan-version bump is not progress.  This is deliberately
# generic and does not name a capability or a workflow.
MAX_NO_PROGRESS_REPEATS = 3

USER_SAFE_REASONING_FAILURE = "这一步没有顺利完成，我暂时无法继续执行。你可以让我重试。"

# Structured-output parse/schema failures that are safe to recover from with a
# bounded repair/retry.  Anything else (missing LLM, provider failure, invalid
# composition) propagates unchanged.
_RECOVERABLE_REASON_ERROR_CODES = {
    "AGENT_RESPONSE_INVALID_JSON",
    "AGENT_RESPONSE_EMPTY",
    "AGENT_ACTION_SCHEMA_INVALID",
}


Reasoner = Callable[[Observation, AgentState], Awaitable[AgentAction] | AgentAction]
Reflector = Callable[
    [Observation, AgentAction, Mapping[str, Any], AgentState],
    Awaitable[Reflection] | Reflection,
]


class AgentLoop:
    """Run a bounded Goal-driven Observe/Reason/Act/Reflect cycle.

    AgentLoop chooses the next action. It never owns execution lifecycle. Tool
    calls are handed to an injected ToolRuntime and task creation is handed to
    an injected Execution Runtime callback after GoalCompiler produces the
    existing graph/plan contracts.
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        tool_selector: ToolSelector | None = None,
        goal_compiler: GoalCompiler | None = None,
        reasoner: Reasoner | None = None,
        reflector: Reflector | None = None,
        dynamic_planner: DynamicPlanner | None = None,
        policy_gate: ToolPolicyGate | None = None,
        task_manager: Any | None = None,
        execution_submission: Any | None = None,
        context_builder: Any | None = None,
        capability_registry: Any | None = None,
        reasoning_result_recorder: Any | None = None,
        tool_result_recorder: Any | None = None,
        max_iterations: int = 8,
    ) -> None:
        self._llm = llm
        self._model = model
        self._selector = tool_selector or ToolSelector(llm=llm, model=model)
        self._compiler = goal_compiler or GoalCompiler()
        self._reasoner = reasoner
        self._reflector = reflector
        self._planner_explicit = dynamic_planner is not None
        self._planner = dynamic_planner or DynamicPlanner(llm=llm, model=model)
        self._policy_gate = policy_gate or ToolPolicyGate()
        self._task_manager = task_manager
        self._execution_submission = execution_submission
        self._context_builder = context_builder
        self._capability_registry = capability_registry
        self._reasoning_result_recorder = reasoning_result_recorder
        self._tool_result_recorder = tool_result_recorder
        self._max_iterations = max(1, max_iterations)

    async def run(
        self,
        command: Command,
        goal_tree: GoalTree,
        *,
        conversation_context: Any | None = None,
        available_tools: Sequence[ToolMetadata] | ToolRegistry | Any = (),
        memory_snapshot: Mapping[str, Any] | None = None,
        tool_runtime: Any = None,
        execution_runtime: Any = None,
        execution_submission: Any | None = None,
        task_manager: Any | None = None,
        task: Any | None = None,
        permission_context: Mapping[str, Any] | None = None,
        permission_scopes: Sequence[str] = (),
        approval_granted: bool = False,
        tool_submission: Any | None = None,
        llm: Any | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        context_builder: Any | None = None,
        context_scope: Mapping[str, Any] | None = None,
        resume_context: ResumeContext | Mapping[str, Any] | None = None,
        activity_callback: Callable[[str, Mapping[str, Any]], Awaitable[None] | None] | None = None,
        bootstrap_action: AgentAction | None = None,
        reasoning_result_recorder: Any | None = None,
        tool_result_recorder: Any | None = None,
    ) -> AgentRunResult:
        """Run AgentLoop for one canonical Command and GoalTree.

        ``activity_callback`` is invoked as soon as a semantic action is
        decided (before any tool/execution completes), letting the API layer
        push a real business activity over SSE without waiting for execution.
        """

        if not isinstance(command, Command):
            raise AgentLoopError("AGENT_COMMAND_INVALID", "AgentLoop requires a canonical Command.")
        if not isinstance(goal_tree, GoalTree):
            raise AgentLoopError("AGENT_GOAL_TREE_INVALID", "AgentLoop requires a GoalTree.")
        goal_tree.validate_tree()
        tools = _normalize_tools(available_tools)
        context = _context_payload(conversation_context)
        resume = _resume_context(resume_context)
        state = AgentState(
            goal=goal_tree.root_goal,
            current_task=None,
            conversation_context=context,
            available_tools=tools,
            memory_snapshot=dict(memory_snapshot or {}),
            command=command,
            goal_tree=goal_tree,
            task=task,
            plan_version=int(getattr(task, "plan_version", 0) or 0),
            resume_context=(resume.model_dump(mode="json") if resume is not None else {}),
            completed_task_ids=(
                list(resume.completed_step_ids)
                if resume is not None else []
            ),
            completed_goal_ids=(
                list(resume.completed_goal_ids)
                if resume is not None else []
            ),
        )
        _set_context_state(state, conversation_context, context)
        state.current_task = _next_task(state)
        last_result: dict[str, Any] = {}
        if resume is not None:
            last_result = {
                "recovery_action": resume.recovery_action.value,
                "recovery_reason": resume.recovery_reason,
                "failed_step_id": resume.failed_step_id,
                "failed_error_code": resume.failed_error_code,
                "replayed_completed_steps": list(resume.completed_step_ids),
            }
            if resume.recovery_action == RecoveryKind.WAIT_FOR_HUMAN:
                state.status = AgentStatus.WAITING_HUMAN
                return self._result(
                    state,
                    question=resume.recovery_reason or "Human input is required to resume.",
                    content=resume.recovery_reason,
                )
            if resume.recovery_action == RecoveryKind.ABORT_TASK:
                state.status = AgentStatus.FAILED
                state.finished = True
                state.last_error = resume.recovery_reason
                return self._result(state, error_message=resume.recovery_reason)
        limit = max(1, max_iterations or self._max_iterations)
        selected_model = model if model is not None else self._model

        while not state.finished and state.iteration < limit:
            state.iteration += 1
            await self._refresh_context(
                state,
                command=command,
                context_builder=context_builder or self._context_builder,
                context_scope=context_scope,
            )
            # Advance the current TaskNode after completed/submitted work so
            # the observation, activity payloads and reasoner always see the
            # step actually being executed (a search finished in iteration 1
            # must not keep labeling the generate step "SEARCH").
            state.current_task = _next_task(state) or state.current_task
            observation = self.observe(state, last_result)
            try:
                if state.iteration == 1:
                    state.timings.setdefault("agent_loop_started_at", _now_timing())
                    if bootstrap_action is None:
                        state.timings.setdefault("first_reason_started_at", _now_timing())
                if state.iteration == 1 and bootstrap_action is not None:
                    # Phase 3.5 bootstrap: understanding already produced a
                    # validated first action; do not re-ask reason for it.
                    action = bootstrap_action
                elif state.preferred_tool_name:
                    action = AgentAction(
                        action=AgentActionType.TOOL_CALL,
                        tool_name=state.preferred_tool_name,
                        tool_args=dict(state.preferred_tool_arguments),
                        reason="Dynamic Planner selected an evidence-bounded alternative tool.",
                    )
                else:
                    action = await self.reason(
                        observation,
                        state,
                        llm=llm,
                        model=selected_model,
                    )
                if state.iteration == 1:
                    state.timings["first_reason_completed_at"] = _now_timing()
                    state.timings["first_action_decided_at"] = _now_timing()
                if activity_callback is not None:
                    emitted = activity_callback(
                        EVENT_SEMANTIC_ACTION,
                        _activity_payload(state, action),
                    )
                    if inspect.isawaitable(emitted):
                        await emitted
                state.history.append({"type": "ACTION", **action.model_dump(mode="json")})
                synthetic_result: dict[str, Any] | None = None
                if action.action == AgentActionType.FINISH:
                    if self._goal_tree_finished_ok(state):
                        state.finished = True
                        state.status = AgentStatus.COMPLETED
                        return self._result(state, content=action.reason)
                    # Premature FINISH is a controlled rejection, never a
                    # completion: feed it back as an Observation so the model
                    # re-reasons with the concrete unsatisfied state.
                    rejections = int(getattr(state, "premature_finish_rejections", 0)) + 1
                    state.premature_finish_rejections = rejections
                    if rejections >= 2:
                        raise AgentLoopError(
                            "GOAL_NOT_SATISFIED",
                            "The model kept requesting FINISH while required Goals were unsatisfied.",
                        )
                    synthetic_result = {
                        "ok": False,
                        "code": "GOAL_NOT_SATISFIED",
                        "error_code": "GOAL_NOT_SATISFIED",
                        "retryable": False,
                        "message": (
                            "The Goal is not yet satisfied; a premature FINISH was rejected. "
                            "Continue the remaining required actions."
                        ),
                    }
                if action.action == AgentActionType.ASK_USER:
                    state.status = AgentStatus.WAITING_HUMAN
                    return self._result(
                        state,
                        question=action.question or action.reason,
                        content=action.reason,
                    )
                if synthetic_result is not None:
                    last_result = synthetic_result
                    state.tool_results.append(dict(last_result))
                elif action.action == AgentActionType.PRODUCE_RESULT:
                    if not self._produce_result_allowed(state):
                        if self._current_reasoning_capability(state):
                            # A reasoning Goal (synthesis) is NOT_READY until it
                            # has real evidence from THIS run.  Steer deterministically
                            # toward the evidence-producing search instead of feeding
                            # a generic tool error or letting the recorder fail on
                            # model-invented source_refs.  Bound it: if the model
                            # keeps producing without evidence, fail fast.
                            rejections = int(getattr(state, "deterministic_rejections", 0)) + 1
                            state.deterministic_rejections = rejections
                            if rejections >= 3:
                                raise AgentLoopError(
                                    "NEED_EVIDENCE",
                                    "当前没有真实的社区检索证据，无法基于空证据生成总结。",
                                )
                            last_result = {
                                "ok": False,
                                "code": "NEED_EVIDENCE",
                                "error_code": "NEED_EVIDENCE",
                                "retryable": True,
                                "request_sent": False,
                                "tool_name": "",
                                "message": (
                                    "当前 Task 还没有任何真实的社区检索证据。必须先执行 "
                                    "SEARCH_COMMUNITY / community.search_public_posts "
                                    "获取帖子，再基于真实结果生成总结。"
                                ),
                            }
                        else:
                            last_result = {
                                "ok": False,
                                "code": "WRONG_EXECUTION_SEMANTICS",
                                "error_code": "WRONG_EXECUTION_SEMANTICS",
                                "retryable": False,
                                "request_sent": False,
                                "message": (
                                    "PRODUCE_RESULT is only valid for a reasoning-backed Goal; "
                                    "tool-backed side effects must execute through Worker."
                                ),
                            }
                    else:
                        last_result = await self._produce_reasoning_result(
                            action,
                            state,
                            reasoning_result_recorder=(
                                reasoning_result_recorder
                                or self._reasoning_result_recorder
                            ),
                        )
                else:
                    last_result = await self.act(
                        action,
                        observation,
                        state,
                        tool_runtime=tool_runtime,
                        execution_runtime=execution_runtime,
                        execution_submission=execution_submission or self._execution_submission,
                        task_manager=task_manager or self._task_manager,
                        permission_context=permission_context,
                        permission_scopes=permission_scopes,
                        approval_granted=approval_granted,
                        tool_submission=tool_submission,
                        llm=llm,
                        model=selected_model,
                    )
                    if (
                        action.action == AgentActionType.TOOL_CALL
                        and _is_successful_direct_read(state, last_result)
                    ):
                        recorder = tool_result_recorder or self._tool_result_recorder
                        if recorder is not None:
                            recorded = recorder(
                                state=state,
                                action=action,
                                result=last_result,
                            )
                            recorded = (
                                await recorded
                                if inspect.isawaitable(recorded)
                                else recorded
                            )
                            if isinstance(recorded, Mapping):
                                last_result.update(dict(recorded))
                if synthetic_result is None and activity_callback is not None:
                    completed = activity_callback(
                        EVENT_ACTION_COMPLETED,
                        {
                            **dict(_activity_payload(state, action)),
                            **{
                                key: last_result.get(key)
                                for key in ("execution_id", "task_id", "goal_id")
                                if last_result.get(key)
                            },
                            "ok": bool(
                                last_result.get("ok", last_result.get("success", False))
                            ),
                            "result": _normalize_result(last_result),
                        },
                    )
                    if inspect.isawaitable(completed):
                        await completed
                if synthetic_result is None and action.action == AgentActionType.TOOL_CALL:
                    # A multi-goal side-effecting TOOL_CALL is deliberately
                    # upgraded in ``act`` to the complete GoalTree plan. It
                    # remains a TOOL_CALL in the LLM action history, but the
                    # durable result is an execution submission and must be
                    # observed as such by subsequent Agent state consumers.
                    if last_result.get("submitted_full_goal_tree_plan"):
                        state.execution_results.append(dict(last_result))
                    else:
                        state.tool_results.append(dict(last_result))
                elif (
                    synthetic_result is None
                    and action.action
                    in {AgentActionType.CREATE_TASK, AgentActionType.PRODUCE_RESULT}
                ):
                    state.execution_results.append(dict(last_result))
                state.history.append({"type": "RESULT", "value": last_result})
                _remember_root_failure(state, last_result)

                # Queue submission is the hand-off boundary between Agent
                # Intelligence and the durable Execution Runtime.  Once a
                # plan or side-effecting tool has been accepted by the queue,
                # this loop must stop reasoning about the same action.  The
                # Worker owns completion, retry, recovery, and the eventual
                # projection back to the conversation.  Continuing here
                # would let the LLM submit a duplicate action while the first
                # execution is still running (or already completed).
                queue_accepted = (
                    bool(last_result.get("queued"))
                    or str(last_result.get("status", "")).upper()
                    in {"QUEUED", "SUBMITTED"}
                )
                if queue_accepted and action.action in {
                    AgentActionType.CREATE_TASK,
                    AgentActionType.TOOL_CALL,
                }:
                    state.finished = True
                    state.status = AgentStatus.RUNNING
                    return self._result(
                        state,
                        content=(
                            str(last_result.get("message") or "")
                            or "Execution accepted by the durable queue."
                        ),
                    )

                if last_result.get("status") == AgentStatus.WAITING_HUMAN.value:
                    state.status = AgentStatus.WAITING_HUMAN
                    return self._result(
                        state,
                        question=str(last_result.get("question") or last_result.get("error_message") or "Approval is required."),
                        content=str(last_result.get("message") or ""),
                    )

                # Re-observe after the action so Reflect and DynamicPlanner
                # receive the concrete result evidence.  Reusing the
                # pre-action Observation hid failure_kind and the available
                # fallback list from the replanner, which could turn a real
                # dependency outage into an unsafe same-tool retry or an
                # unexplained wait state.
                result_observation = self.observe(state, last_result)
                reflection = await self.reflect(
                    result_observation,
                    action,
                    last_result,
                    state,
                    llm=llm,
                    model=selected_model,
                )
                state.history.append({"type": "REFLECTION", **reflection.model_dump(mode="json")})
                planner_trigger = self._planner_explicit and (
                    result_observation.result_status in {"EMPTY", "FAILED"}
                    or last_result.get("ok") is False
                    or last_result.get("success") is False
                )
                if (reflection.finished or not reflection.needs_next_step) and not planner_trigger:
                    state.finished = True
                    state.status = (
                        AgentStatus.COMPLETED
                        if bool(last_result.get("ok", last_result.get("success", False)))
                        else AgentStatus.FAILED
                    )
                    if state.status == AgentStatus.FAILED:
                        state.last_error = str(
                            last_result.get("error_code")
                            or last_result.get("error_message")
                            or reflection.reason
                        )
                    return self._result(state, content=reflection.reason)
                if reflection.retry and last_result.get("ok") is False:
                    state.last_error = str(last_result.get("error_code") or "AGENT_ACTION_FAILED")
                has_runtime_observation = bool(
                    state.tool_results or state.execution_results
                )
                if planner_trigger or (reflection.adjust_plan and has_runtime_observation):
                    if _no_progress_detected(state, last_result):
                        state.status = AgentStatus.FAILED
                        state.finished = True
                        state.last_error = "NO_PROGRESS_DETECTED"
                        return self._result(
                            state,
                            error_code="NO_PROGRESS_DETECTED",
                            error_message=_user_safe_error(
                                "NO_PROGRESS_DETECTED",
                                "Agent state did not make business progress.",
                            ),
                        )
                    decision = await self.replan(
                        result_observation,
                        state,
                        task=state.task,
                        llm=llm,
                        model=selected_model,
                    )
                    state.planning_decisions.append(decision.model_dump(mode="json"))
                    state.history.append({"type": "PLANNING_DECISION", **decision.model_dump(mode="json")})
                    if decision.decision == PlanningDecisionType.ASK_HUMAN:
                        state.status = AgentStatus.WAITING_HUMAN
                        state.last_error = state.last_error or "EVIDENCE_INSUFFICIENT"
                        return self._result(state, question=decision.reason, content=decision.reason)
                    if decision.decision == PlanningDecisionType.ABORT:
                        state.status = AgentStatus.FAILED
                        state.last_error = decision.reason or "Dynamic Planner aborted the Task."
                        return self._result(state, error_message=state.last_error)
                    if decision.decision == PlanningDecisionType.FINISH:
                        state.finished = True
                        state.status = AgentStatus.COMPLETED
                        return self._result(state, content=decision.reason)
                    if decision.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL or decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS:
                        self._set_preferred_tool(state, decision)
                    if state.goal_tree is not None and decision.decision in {
                        PlanningDecisionType.INSERT_STEP,
                        PlanningDecisionType.REMOVE,
                        PlanningDecisionType.REORDER,
                        PlanningDecisionType.RETRY_WITH_NEW_ARGS,
                    }:
                        state.goal_tree = self._planner.apply(state.goal_tree, decision)
                        state.goal = state.goal_tree.root_goal
                        state.current_task = _next_task(state)
                    manager = task_manager or self._task_manager
                    if (
                        state.task is not None
                        and getattr(state.task, "task_id", "")
                        and state.goal_tree is not None
                        and decision.decision in {
                            PlanningDecisionType.INSERT_STEP,
                            PlanningDecisionType.REMOVE,
                            PlanningDecisionType.REORDER,
                            PlanningDecisionType.RETRY_WITH_NEW_ARGS,
                        }
                    ):
                        bind_tree = getattr(manager, "bind_goal_tree", None) if manager is not None else None
                        if callable(bind_tree):
                            updated = bind_tree(state.task.task_id, state.goal_tree)
                            state.task = await updated if inspect.isawaitable(updated) else updated
                    if state.task is not None and getattr(state.task, "task_id", ""):
                        record = getattr(manager, "record_replan", None) if manager is not None else None
                        if callable(record):
                            updated = record(
                                state.task.task_id,
                                decision=decision.decision.value,
                                observation=observation.model_dump(mode="json"),
                                reason=decision.reason,
                            )
                            state.task = await updated if inspect.isawaitable(updated) else updated
                            state.plan_version = int(getattr(state.task, "plan_version", state.plan_version) or state.plan_version)
                if state.goal_tree is not None:
                    state.current_task = _next_task(state)
            except StructuredOutputError as exc:
                state.status = AgentStatus.FAILED
                state.last_error = USER_SAFE_REASONING_FAILURE
                state.reasoning_failure = dict(exc.technical)
                _remember_root_failure(
                    state,
                    {
                        "ok": False,
                        "error_code": exc.code,
                        "error_message": str(exc),
                    },
                )
                logger.warning(
                    "agent_reason controlled_failure code=%s iteration=%s model=%s technical=%s",
                    exc.code,
                    state.iteration,
                    selected_model,
                    json.dumps(exc.technical, ensure_ascii=False, default=str),
                )
                return self._result(
                    state,
                    error_code=exc.code,
                    error_message=USER_SAFE_REASONING_FAILURE,
                )
            except (AgentLoopError, ToolSelectionError) as exc:
                code = str(getattr(exc, "code", "AGENT_LOOP_FAILED"))
                if code in {
                    "TOOL_CAPABILITY_MISMATCH",
                    "TOOL_NOT_IN_CATALOG",
                    "TOOL_SELECTION_EMPTY",
                }:
                    # Deterministic tool-selection rejection: the model may be
                    # corrected ONCE (a bounded replan), but repeating the same
                    # rejection is a hard path failure — it must never burn the
                    # whole iteration budget in a busy-loop.
                    state.deterministic_rejections = (
                        state.deterministic_rejections + 1
                    )
                    if state.deterministic_rejections <= 1:
                        state.last_error = str(exc)
                        rejection: dict[str, Any] = {
                            "ok": False,
                            "error_code": code,
                            "error_message": str(exc),
                            "retryable": True,
                            "tool_name": str(getattr(action, "tool_name", "") or ""),
                        }
                        state.tool_results.append(dict(rejection))
                        state.history.append({"type": "RESULT", "value": rejection})
                        _remember_root_failure(state, rejection)
                        continue
                    # Second identical deterministic rejection: fail this path
                    # instead of re-reasoning against the same impossible tool.
                    state.status = AgentStatus.FAILED
                    state.last_error = str(exc)
                    _remember_root_failure(
                        state,
                        {
                            "ok": False,
                            "error_code": code,
                            "error_message": str(exc),
                            "retryable": False,
                        },
                    )
                    return self._result(
                        state,
                        error_code=code,
                        error_message=str(exc),
                    )
                state.status = AgentStatus.FAILED
                state.last_error = str(exc)
                _remember_root_failure(
                    state,
                    {
                        "ok": False,
                        "error_code": code,
                        "error_message": str(exc),
                    },
                )
                return self._result(
                    state,
                    error_code=code,
                    error_message=_user_safe_error(code, str(exc)),
                )
            except Exception as exc:
                state.status = AgentStatus.FAILED
                state.last_error = str(exc)
                _remember_root_failure(
                    state,
                    {
                        "ok": False,
                        "error_code": "AGENT_LOOP_FAILED",
                        "error_message": str(exc),
                    },
                )
                return self._result(
                    state,
                    error_code="AGENT_LOOP_FAILED",
                    error_message=_user_safe_error("", str(exc)),
                )

        state.status = AgentStatus.MAX_ITERATIONS
        state.last_error = "AgentLoop reached its iteration limit."
        return self._result(
            state,
            error_code="AGENT_MAX_ITERATIONS",
            error_message=_user_safe_error("AGENT_MAX_ITERATIONS", state.last_error),
        )

    @staticmethod
    def _set_preferred_tool(state: AgentState, decision: PlanningDecision) -> None:
        """Accept only a catalog member as a planner's alternative-tool hint."""

        if not decision.tool_name:
            raise AgentLoopError(
                "ALTERNATIVE_TOOL_MISSING",
                "SELECT_ALTERNATIVE_TOOL requires a tool_name.",
            )
        if not any(item.name == decision.tool_name for item in state.available_tools):
            raise AgentLoopError(
                "ALTERNATIVE_TOOL_NOT_IN_CATALOG",
                f"Planner selected unavailable tool '{decision.tool_name}'.",
            )
        state.preferred_tool_name = decision.tool_name
        state.preferred_tool_arguments = dict(decision.arguments or {})

    async def _refresh_context(
        self,
        state: AgentState,
        *,
        command: Command,
        context_builder: Any | None,
        context_scope: Mapping[str, Any] | None,
    ) -> None:
        """Refresh the bounded working set before each Observe step."""

        builder = context_builder
        if builder is None or not callable(getattr(builder, "build", None)):
            return
        scope = dict(context_scope or {})
        if not scope.get("conversation_id"):
            return
        value = builder.build(
            conversation_id=str(scope.get("conversation_id", "")),
            user_id=str(scope.get("user_id", "")),
            tenant_id=str(scope.get("tenant_id", "")),
            timezone=str(scope.get("timezone", "Asia/Shanghai")),
            session=scope.get("session"),
            current_command=command,
            current_goal=state.goal,
        )
        value = await value if inspect.isawaitable(value) else value
        payload = _context_payload(value)
        _set_context_state(state, value, payload)

    def observe(self, state: AgentState, last_result: Mapping[str, Any] | None = None) -> Observation:
        """Collect the current Goal, task, context, and runtime evidence."""

        task = state.current_task.model_dump(mode="json") if state.current_task else {}
        evidence = _observation_evidence(last_result or {})
        # The reasoner's goal_states must reflect the LIVE durable evidence every
        # iteration, not the one-time resume snapshot.  A prerequisite capability
        # completed mid-run (e.g. a reasoning PRODUCE_RESULT) must mark that Goal
        # satisfied so the reasoner advances to the remaining capabilities instead
        # of re-targeting the already-finished one.  goal_is_satisfied keeps the
        # multi-capability rule: a Goal is only satisfied when EVERY declared
        # required_capability has verified evidence, so one completed
        # prerequisite never prematurely completes the Objective.
        if state.goal_tree is not None and isinstance(state.resume_context, Mapping):
            state.resume_context = {
                **state.resume_context,
                "goal_states": goal_states(
                    state.goal_tree,
                    _facts_by_goal_from_state(state),
                ),
            }
        observation = Observation(
            goal=state.goal.model_dump(mode="json") if state.goal else {},
            current_task=task,
            current_task_status=state.current_task.status if state.current_task else "",
            conversation_context=dict(state.conversation_context),
            tool_results=list(state.tool_results),
            execution_results=list(state.execution_results),
            last_result=dict(last_result or {}),
            summary=(
                f"iteration={state.iteration}; "
                f"tool_results={len(state.tool_results)}; "
                f"execution_results={len(state.execution_results)}"
            ),
            context_snapshot_id=state.context_snapshot_id,
            memory_ids_used=list(state.memory_ids_used),
            artifacts=list(state.context_snapshot.get("artifacts", [])),
            execution_states=list(state.context_snapshot.get("execution_states", [])),
            waiting_human=dict(state.context_snapshot.get("waiting_human", {})),
            resume_context=dict(state.resume_context),
            result_status=evidence["result_status"],
            resource_count=evidence["resource_count"],
            missing_required_reference=evidence["missing_required_reference"],
            available_fallback_capabilities=_available_read_fallbacks(
                state.available_tools,
                failed_tool=str((last_result or {}).get("tool_name") or ""),
            ),
            failure_kind=evidence["failure_kind"],
        )
        state.observations.append(observation)
        return observation

    async def reason(
        self,
        observation: Observation,
        state: AgentState,
        *,
        llm: Any | None = None,
        model: str = "",
    ) -> AgentAction:
        if self._reasoner is not None:
            result = self._reasoner(observation, state)
            return await result if inspect.isawaitable(result) else result
        client = llm or self._llm
        if client is None:
            raise AgentLoopError("AGENT_REASON_LLM_UNAVAILABLE", "AgentLoop Reason requires an LLM.")
        request = self._reason_request(observation, state)
        schema = AgentAction.model_json_schema()
        schema_name = "greenbook_agent_action"
        parse_failures: list[dict[str, Any]] = []
        repairs_used = 0
        retries_used = 0
        pending_response: Any = None
        while True:
            if pending_response is None:
                response = await structured_call(
                    client,
                    model,
                    _REASON_PROMPT,
                    schema_name,
                    schema,
                    request,
                )
            else:
                response = pending_response
                pending_response = None
            try:
                action = AgentAction.model_validate(
                    _normalize_agent_action_payload(_response_payload(response)),
                )
                logger.info(
                    "agent_reason iteration=%s model=%s status=ok",
                    state.iteration,
                    model,
                )
                return action
            except (ValidationError, AgentLoopError) as exc:
                code = getattr(exc, "code", "AGENT_ACTION_SCHEMA_INVALID")
                if code not in _RECOVERABLE_REASON_ERROR_CODES:
                    raise
                last_content = _extract_content(response)
                summary = _truncate(str(exc), 600)
                parse_failures.append({"code": code, "summary": summary})
                logger.warning(
                    "agent_reason iteration=%s model=%s status=parse_failed code=%s "
                    "repairs=%s retries=%s",
                    state.iteration,
                    model,
                    code,
                    repairs_used,
                    retries_used,
                )
                if repairs_used < MAX_STRUCTURED_OUTPUT_REPAIRS:
                    repairs_used += 1
                    try:
                        pending_response = await _repair_structured_output(
                            client,
                            model,
                            schema_name=schema_name,
                            schema=schema,
                            original_content=last_content,
                            error_summary=summary,
                        )
                        logger.info(
                            "agent_reason iteration=%s model=%s status=repair_attempt attempt=%s",
                            state.iteration,
                            model,
                            repairs_used,
                        )
                        continue
                    except Exception:
                        logger.warning(
                            "agent_reason iteration=%s model=%s status=repair_failed attempt=%s",
                            state.iteration,
                            model,
                            repairs_used,
                        )
                if retries_used < MAX_REASON_RETRIES:
                    retries_used += 1
                    logger.info(
                        "agent_reason iteration=%s model=%s status=reason_retry attempt=%s",
                        state.iteration,
                        model,
                        retries_used,
                    )
                    continue
                raise StructuredOutputError(
                    code="STRUCTURED_OUTPUT_INVALID",
                    message=USER_SAFE_REASONING_FAILURE,
                    technical={
                        "reasoning_type": "reason",
                        "parse_failures": parse_failures,
                        "repair_attempted": repairs_used > 0,
                        "repair_succeeded": False,
                        "iteration": state.iteration,
                        "model": model,
                    },
                ) from exc

    def _reason_request(
        self,
        observation: Observation,
        state: AgentState,
    ) -> dict[str, Any]:
        return {
            "command": state.command.model_dump(mode="json") if state.command else {},
            "goal_tree": state.goal_tree.model_dump(mode="json") if state.goal_tree else {},
            "observation": observation.model_dump(mode="json"),
            "available_tool_metadata": [_metadata_payload(item) for item in state.available_tools],
            "runtime_evidence_constraints": _read_evidence_constraints(state),
            "memory_snapshot": state.memory_snapshot,
        }

    async def act(
        self,
        action: AgentAction,
        observation: Observation,
        state: AgentState,
        *,
        tool_runtime: Any = None,
        execution_runtime: Any = None,
        execution_submission: Any = None,
        task_manager: Any = None,
        permission_context: Mapping[str, Any] | None = None,
        permission_scopes: Sequence[str] = (),
        approval_granted: bool = False,
        tool_submission: Any = None,
        llm: Any | None = None,
        model: str = "",
    ) -> dict[str, Any]:
        if action.action == AgentActionType.TOOL_CALL:
            preferred_tool = state.preferred_tool_name
            preferred_arguments = dict(state.preferred_tool_arguments)
            selected = await self._selector.select(
                state.goal,
                observation,
                state.available_tools,
                requested_tool=action.tool_name or preferred_tool,
                requested_arguments=action.tool_args or preferred_arguments,
            )
            if preferred_tool:
                # A planner-selected alternative is a one-action hint. The
                # catalog and policy gate still validate the actual call.
                state.preferred_tool_name = ""
                state.preferred_tool_arguments = {}
            if self._requires_full_goal_tree_submission(state, selected.metadata):
                # LLM action selection is intentionally flexible, but it
                # cannot be allowed to collapse several independently
                # executable business Goals into one durable tool step. The
                # compiler is the canonical path that preserves every Goal's
                # identity, constraints, and dependencies.
                result = await self._create_task(
                    state,
                    execution_runtime,
                    execution_submission=execution_submission,
                    task_manager=task_manager,
                    llm=llm,
                    model=model,
                )
                result.setdefault("submitted_full_goal_tree_plan", True)
                result.setdefault("selected_tool_name", selected.tool_name)
                return result
            result = await self._invoke_tool(
                selected.tool_name,
                selected.arguments,
                state,
                tool_runtime,
                metadata=selected.metadata,
                permission_context=permission_context,
                permission_scopes=permission_scopes,
                approval_granted=approval_granted,
                tool_submission=tool_submission,
                execution_runtime=execution_runtime,
            )
            result.setdefault("tool_name", selected.tool_name)
            result.setdefault("tool_arguments", dict(selected.arguments))
            result.setdefault("selection_reason", selected.reason)
            return result
        if action.action == AgentActionType.CREATE_TASK:
            return await self._create_task(
                state,
                execution_runtime,
                execution_submission=execution_submission,
                task_manager=task_manager,
                llm=llm,
                model=model,
            )
        if action.action == AgentActionType.UPDATE_PLAN:
            return self._update_plan(action, state)
        raise AgentLoopError(
            "AGENT_ACTION_UNSUPPORTED",
            f"Unsupported AgentAction: {action.action}",
        )

    @staticmethod
    def _requires_full_goal_tree_submission(
        state: AgentState,
        metadata: ToolMetadata | None,
    ) -> bool:
        """Keep multi-goal side effects on the GoalCompiler submission path.

        A read-only tool can still be used as an in-loop observation for a
        multi-goal conversation. A side effect, approval, or destructive
        operation cannot: queueing only that selected tool would discard the
        other executable Goal leaves before the durable runtime sees them.
        """

        goal_tree = state.goal_tree
        if goal_tree is None or len(goal_tree.executable_goals()) <= 1:
            return False
        if metadata is None:
            return False
        policy = metadata.policy
        return bool(
            policy.side_effect.has_side_effect
            or policy.side_effect.destructive
            or policy.requires_approval
        )

    async def reflect(
        self,
        observation: Observation,
        action: AgentAction,
        result: Mapping[str, Any],
        state: AgentState,
        *,
        llm: Any | None = None,
        model: str = "",
    ) -> Reflection:
        if self._reflector is not None:
            value = self._reflector(observation, action, result, state)
            return await value if inspect.isawaitable(value) else value
        client = llm or self._llm
        if client is None:
            # A deterministic failure mode is used only when an embedding
            # application supplies its own Reasoner but no Reflector.
            # Queue acceptance is a successful Agent decision even though the
            # Execution result is intentionally not complete yet.
            ok = bool(
                result.get("ok", result.get("success", False))
                or result.get("queued", False)
                or str(result.get("status", "")).upper() in {"QUEUED", "SUBMITTED"}
            )
            return Reflection(
                finished=ok and action.action == AgentActionType.CREATE_TASK,
                needs_next_step=not (ok and action.action == AgentActionType.CREATE_TASK),
                retry=not ok,
                reason="Deterministic reflection fallback.",
            )
        request = {
            "goal": state.goal.model_dump(mode="json") if state.goal else {},
            "observation": observation.model_dump(mode="json"),
            "action": action.model_dump(mode="json"),
            "result": dict(result),
        }
        schema = Reflection.model_json_schema()
        repairs_used = 0
        retries_used = 0
        pending_response: Any = None
        while True:
            if pending_response is None:
                response = await structured_call(
                    client,
                    model,
                    _REFLECT_PROMPT,
                    "greenbook_agent_reflection",
                    schema,
                    request,
                )
            else:
                response = pending_response
                pending_response = None
            try:
                return Reflection.model_validate(
                    _normalize_reflection_payload(_response_payload(response)),
                )
            except (ValidationError, AgentLoopError) as exc:
                code = getattr(exc, "code", "AGENT_REFLECTION_SCHEMA_INVALID")
                if code not in _RECOVERABLE_REASON_ERROR_CODES and code != "AGENT_REFLECTION_SCHEMA_INVALID":
                    raise
                summary = _truncate(str(exc), 600)
                logger.warning(
                    "agent_reflect model=%s status=parse_failed code=%s repairs=%s retries=%s",
                    model,
                    code,
                    repairs_used,
                    retries_used,
                )
                if repairs_used < MAX_STRUCTURED_OUTPUT_REPAIRS:
                    repairs_used += 1
                    try:
                        pending_response = await _repair_structured_output(
                            client,
                            model,
                            schema_name="greenbook_agent_reflection",
                            schema=schema,
                            original_content=_extract_content(response),
                            error_summary=summary,
                        )
                        continue
                    except Exception:
                        logger.warning(
                            "agent_reflect model=%s status=repair_failed attempt=%s",
                            model,
                            repairs_used,
                        )
                if retries_used < MAX_REASON_RETRIES:
                    retries_used += 1
                    continue
                # A failed reflection is an internal control signal, not a
                # business failure.  Fall back to a deterministic reflection so
                # the loop continues with the observed result instead of killing
                # the Run over a control-plane JSON error.
                logger.warning(
                    "agent_reflect model=%s status=fallback_deterministic code=%s",
                    model,
                    code,
                )
                ok = bool(
                    result.get("ok", result.get("success", False))
                    or result.get("queued", False)
                    or str(result.get("status", "")).upper()
                    in {"QUEUED", "SUBMITTED"}
                )
                return Reflection(
                    finished=False,
                    needs_next_step=True,
                    retry=not ok,
                    adjust_plan=not ok,
                    reason="Reflection structured output was unavailable; continuing from the observed result.",
                )

    async def _invoke_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        state: AgentState,
        tool_runtime: Any,
        *,
        metadata: ToolMetadata | None,
        permission_context: Mapping[str, Any] | None,
        permission_scopes: Sequence[str],
        approval_granted: bool,
        tool_submission: Any,
        execution_runtime: Any,
    ) -> dict[str, Any]:
        if tool_runtime is None:
            raise AgentLoopError(
                "TOOL_RUNTIME_UNAVAILABLE",
                "TOOL_CALL requires an injected ToolRuntime.",
            )
        if metadata is None:
            raise AgentLoopError(
                "TOOL_METADATA_REQUIRED",
                "ToolSelector must return metadata before invocation.",
            )
        # Last-mile schema validation before any in-loop call: the selector
        # normally validates, but a custom selector (or a direct tool
        # submission) must not send malformed arguments downstream.
        try:
            validate_arguments_against_schema(
                metadata.name,
                dict(arguments),
                metadata.input_schema,
            )
        except ToolSelectionError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "error_code": exc.code,
                "retryable": False,
                "request_sent": False,
                "message": str(exc),
            }
        reject = self._reject_equivalent_read(state, tool_name, arguments, metadata)
        if reject is not None:
            return reject
        # A read-only observation must return to AgentLoop so the model can
        # reason over it and choose the next step.  ``multi_step`` is not a
        # reason by itself to detach a read: detaching it as a one-step
        # execution loses the observation and prematurely terminates a
        # multi-goal conversation.  Durable queue hand-off remains required
        # for side effects, approvals, retries, and long-running operations.
        policy_metadata = metadata.policy
        durable_tool = bool(
            policy_metadata.side_effect.has_side_effect
            or policy_metadata.side_effect.destructive
            or policy_metadata.requires_approval
            or policy_metadata.retry_policy.max_attempts > 1
            or policy_metadata.timeout_seconds > 120.0
        )
        policy = self._policy_gate.evaluate(
            metadata,
            scopes=permission_scopes,
            approval_granted=approval_granted,
            context=permission_context,
            multi_step=bool(
                durable_tool
                and state.goal_tree
                and len(state.goal_tree.task_nodes) > 1
            ),
            max_cost=(
                float(permission_context["max_tool_cost"])
                if permission_context is not None
                and permission_context.get("max_tool_cost") is not None
                else None
            ),
        )
        if policy.mode == ToolExecutionMode.DENY:
            raise AgentLoopError("TOOL_POLICY_DENIED", policy.reason)
        if policy.mode == ToolExecutionMode.WAITING_HUMAN:
            return {
                "ok": False,
                "status": AgentStatus.WAITING_HUMAN.value,
                "approval_required": True,
                "tool_name": tool_name,
                "message": policy.reason,
            }
        if policy.mode == ToolExecutionMode.QUEUE:
            submitter = tool_submission or getattr(tool_runtime, "submit", None)
            if submitter is None and execution_runtime is not None:
                submitter = getattr(execution_runtime, "submit_tool", None)
            if not callable(submitter):
                raise AgentLoopError(
                    "TOOL_QUEUE_UNAVAILABLE",
                    "Tool policy requires queue submission, but no submission boundary was injected.",
                )
            queued = submitter(
                tool_name=tool_name,
                arguments=dict(arguments),
                state=state,
            )
            queued = await queued if inspect.isawaitable(queued) else queued
            result = _normalize_result(queued)
            # Only a real queue acceptance is a hand-off boundary.  A failed
            # or rejected submission must not be reported as in-flight work:
            # the loop re-observes the failure and re-routes instead.
            submitted_status = str(result.get("status") or "").upper()
            accepted = (
                bool(result.get("ok"))
                or bool(result.get("success"))
                or submitted_status in {"QUEUED", "SUBMITTED", "ACCEPTED"}
                or bool(result.get("queued"))
            )
            if accepted:
                result["queued"] = True
            else:
                result.pop("queued", None)
                result["ok"] = False
            result.setdefault("policy_mode", policy.mode.value)
            return result
        step_id = f"agent-{state.iteration}"
        invoke = getattr(tool_runtime, "invoke", None)
        if callable(invoke):
            context = ToolInvocationContext.build(
                task_id=state.goal.goal_id if state.goal else "agent-goal",
                execution_id="",
                step_id=step_id,
                capability="",
                tool_name=tool_name,
                tool_args=dict(arguments),
            )
            raw = invoke(context)
            raw = await raw if inspect.isawaitable(raw) else raw
            return _normalize_result(raw)
        execute_tool = getattr(tool_runtime, "execute_tool", None)
        if callable(execute_tool):
            raw = execute_tool(tool_name, **dict(arguments))
            raw = await raw if inspect.isawaitable(raw) else raw
            return _normalize_result(raw)
        if callable(tool_runtime):
            raw = tool_runtime(tool_name, dict(arguments))
            raw = await raw if inspect.isawaitable(raw) else raw
            return _normalize_result(raw)
        raise AgentLoopError(
            "TOOL_RUNTIME_INVALID",
            "Injected ToolRuntime has no invoke/execute_tool interface.",
        )

    def _reject_equivalent_read(
        self,
        state: AgentState,
        tool_name: str,
        arguments: Mapping[str, Any],
        metadata: ToolMetadata | None,
    ) -> dict[str, Any] | None:
        """Generic anti-loop guard for equivalent read actions (not Search-only).

        A read that already returned a non-empty SUCCESS for the same scope is
        consumed evidence; executing it again adds nothing.  The rejection is
        returned as a structured Observation so the AgentLoop re-reasons with
        the feedback instead of failing the Run.  A materially different query,
        target, or empty prior result is explicitly allowed to re-read.
        """

        if metadata is None or not _is_read_tool_metadata(metadata):
            return None
        prior = self._prior_successful_read(state, tool_name, arguments)
        if prior is None:
            return None
        if not _equivalent_read_scope(prior, arguments):
            return None
        signature = _read_signature(tool_name, arguments)
        counts = dict(getattr(state, "duplicate_read_counts", None) or {})
        current = counts.get(signature, 0) + 1
        counts[signature] = current
        state.duplicate_read_counts = counts
        if current >= MAX_DUPLICATE_READ_ATTEMPTS:
            logger.warning(
                "agent_loop_detected tool=%s scope=%s attempts=%s",
                tool_name,
                signature,
                current,
            )
            return {
                "ok": False,
                "code": "LOOP_DETECTED",
                "error_code": "LOOP_DETECTED",
                "duplicate_guard": True,
                "retryable": False,
                "message": (
                    "重复的相同读取动作被拦截。请使用已有内容继续，或明确需要新内容的理由。"
                ),
            }
        return {
            "ok": False,
            "code": "EQUIVALENT_ACTION_ALREADY_SUCCEEDED",
            "error_code": "EQUIVALENT_ACTION_ALREADY_SUCCEEDED",
            "duplicate_guard": True,
            "retryable": False,
            "message": (
                "Equivalent read action already succeeded; use the existing "
                "evidence or change the query/target before reading again."
            ),
        }

    def _prior_successful_read(
        self,
        state: AgentState,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Find a prior successful/empty read with the same tool scope."""

        for result in reversed(state.tool_results):
            if str(result.get("tool_name") or "") != tool_name:
                continue
            evidence = observation_evidence(result)
            if evidence["result_status"] not in {"SUCCESS", "EMPTY"}:
                continue
            if _equivalent_read_scope(
                {
                    "arguments": dict(result.get("tool_arguments") or {}),
                    "result_status": evidence["result_status"],
                },
                arguments,
            ):
                return {
                    "arguments": dict(result.get("tool_arguments") or {}),
                    "result_status": evidence["result_status"],
                }
        goal_id = str(getattr(state.current_task, "goal_id", "") or "")
        if goal_id:
            for es in reversed(
                list(state.context_snapshot.get("execution_states") or [])
            ):
                if str(es.get("goal_id") or "") != goal_id:
                    continue
                if str(es.get("status") or "").upper() not in {"COMPLETED", "SUCCESS"}:
                    continue
                capability = str(es.get("capability") or "")
                metadata = next(
                    (
                        item
                        for item in state.available_tools
                        if _is_read_tool_metadata(item)
                        and capability
                        in {
                            str(value).upper()
                            for value in (getattr(item, "capabilities", ()) or ())
                        }
                    ),
                    None,
                )
                if metadata is None or str(getattr(metadata, "name", "")) != tool_name:
                    continue
                return {
                    "arguments": _scope_arguments_for_execution_evidence(state, goal_id),
                    "result_status": "SUCCESS",
                }
        return None

    def _goal_tree_finished_ok(self, state: AgentState) -> bool:
        """Return whether every executable Goal is provably satisfied.

        FINISH is only accepted when the desired business state exists in the
        durable facts (draft/schedule/post for publication goals, completed
        required capabilities for observation-only goals).  A premature FINISH
        is rejected and fed back as an Observation.
        """

        if state.goal_tree is None:
            return True
        executable = list(state.goal_tree.executable_goals())
        if not executable:
            return True
        for goal in executable:
            goal_id = str(getattr(goal, "goal_id", ""))
            intent = _publication_intent_of(goal)
            if intent == "DRAFT_ONLY":
                # An owned Draft is the provable desired result.
                if not _goal_facts(state, goal_id).get("draft_id"):
                    return False
                continue
            if intent in {"SCHEDULED_PUBLISH", "IMMEDIATE_PUBLISH"}:
                facts = _goal_facts(state, goal_id)
                if not _goal_satisfied(goal, facts):
                    return False
                continue
            # No explicit publication intent: a Goal that declares
            # content-production capabilities (draft/schedule/publish) is
            # still in flight until every capability has completed — a draft
            # alone must not finish a draft+schedule Goal.  Pure
            # observation/analysis Goals keep the historical model-judgment
            # behaviour (their reads are guarded by the duplicate-read guard).
            declared = {
                str(value).upper()
                for value in (getattr(goal, "required_capabilities", ()) or ())
                if str(value)
            }
            if (
                declared & {
                    "GENERATE_CONTENT",
                    "SCHEDULE_PUBLISH",
                    "PUBLISH_NOW",
                }
                and not _goal_satisfied(goal, _goal_facts(state, goal_id))
            ):
                return False
            continue
        return True

    async def _produce_reasoning_result(
        self,
        action: AgentAction,
        state: AgentState,
        *,
        reasoning_result_recorder: Any,
    ) -> dict[str, Any]:
        """Execute a reasoning-backed Goal in the same AgentLoop.

        Consumes existing Observation/Artifact evidence, validates the model's
        structured result against a minimal contract, persists it as a durable
        reasoning execution + artifact (lineage via ``source_refs``), and marks
        the Goal satisfied through the execution evidence.  No ToolSelector and
        no Worker/Queue hand-off — this is a reasoning step, not a tool call.
        """

        goal_id = str(getattr(state.current_task, "goal_id", "") or "")
        capability = str(getattr(state.current_task, "capability", "") or "")
        if not capability:
            goal_id, capability = self._next_reasoning_goal(state)
        payload = dict(action.result_payload or {})
        result_type = str(action.result_type or "CONTENT_ANALYSIS")
        source_refs = [str(item) for item in (action.source_refs or [])]
        if not source_refs:
            raise StructuredOutputError(
                code="REASONING_RESULT_INVALID",
                message=USER_SAFE_REASONING_FAILURE,
                technical={
                    "reasoning_type": "produce_result",
                    "result_type": result_type,
                    "goal_id": goal_id,
                    "capability": capability,
                    "repair_attempted": False,
                    "validation_error": "reasoning result requires source_refs",
                },
            )
        content = str(payload.get("summary") or payload.get("content") or "")
        key_points = payload.get("key_points") or payload.get("key_patterns") or payload.get("points") or []
        if not content and not key_points:
            raise StructuredOutputError(
                code="STRUCTURED_OUTPUT_INVALID",
                message=USER_SAFE_REASONING_FAILURE,
                technical={
                    "reasoning_type": "produce_result",
                    "result_type": result_type,
                    "goal_id": goal_id,
                    "capability": capability,
                    "repair_attempted": False,
                    "validation_error": "reasoning result has no summary or key_points",
                },
            )
        task_id = str(
            getattr(getattr(state, "task", None), "task_id", "")
            or state.conversation_context.get("task_id", "")
            or state.conversation_context.get("active_task_id", "")
            or ""
        )
        conversation_id = str(state.conversation_context.get("conversation_id", ""))
        result: dict[str, Any] = {
            "ok": True,
            "success": True,
            "status": "COMPLETED",
            "goal_id": goal_id,
            "capability": capability,
            "result_type": result_type,
            "artifact_type": "ANALYSIS_REPORT",
            "source_refs": source_refs,
            "task_id": task_id,
            "reasoning_result": {
                "summary": content,
                "key_points": (
                    list(key_points)
                    if isinstance(key_points, list)
                    else [str(key_points)]
                ),
                "payload": dict(payload),
            },
            "execution_id": "",
            "artifact_id": "",
        }
        if reasoning_result_recorder is not None:
            try:
                recorded = reasoning_result_recorder(
                    goal_id=goal_id,
                    capability=capability,
                    result_type=result_type,
                    payload=payload,
                    source_refs=source_refs,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    user_id=str(state.conversation_context.get("user_id") or ""),
                    tenant_id=str(state.conversation_context.get("tenant_id") or ""),
                )
                recorded = await recorded if inspect.isawaitable(recorded) else recorded
            except Exception as exc:
                raise StructuredOutputError(
                    code="REASONING_RESULT_COMMIT_FAILED",
                    message=USER_SAFE_REASONING_FAILURE,
                    technical={
                        "reasoning_type": "produce_result",
                        "goal_id": goal_id,
                        "capability": capability,
                        "source_refs": source_refs,
                        "commit_error": str(exc),
                    },
                ) from exc
            if isinstance(recorded, Mapping):
                result.update(dict(recorded))
        # A reasoning result is only a business fact once the recorder has
        # confirmed a durable write.  Fabricating COMPLETED evidence in
        # memory would let the Goal be reported satisfied (and the Run
        # "completed") without any persisted execution/artifact, and a crash
        # would lose it.  Fail closed instead of inventing completion.
        persisted_execution_id = str(result.get("execution_id") or "")
        if not persisted_execution_id:
            recorded_payload = {}
            if reasoning_result_recorder is not None and isinstance(recorded, Mapping):
                recorded_payload = dict(recorded)
            raise StructuredOutputError(
                code="REASONING_RESULT_NOT_PERSISTED",
                message=USER_SAFE_REASONING_FAILURE,
                technical={
                    "reasoning_type": "produce_result",
                    "goal_id": goal_id,
                    "capability": capability,
                    "result_type": result_type,
                    "recorder_configured": reasoning_result_recorder is not None,
                    "recorded": recorded_payload,
                },
            )
        evidence = list(state.context_snapshot.get("execution_states") or [])
        evidence.append({
            "execution_id": persisted_execution_id,
            "plan_id": f"reasoning:{task_id}:{goal_id}:{capability}",
            "goal_id": goal_id,
            "task_id": task_id,
            "capability": capability,
            "status": "COMPLETED",
            "artifact_id": result.get("artifact_id") or "",
            "artifact_type": result.get("artifact_type") or "ANALYSIS_REPORT",
            "steps": [{
                "goal_id": goal_id,
                "capability": capability,
                "status": "COMPLETED",
                "output_artifact": {
                    "artifact_id": result.get("artifact_id") or "",
                    "artifact_type": result.get("artifact_type") or "ANALYSIS_REPORT",
                    "summary": content[:2000],
                },
            }],
            "observed_at": _now_timing(),
        })
        state.context_snapshot["execution_states"] = evidence
        artifacts = list(state.context_snapshot.get("artifacts") or [])
        artifact_id = str(result.get("artifact_id") or "")
        if artifact_id and not any(
            isinstance(item, Mapping) and str(item.get("artifact_id") or "") == artifact_id
            for item in artifacts
        ):
            artifacts.append({
                "artifact_id": artifact_id,
                "artifact_type": result.get("artifact_type") or "ANALYSIS_REPORT",
                "task_id": task_id,
                "execution_id": result.get("execution_id") or "",
                "step_id": f"{goal_id}:reasoning",
                "summary": content[:2000],
                "goal_id": goal_id,
                "source_refs": source_refs,
            })
        state.context_snapshot["artifacts"] = artifacts
        return result

    def _current_reasoning_capability(self, state: AgentState) -> str:
        """Resolve the current Goal's capability name, if any."""
        registry = self._capability_registry or getattr(self._compiler, "_registry", None)
        capability_name = str(getattr(state.current_task, "capability", "") or "")
        if not capability_name and state.goal_tree is not None:
            goal_id = str(getattr(state.current_task, "goal_id", "") or "")
            for goal in state.goal_tree.executable_goals():
                if goal_id and str(getattr(goal, "goal_id", "")) != goal_id:
                    continue
                required = getattr(goal, "required_capabilities", ()) or ()
                if required:
                    capability_name = str(required[0])
                    break
        return capability_name

    def _has_grounding_evidence(self, state: AgentState) -> bool:
        """True when THIS run already holds real community read evidence.

        A reasoning/synthesis step may only produce a result after a real
        SEARCH/GET_POST has returned post evidence in the current task/run.
        Model-invented source_refs are never evidence — they must resolve to a
        tool result that actually exists here.  Historical tasks are excluded:
        only tool_results/executions captured on this run count.
        """
        for result in state.tool_results:
            if not bool(result.get("ok", result.get("success", True))):
                continue
            tool = str(result.get("tool_name") or "").upper()
            if not _is_read_evidence_tool(tool):
                continue
            evidence = observation_evidence(result)
            if evidence["result_status"] != "SUCCESS":
                continue
            if (evidence["resource_count"] or 0) <= 0:
                continue
            return True
        # Reads that ran through CREATE_TASK land in execution evidence, not
        # tool_results.  Treat those COMPLETED read executions as evidence too.
        for evidence in _execution_read_evidence(state):
            if evidence.get("result_status") != "SUCCESS":
                continue
            if (evidence.get("resource_count") or 0) > 0:
                return True
        # The durable read projection may carry verified post evidence
        # (post_ids / post_id) even when the tool catalog is not re-injected
        # on resume.  A real post id is itself read evidence, so a reasoning /
        # analysis step after a SEARCH / GET_POST stays grounded on the
        # verified evidence alone; the gate must not depend on the catalog.
        snapshot = getattr(state, "context_snapshot", None)
        if isinstance(snapshot, Mapping):
            for execution in (snapshot.get("execution_states") or []):
                if not isinstance(execution, Mapping):
                    continue
                post_ids = execution.get("post_ids") or []
                single_post = str(execution.get("post_id") or "")
                if single_post or any(str(value) for value in post_ids):
                    return True
        return False

    def _produce_result_allowed(self, state: AgentState) -> bool:
        """Require registry + grounded evidence before accepting a reasoning result.

        A reasoning-backed Goal may only produce when it has real upstream
        community evidence in the current run; otherwise it is NOT_READY and the
        loop must first run the evidence-producing search.  This prevents
        ungrounded synthesis and the recorder failing on invented source_refs.
        """

        capability_name = self._current_reasoning_capability(state)
        registry = self._capability_registry or getattr(self._compiler, "_registry", None)
        capability = registry.get(capability_name) if registry and capability_name else None
        if capability is None or not _is_reasoning_capability(capability):
            return False
        if not self._has_grounding_evidence(state):
            return False
        return True

    def _next_reasoning_goal(self, state: AgentState) -> tuple[str, str]:
        """Return the next ready reasoning-backed Goal's (goal_id, capability)."""

        if state.goal_tree is None:
            return "", ""
        registry = self._capability_registry
        if registry is None:
            registry = getattr(self._compiler, "_registry", None)
        for goal in state.goal_tree.executable_goals():
            for capability in (getattr(goal, "required_capabilities", ()) or ()):
                cap = registry.get(str(capability)) if registry is not None else None
                if cap is not None and _is_reasoning_capability(cap):
                    return str(getattr(goal, "goal_id", "")), str(capability)
        return "", ""

    async def _create_task(
        self,
        state: AgentState,
        execution_runtime: Any,
        *,
        execution_submission: Any = None,
        task_manager: Any = None,
        llm: Any | None = None,
        model: str = "",
    ) -> dict[str, Any]:
        if state.goal_tree is None:
            raise AgentLoopError("GOAL_TREE_REQUIRED", "CREATE_TASK requires a GoalTree.")
        graph = self._compiler.compile(state.goal_tree, command=state.command)
        compiled_task_id = str(
            getattr(state.task, "task_id", "")
            or (state.goal.goal_id if state.goal else "")
        )
        plan = self._compiler.compile_plan(
            state.goal_tree,
            task_id=compiled_task_id,
            plan_version=max(1, state.plan_version),
            command=state.command,
        )
        await self._resolve_composite_plan_tools(
            plan,
            state,
            llm=llm,
            model=model,
        )
        manager = task_manager or self._task_manager
        if manager is not None and state.task is None:
            create = getattr(manager, "create_task", None)
            if callable(create):
                context = state.conversation_context
                created = create(
                    conversation_id=str(context.get("conversation_id", "agent-conversation")),
                    user_id=str(context.get("user_id", "agent-user")),
                    tenant_id=str(context.get("tenant_id", "agent-tenant")),
                    root_goal=state.goal,
                    goal_tree=state.goal_tree,
                    priority=int(context.get("priority", 0) or 0),
                )
                state.task = await created if inspect.isawaitable(created) else created
                state.plan_version = int(getattr(state.task, "plan_version", 0) or 0)
        submission = execution_submission or self._execution_submission
        runner = getattr(submission, "submit", None) if submission is not None else None
        if runner is None and execution_runtime is not None:
            runner = getattr(execution_runtime, "execute_goal_tree", None)
            if runner is None:
                runner = getattr(execution_runtime, "execute_plan", None)
            if runner is None and callable(execution_runtime):
                runner = execution_runtime
        if not callable(runner):
            raise AgentLoopError(
                "EXECUTION_SUBMISSION_INVALID",
                "CREATE_TASK requires an ExecutionSubmissionService or reliable runtime boundary.",
            )
        raw = runner(graph=graph, plan=plan, state=state)
        raw = await raw if inspect.isawaitable(raw) else raw
        result = _normalize_result(raw)
        result.setdefault("plan_id", plan.plan_id)
        result.setdefault("plan_source", plan.plan_source)
        state.timings.setdefault("execution_submitted_at", _now_timing())
        execution_id = str(result.get("execution_id") or "")
        if manager is not None and state.task is not None and execution_id:
            bind_execution = getattr(manager, "bind_execution", None)
            if callable(bind_execution):
                updated = bind_execution(
                    state.task.task_id,
                    execution_id,
                    status=str(result.get("status") or "SUBMITTED"),
                )
                state.task = await updated if inspect.isawaitable(updated) else updated
        state.submitted_task_ids.extend(
            task.task_id
            for task in state.goal_tree.task_nodes
            if task.task_id not in state.submitted_task_ids
        )
        return result

    async def _resolve_composite_plan_tools(
        self,
        plan: Any,
        state: AgentState,
        *,
        llm: Any | None,
        model: str,
    ) -> None:
        """Resolve only genuinely ambiguous plan steps through ToolSelector.

        Goal decomposition owns semantic capabilities, while a compiled plan
        may contain a capability with several concrete tools.  The worker
        must receive an explicit tool name; otherwise it is correct to fail
        closed rather than choose by registry order.  Reuse the same
        ToolMetadata selector used by immediate AgentLoop actions and attach
        the selected name to the plan before it crosses the durable queue.
        """

        catalog = list(state.available_tools)
        for step in getattr(plan, "steps", ()):
            if str(getattr(step, "tool_name", "") or ""):
                continue
            capability = str(getattr(step, "capability", "") or "")
            candidates = [
                item
                for item in catalog
                if capability in {
                    str(value) for value in (getattr(item, "capabilities", ()) or ())
                }
            ]
            if len(candidates) <= 1:
                continue
            if state.goal_tree is None:
                raise AgentLoopError(
                    "TOOL_SELECTION_CONTEXT_MISSING",
                    f"Cannot select a tool for capability '{capability}' without a GoalTree.",
                )
            goal = Goal(
                goal_id=str(getattr(step, "goal_id", "") or getattr(step, "step_id", "")),
                description=str(getattr(step, "description", "") or capability),
                required_capabilities=[capability],
            )
            observation = Observation(
                goal=goal.model_dump(mode="json"),
                current_task=step.model_dump(mode="json"),
                conversation_context=dict(state.conversation_context),
                tool_results=list(state.tool_results),
                execution_results=list(state.execution_results),
                context_snapshot_id=state.context_snapshot_id,
                memory_ids_used=list(state.memory_ids_used),
            )
            selected = await self._selector.select(
                goal,
                observation,
                catalog,
                requested_arguments=dict(getattr(step, "constraints", {}) or {}),
                llm=llm,
                model=model,
            )
            candidate_names = {str(item.name) for item in candidates}
            if selected.tool_name not in candidate_names:
                raise AgentLoopError(
                    "TOOL_SELECTION_CAPABILITY_MISMATCH",
                    f"ToolSelector returned '{selected.tool_name}' outside capability '{capability}'.",
                )
            step.tool_name = selected.tool_name
            # Explicit plan constraints are authoritative.  Selector-filled
            # arguments only supply fields that decomposition left open.
            step.constraints = {
                **dict(selected.arguments or {}),
                **dict(getattr(step, "constraints", {}) or {}),
            }

    async def replan(
        self,
        observation: Observation,
        state: AgentState,
        *,
        task: Any | None = None,
        llm: Any | None = None,
        model: str = "",
    ) -> PlanningDecision:
        """Ask DynamicPlanner for a typed mutation using current evidence."""

        if state.goal_tree is None:
            raise AgentLoopError("GOAL_TREE_REQUIRED", "Replan requires a GoalTree.")
        return await self._planner.replan(
            goal_tree=state.goal_tree,
            agent_state=state,
            task=task,
            tool_catalog=state.available_tools,
            execution_history=state.execution_results,
            observations=[item.model_dump(mode="json") for item in state.observations],
            context_snapshot=state.context_snapshot,
            llm=llm,
            model=model,
        )

    def _update_plan(self, action: AgentAction, state: AgentState) -> dict[str, Any]:
        candidate = action.goal_tree
        if candidate is None and action.plan_patch:
            raw_tree = action.plan_patch.get("goal_tree", action.plan_patch)
            try:
                candidate = GoalTree.model_validate(raw_tree)
            except ValidationError as exc:
                raise AgentLoopError("GOAL_TREE_PATCH_INVALID", "UPDATE_PLAN supplied invalid GoalTree.") from exc
        if candidate is None:
            raise AgentLoopError("GOAL_TREE_PATCH_MISSING", "UPDATE_PLAN requires a GoalTree patch.")
        candidate.validate_tree()
        self._compiler.compile(candidate, command=state.command)
        state.goal_tree = candidate
        state.goal = candidate.root_goal
        state.current_task = _next_task(state)
        return {
            "ok": True,
            "action": AgentActionType.UPDATE_PLAN.value,
            "goal_tree": candidate.model_dump(mode="json"),
        }

    def _result(
        self,
        state: AgentState,
        *,
        content: str = "",
        question: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> AgentRunResult:
        success = state.status == AgentStatus.COMPLETED
        return AgentRunResult(
            success=success,
            status=state.status,
            content=content,
            question=question,
            error_code=error_code or ("" if success else state.last_error),
            error_message=error_message or state.last_error,
            iterations=state.iteration,
            actions=[item for item in state.history if item.get("type") == "ACTION"],
            observations=[item.model_dump(mode="json") for item in state.observations],
            tool_results=list(state.tool_results),
            execution_results=list(state.execution_results),
            root_error_code=state.root_error_code,
            root_error_message=state.root_error_message,
            root_error_goal_id=state.root_error_goal_id,
            root_error_iteration=state.root_error_iteration,
            state=state,
        )


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise AgentLoopError("AGENT_RESPONSE_EMPTY", "LLM returned no structured Agent response.")
    # Safe normalization only: strip a markdown fence and extract the first
    # top-level JSON object.  We never infer a business action from prose.
    candidate = extract_top_level_json(content)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        logger.warning(
            "agent_structured_response invalid_json content_chars=%s error=%s",
            len(content),
            exc,
        )
        raise AgentLoopError("AGENT_RESPONSE_INVALID_JSON", "LLM returned invalid Agent JSON.") from exc


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _extract_content(response: Any) -> str:
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and str(item.get("text") or "").strip()
        ]
        return "\n".join(parts)
    if isinstance(content, Mapping):
        return json.dumps(content, ensure_ascii=False, default=str)
    return ""


async def _repair_structured_output(
    client: Any,
    model: str,
    *,
    schema_name: str,
    schema: dict[str, Any],
    original_content: str,
    error_summary: str,
) -> Any:
    """Bounded repair: convert an already-expressed action into valid schema.

    The repair is deliberately NOT a replanner.  It receives only the original
    raw output, the validation error, and the schema; it must not re-read the
    conversation, re-call tools, or invent a new plan.
    """

    schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "A previous response was not valid structured output for the required schema.\n\n"
        f"Original response:\n{_truncate(original_content, 4000)}\n\n"
        f"Validation error:\n{_truncate(error_summary, 1200)}\n\n"
        "Return only valid JSON matching this schema. Do not explain. Do not change "
        "the intended action unless the original output is impossible to represent. "
        "Use only allowed enum values.\n\n"
        f"Schema:\n{schema_json}"
    )
    kwargs = {
        "model": model,
        "messages": [{"role": "system", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": STRUCTURED_OUTPUT_RETRY_MAX_TOKENS,
        **structured_provider_options(client, model),
    }
    return await client.chat.completions.create(**kwargs)


def _normalize_tools(value: Sequence[ToolMetadata] | ToolRegistry | Any) -> list[ToolMetadata]:
    if isinstance(value, ToolRegistry):
        return value.list()
    list_metadata = getattr(value, "list_tool_metadata", None)
    if callable(list_metadata):
        return list(list_metadata())
    list_method = getattr(value, "list", None)
    values = list_method() if callable(list_method) and not isinstance(value, (list, tuple)) else (value or [])
    return [item if isinstance(item, ToolMetadata) else ToolMetadata.model_validate(item) for item in values]


def _metadata_payload(metadata: ToolMetadata) -> dict[str, Any]:
    return metadata.model_dump(mode="json")


def _equivalent_read_scope(
    prior: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> bool:
    """Return whether a new read is the same scope as a prior successful read.

    Exact-equivalent queries are duplicates; a changed query/target, an empty
    prior result, or pagination drift is a materially different scope and is
    allowed to re-read.
    """

    if str(prior.get("result_status") or "").upper() == "EMPTY":
        return False
    prior_args = dict(prior.get("arguments") or {})
    new_args = dict(arguments or {})
    keys = set(prior_args) | set(new_args)
    for key in keys:
        if key in {"page", "page_size", "limit", "offset"}:
            continue
        pv = prior_args.get(key)
        nv = new_args.get(key)
        if pv is None and nv is None:
            continue
        if pv is None or nv is None:
            return False
        if str(pv).strip().lower() == str(nv).strip().lower():
            continue
        return False
    return True


def _read_signature(tool_name: str, arguments: Mapping[str, Any]) -> str:
    return f"{tool_name}|{_arguments_signature(arguments)}"


def _scope_arguments_for_execution_evidence(state: AgentState, goal_id: str) -> dict[str, Any]:
    """Derive the semantic scope for a durable read from the Goal's target."""

    if state.goal_tree is None:
        return {}
    goal = next(
        (
            item
            for item in state.goal_tree.all_goals()
            if str(getattr(item, "goal_id", "")) == goal_id
        ),
        None,
    )
    target = getattr(goal, "target", None) if goal is not None else None
    if isinstance(target, Mapping):
        keyword = str(target.get("keyword") or target.get("topic") or "")
        if keyword:
            return {"query": keyword}
    return {}


def _execution_states(state: AgentState) -> list[dict[str, Any]]:
    snapshot = getattr(state, "context_snapshot", None)
    states: list[dict[str, Any]] = []
    if isinstance(snapshot, Mapping):
        states.extend(snapshot.get("execution_states") or [])
    # Reasoning results produced in-loop (PRODUCE_RESULT) are durable
    # execution evidence for Goal satisfaction even before the next context
    # refresh projects them; without this the finished reasoning Goal would
    # look unsatisfied and a legitimate FINISH would be rejected as premature.
    for result in getattr(state, "execution_results", ()) or ():
        if not isinstance(result, Mapping):
            continue
        if not (result.get("goal_id") and result.get("capability")):
            continue
        states.append({
            "goal_id": str(result.get("goal_id") or ""),
            "capability": str(result.get("capability") or ""),
            "status": str(result.get("status") or ""),
            "artifact_type": str(result.get("artifact_type") or ""),
        })
    return states


def _is_successful_direct_read(state: AgentState, result: Mapping[str, Any]) -> bool:
    """Identify an in-loop successful read without treating queue acceptance as completion."""

    if not bool(result.get("ok", result.get("success", False))):
        return False
    if str(result.get("status") or "").upper() in {"QUEUED", "SUBMITTED"}:
        return False
    tool_name = str(result.get("tool_name") or "")
    if not tool_name:
        return False
    metadata = next(
        (item for item in state.available_tools if str(getattr(item, "name", "")) == tool_name),
        None,
    )
    return bool(metadata is not None and _is_read_tool_metadata(metadata))


def _remember_root_failure(state: AgentState, result: Mapping[str, Any]) -> None:
    """Preserve the first structured failure; terminal guards must not replace it."""

    if bool(result.get("ok", result.get("success", True))):
        return
    if state.root_error_code:
        return
    code = str(result.get("error_code") or result.get("code") or "AGENT_ACTION_FAILED")
    state.root_error_code = code
    state.root_error_message = str(
        result.get("error_message") or result.get("message") or code
    )
    state.root_error_goal_id = str(
        result.get("goal_id")
        or getattr(getattr(state, "current_task", None), "goal_id", "")
        or ""
    )
    state.root_error_iteration = state.iteration


def _no_progress_detected(state: AgentState, result: Mapping[str, Any]) -> bool:
    """Stop repeated replans when durable/business state has not changed."""

    snapshot = state.context_snapshot if isinstance(state.context_snapshot, Mapping) else {}
    relevant = {
        "goal_id": str(getattr(getattr(state, "current_task", None), "goal_id", "") or ""),
        "goal_statuses": [
            {
                "goal_id": str(item.get("goal_id") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in (snapshot.get("goal_states") or snapshot.get("unfinished_goals") or [])
            if isinstance(item, Mapping)
        ],
        "artifacts": sorted(
            (
                str(item.get("artifact_id") or ""),
                str(item.get("artifact_type") or item.get("type") or ""),
            )
            for item in (snapshot.get("artifacts") or [])
            if isinstance(item, Mapping)
        ),
        "executions": sorted(
            (
                str(item.get("execution_id") or ""),
                str(item.get("status") or ""),
                str(item.get("capability") or ""),
            )
            for item in (snapshot.get("execution_states") or [])
            if isinstance(item, Mapping)
        ),
        "desired_version": int(getattr(getattr(state, "goal_tree", None), "version", 0) or 0),
        "failure": str(result.get("error_code") or result.get("code") or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if fingerprint == state.no_progress_fingerprint:
        state.no_progress_count += 1
    else:
        state.no_progress_fingerprint = fingerprint
        state.no_progress_count = 1
    return state.no_progress_count >= MAX_NO_PROGRESS_REPEATS


def _completed_capabilities_by_goal(state: AgentState) -> dict[str, set[str]]:
    completed: dict[str, set[str]] = {}
    for es in _execution_states(state):
        goal_id = str(es.get("goal_id") or "")
        status = str(es.get("status") or "").upper()
        capability = str(es.get("capability") or "")
        if goal_id and status == "COMPLETED" and capability:
            completed.setdefault(goal_id, set()).add(capability.upper())
    return completed


def _goal_facts(state: AgentState, goal_id: str) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "draft_id": "",
        "schedule_id": "",
        "post_id": "",
        "run_at": "",
        "status": "",
        "completed_capabilities": [],
        "artifact_types": [],
    }
    for es in _execution_states(state):
        if str(es.get("goal_id") or "") != goal_id:
            continue
        for key in ("draft_id", "schedule_id", "post_id", "run_at"):
            value = es.get(key)
            if value not in (None, ""):
                facts[str(key)] = str(value)
        status = str(es.get("status") or "")
        if status:
            facts["status"] = status
        capability = str(es.get("capability") or "")
        if status.upper() == "COMPLETED" and capability:
            completed = facts["completed_capabilities"]
            if capability not in completed:
                completed.append(capability)
        artifact_type = str(es.get("artifact_type") or "")
        if artifact_type:
            artifact_types = facts["artifact_types"]
            if artifact_type not in artifact_types:
                artifact_types.append(artifact_type)
    return facts


_goal_satisfied = goal_is_satisfied
_publication_intent_of = publication_intent_of

# Raw reasoning/control errors that must never leak to the main UI.
_RAW_ERROR_TO_USER_SAFE = {
    "AGENT_RESPONSE_INVALID_JSON": USER_SAFE_REASONING_FAILURE,
    "AGENT_RESPONSE_EMPTY": USER_SAFE_REASONING_FAILURE,
    "AGENT_ACTION_SCHEMA_INVALID": USER_SAFE_REASONING_FAILURE,
    "AGENT_REFLECTION_SCHEMA_INVALID": USER_SAFE_REASONING_FAILURE,
    "STRUCTURED_OUTPUT_INVALID": USER_SAFE_REASONING_FAILURE,
    "REASONING_RESULT_INVALID": USER_SAFE_REASONING_FAILURE,
    "REASONING_RESULT_COMMIT_FAILED": USER_SAFE_REASONING_FAILURE,
    "REASONING_RESULT_NOT_PERSISTED": USER_SAFE_REASONING_FAILURE,
    "NO_PROGRESS_DETECTED": USER_SAFE_REASONING_FAILURE,
    "GOAL_NOT_SATISFIED": USER_SAFE_REASONING_FAILURE,
    # ToolSelector / Goal / Command control errors: technical LLM or catalog
    # details belong in the developer log, never in the user-facing message.
    "TOOL_SELECTION_EMPTY": USER_SAFE_REASONING_FAILURE,
    "TOOL_SELECTION_INVALID_JSON": USER_SAFE_REASONING_FAILURE,
    "TOOL_SELECTION_SCHEMA_INVALID": USER_SAFE_REASONING_FAILURE,
    "TOOL_NOT_IN_CATALOG": USER_SAFE_REASONING_FAILURE,
    "TOOL_SELECTOR_LLM_UNAVAILABLE": USER_SAFE_REASONING_FAILURE,
    "TOOL_CATALOG_EMPTY": USER_SAFE_REASONING_FAILURE,
    "TOOL_METADATA_REQUIRED": USER_SAFE_REASONING_FAILURE,
    "TOOL_POLICY_DENIED": USER_SAFE_REASONING_FAILURE,
    "TOOL_QUEUE_UNAVAILABLE": USER_SAFE_REASONING_FAILURE,
    "TOOL_RUNTIME_INVALID": USER_SAFE_REASONING_FAILURE,
    "TOOL_SELECTION_CONTEXT_MISSING": USER_SAFE_REASONING_FAILURE,
    "GOAL_TREE_REQUIRED": USER_SAFE_REASONING_FAILURE,
    "GOAL_COMPILATION_INVALID": USER_SAFE_REASONING_FAILURE,
    "EXECUTION_SUBMISSION_INVALID": USER_SAFE_REASONING_FAILURE,
    "AGENT_ACTION_UNSUPPORTED": USER_SAFE_REASONING_FAILURE,
    "AGENT_COMMAND_INVALID": USER_SAFE_REASONING_FAILURE,
    "AGENT_GOAL_TREE_INVALID": USER_SAFE_REASONING_FAILURE,
    "WRONG_EXECUTION_SEMANTICS": USER_SAFE_REASONING_FAILURE,
    "AGENT_MAX_ITERATIONS": "这一步需要较多后续步骤，尚未全部完成。你可以让我继续处理。",
}


def _user_safe_error(code: str, message: str) -> str:
    safe = _RAW_ERROR_TO_USER_SAFE.get(str(code) or "")
    if safe:
        return safe
    # AGENT_LOOP_FAILED / provider / schema errors: keep the technical message
    # in the developer log, never project raw JSON/Pydantic text to the user.
    lowered = str(message or "").lower()
    pydantic_signals = (
        "invalid agent json",
        "jsondecodeerror",
        "validation errors for",
        "field required",
        "extra inputs are not permitted",
        "pydantic",
        "is not a planningdecision",
        "is not an agentaction",
        "is not a reflection",
        "is not a tool selection",
        "not a planningdecision",
        "goaltree could not be compiled",
        "goalcompilationerror",
        "could not be compiled into",
        "requires one grounded goal",
        "cannot remove the root goal",
    )
    if any(signal in lowered for signal in pydantic_signals):
        return USER_SAFE_REASONING_FAILURE
    if "does not match" in lowered and "schema" in lowered:
        return USER_SAFE_REASONING_FAILURE
    return str(message or "Agent execution failed.")


def _normalize_agent_action_payload(value: Any) -> Any:
    """Treat nullable JSON-mode defaults as their typed empty values."""

    if not isinstance(value, Mapping):
        return value
    payload = dict(value)
    # Strict-schema tolerance: a reasoning model occasionally echoes GoalTree /
    # Command fields (goals, task_nodes, command_id, source) into the AgentAction
    # envelope.  Those are never part of the action contract; strip them so the
    # decision survives instead of failing extra_forbidden (the action's own
    # fields are still validated by Pydantic).
    allowed = {
        "action", "tool_name", "tool_args", "goal_tree", "plan_patch",
        "question", "reason", "confidence", "result_payload", "result_type",
        "source_refs",
    }
    payload = {key: value for key, value in payload.items() if key in allowed}
    for field in ("tool_args", "plan_patch", "result_payload"):
        if payload.get(field) is None:
            payload[field] = {}
    for field in ("tool_name", "question", "reason", "result_type"):
        if payload.get(field) is None:
            payload[field] = ""
    if payload.get("source_refs") is None:
        payload["source_refs"] = []
    if payload.get("confidence") is None:
        payload["confidence"] = 0.0
    if isinstance(payload.get("goal_tree"), Mapping):
        payload["goal_tree"] = _normalize_goal_tree_payload(payload["goal_tree"])
    if isinstance(payload.get("plan_patch"), Mapping):
        patch = dict(payload["plan_patch"])
        if isinstance(patch.get("goal_tree"), Mapping):
            patch["goal_tree"] = _normalize_goal_tree_payload(patch["goal_tree"])
        payload["plan_patch"] = patch
    return payload


def _normalize_goal_tree_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve compact child goal-id references emitted by JSON-mode LLMs.

    The canonical GoalTree contract stores nested Goal objects.  Some models
    return the equivalent compact form ``children: ["goal-id"]``.  Expand
    those references from the same payload (or a minimal typed placeholder)
    before strict Pydantic validation; no task or tool is inferred here.
    """

    import copy

    payload = copy.deepcopy(dict(value))
    index: dict[str, dict[str, Any]] = {}
    for candidate in list(payload.get("goals") or ()):
        if isinstance(candidate, Mapping) and candidate.get("goal_id"):
            index[str(candidate["goal_id"])] = dict(candidate)
    for key in ("root", "root_goal"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping) and candidate.get("goal_id"):
            index.setdefault(str(candidate["goal_id"]), dict(candidate))

    def expand(goal: Any, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if isinstance(goal, str):
            goal_id = str(goal)
            source = index.get(goal_id, {"goal_id": goal_id})
            if goal_id in stack:
                source = {"goal_id": goal_id}
            return expand(source, (*stack, goal_id))
        if not isinstance(goal, Mapping):
            return {"goal_id": str(goal)}
        result = dict(goal)
        goal_id = str(result.get("goal_id") or "")
        next_stack = (*stack, goal_id) if goal_id else stack
        result["children"] = [expand(child, next_stack) for child in (result.get("children") or ())]
        return result

    for key in ("root", "root_goal"):
        if isinstance(payload.get(key), (Mapping, str)):
            payload[key] = expand(payload[key])
    if isinstance(payload.get("goals"), list):
        payload["goals"] = [expand(goal) for goal in payload["goals"]]
    return payload


def _normalize_reflection_payload(value: Any) -> Any:
    """Treat nullable JSON-mode defaults as their typed empty values."""

    if not isinstance(value, Mapping):
        return value
    payload = dict(value)
    # Some JSON-mode providers echo this root JSON-Schema keyword alongside
    # the requested object. It is schema metadata, not a Reflection field;
    # keep Pydantic strict for every other unknown field.
    if payload.get("additionalProperties") is False:
        payload.pop("additionalProperties")
    for field in ("finished", "needs_next_step", "retry", "adjust_plan"):
        if payload.get(field) is None:
            payload[field] = field == "needs_next_step"
    if payload.get("reason") is None:
        payload["reason"] = ""
    return payload


def _context_payload(value: Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    decision_payload = getattr(value, "decision_payload", None)
    if callable(decision_payload):
        value = decision_payload()
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": str(value)}


def _set_context_state(state: AgentState, source: Any, payload: Mapping[str, Any]) -> None:
    """Keep only the latest bounded snapshot and its audit identifiers."""

    state.context_snapshot = dict(payload)
    state.conversation_context = dict(payload)
    state.memory_snapshot = {
        "recalled_memories": list(payload.get("recalled_memories", [])),
        "user_preferences": list(payload.get("user_preferences", [])),
        "memory_ids_used": list(payload.get("memory_ids_used", [])),
    }
    state.context_snapshot_id = str(payload.get("snapshot_id", ""))
    state.memory_ids_used = [
        str(value) for value in payload.get("memory_ids_used", []) if value
    ]
    if not state.context_snapshot_id:
        state.context_snapshot_id = str(getattr(source, "snapshot_id", ""))


def _normalize_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    elif hasattr(value, "model_dump"):
        result = value.model_dump(mode="json")
    elif is_dataclass(value):
        result = asdict(value)
    else:
        result = {"value": value}
    if "success" in result and "ok" not in result:
        result["ok"] = bool(result["success"])
    if "code" in result and "error_code" not in result:
        result["error_code"] = str(result.get("code") or "")
    if "message" in result and "error_message" not in result:
        result["error_message"] = str(result.get("message") or "")
    return result


def _next_task(state: AgentState):
    if state.goal_tree is None:
        return None
    goals = {
        str(goal.goal_id): goal
        for goal in state.goal_tree.all_goals()
    }
    facts_by_goal = _facts_by_goal_from_state(state)
    completed = set(state.completed_task_ids)
    completed_goals = set(state.completed_goal_ids)
    submitted = set(state.submitted_task_ids)
    for task in state.goal_tree.task_nodes:
        if task.task_id in completed:
            continue
        goal = goals.get(str(task.goal_id))
        goal_id = str(task.goal_id) or ""
        if goal_id in completed_goals:
            # The owning Goal is already satisfied by durable facts (or a
            # resumed completion); every TaskNode of that Goal is done.
            completed.add(task.task_id)
            continue
        facts = facts_by_goal.get(goal_id, {})
        if goal is not None and goal_is_satisfied(goal, facts):
            completed.add(task.task_id)
            completed_goals.add(goal_id)
            continue
        # Node-level completion: this TaskNode's capability already has a
        # COMPLETED execution for this Goal (e.g. an in-loop SEARCH finished
        # in the previous iteration).  Without this check the scan keeps
        # returning the first node forever and the activity label never
        # advances past it.
        capability = str(getattr(task, "capability", "") or "").upper()
        if capability and capability in {
            str(value).upper() for value in (facts.get("completed_capabilities") or ())
        }:
            completed.add(task.task_id)
            continue
        if goal is not None and not dependencies_satisfied(
            goal,
            goals,
            facts_by_goal,
        ):
            continue
        if task.task_id in submitted:
            # Already durably submitted; wait for the Observation before
            # re-selecting.  Submission is a hand-off, never a completion.
            continue
        # Persist newly-observed completions before returning the next task,
        # so _activity_payload and later scans see current state.
        state.completed_task_ids = sorted(completed)
        state.completed_goal_ids = sorted(completed_goals)
        state.submitted_task_ids = sorted(submitted)
        return task
    state.completed_task_ids = sorted(completed)
    state.completed_goal_ids = sorted(completed_goals)
    state.submitted_task_ids = sorted(submitted)
    return None


def _facts_by_goal_from_state(state: AgentState) -> dict[str, dict[str, Any]]:
    """Build the minimal Goal facts needed for same-loop task convergence."""

    facts: dict[str, dict[str, Any]] = {}
    for execution in _execution_states(state):
        goal_id = str(execution.get("goal_id") or "")
        entry = facts.setdefault(
            goal_id,
            {"status": "", "completed_capabilities": [], "post_ids": []},
        )
        status = str(execution.get("status") or "")
        if status:
            entry["status"] = status
        capability = str(execution.get("capability") or "")
        if (
            status.upper() == "COMPLETED"
            and capability
            and capability not in entry["completed_capabilities"]
        ):
            entry["completed_capabilities"].append(capability)
        artifact_type = str(
            execution.get("artifact_type")
            or execution.get("output_artifact_type")
            or ""
        )
        if artifact_type:
            types = entry.setdefault("artifact_types", [])
            if artifact_type not in types:
                types.append(artifact_type)
        for step in execution.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            step_goal = str(step.get("goal_id") or "")
            if not step_goal:
                continue
            step_entry = facts.setdefault(
                step_goal,
                {"status": "", "completed_capabilities": [], "post_ids": []},
            )
            step_status = str(step.get("status") or "").upper()
            step_capability = str(step.get("capability") or "")
            if step_status == "COMPLETED":
                step_entry["status"] = "COMPLETED"
                if step_capability and step_capability not in step_entry["completed_capabilities"]:
                    step_entry["completed_capabilities"].append(step_capability)
                output = step.get("output_artifact") or {}
                output_type = str(output.get("artifact_type") or "") if isinstance(output, Mapping) else ""
                if output_type:
                    types = step_entry.setdefault("artifact_types", [])
                    if output_type not in types:
                        types.append(output_type)
                if isinstance(output, Mapping):
                    resource_id = str(output.get("resource_id") or "")
                    normalized_type = output_type.upper()
                    if resource_id and normalized_type in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
                        step_entry["draft_id"] = resource_id
                    elif resource_id and normalized_type in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
                        step_entry["schedule_id"] = resource_id
                    elif resource_id and normalized_type in {"POST", "PUBLISHED_POST"}:
                        step_entry["post_id"] = resource_id
                    # SEARCH_RESULT outputs reference every returned post in
                    # resource_refs; carry those ids so a follow-up
                    # GET_POST_DETAIL step can pick a real post_id instead of
                    # degrading into repeated searches (observed live: the
                    # first of three tasks looped 20+ iterations on search).
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
    return facts


def _resume_context(value: ResumeContext | Mapping[str, Any] | None) -> ResumeContext | None:
    if value is None:
        return None
    if isinstance(value, ResumeContext):
        return value
    return ResumeContext.model_validate(value)


_REASON_PROMPT = """You are the GreenBook Goal-driven Community Agent.

Given the canonical Command, GoalTree, current Observation, memory snapshot,
and ToolMetadata, choose exactly one next AgentAction. Use TOOL_CALL when a
concrete read-only observation is needed; leave tool_name empty when the
ToolSelector should choose from metadata. For a GoalTree with dependent or
parallel child goals, or for any side-effecting multi-step outcome, prefer
CREATE_TASK so GoalCompiler and the existing Execution Runtime receive the
complete plan. Do not submit one side-effecting child with TOOL_CALL when the
GoalTree still contains later dependent work. Use CREATE_TASK to hand a
structured GoalTree to GoalCompiler and the existing Execution Runtime. Use
UPDATE_PLAN only with a complete GoalTree patch. Use ASK_USER when
clarification or approval is required. Use FINISH only when the user goal is
complete.

A Goal whose required capability is a reasoning/analysis step (for example
ANALYZE_CONTENT_PATTERNS, VALIDATE_QUALITY, or any capability with no tool)
is completed by reasoning, not by a tool call: return PRODUCE_RESULT with a
structured result_payload that consumes the dependency's already-available
evidence. Populate result_type (for example CONTENT_ANALYSIS), source_refs
(the source artifact/observation identifiers this result is derived from),
and a result_payload containing summary and/or key_points. Do not select a
tool for a reasoning-backed Goal, and do not invent a summary when the source
evidence does not exist.

Treat the latest concrete Observation as consumed evidence. If a read action
already ran with the same scope and returned SUCCESS or EMPTY, do not emit the
same read action again unless the scope or arguments materially change. In
particular, a successful search whose result count is 0 (or whose items are
empty) is already consumed evidence: choose ASK_USER for a broader
user-provided scope or FINISH with an evidence-bounded explanation. Never
retry that exact empty read merely to obtain a different result.
When a search returns results but none matches the requested topic exactly,
still consume that evidence: extract the related themes, techniques, and
concerns from the returned posts and continue toward the Goal. Reusing real
returned material with a reasonable interpretation (for example summarizing
the technical topics most discussed in roadmap/guide posts) is not fabrication
and must not trigger ASK_USER. Only when the Goal has no usable evidence at
all — no search results and no other sources — choose ASK_USER with a concrete
question.
The runtime_evidence_constraints block is a hard evidence boundary: if a
read-only tool already returned SUCCESS for this Goal, consume that evidence
and choose the next semantic Goal action; do not select the same read tool
again merely because another result might be interesting. If it returned
EMPTY, only a materially changed, evidence-bounded scope is allowed.

Do not emit MCP handlers, positional tool choices, business Agent names, or
execution lifecycle operations. Do not reinterpret the raw user message.
Return exactly one AgentAction object: only the schema fields (action,
tool_name, tool_args, goal_tree, plan_patch, question, reason, confidence,
result_payload, result_type, source_refs). Do not copy GoalTree or Command
envelope fields (goals, task_nodes, command_id, source, version) into the
AgentAction object.
"""

_REFLECT_PROMPT = """You are the GreenBook Agent Reflector.

Inspect the Goal, Observation, last AgentAction, and its result. Return one
Reflection object. Decide whether the goal is complete, whether another step
is needed, whether a failed action is safe to retry, and whether the plan must
be adjusted. Do not perform a tool call and do not emit execution commands.
An EMPTY result is a valid Observation, not proof that the goal is complete.
For a safe read failure or EMPTY result, allow Dynamic Planner to inspect the
available read-only metadata and choose an evidence-bounded alternative,
broader query, lower-scope analysis, or an evidence-based question. Never
invent identifiers or claim evidence that was not returned. Treat the
result.provenance field as authoritative: distinguish PERSONAL_DATA,
COMMUNITY_DATA, and MODEL_INFERENCE, and never describe
community evidence as the user's own historical performance. If personal
evidence is empty, say that the personal sample is insufficient before using
community evidence.
Do not retry the identical read action after an EMPTY, zero-item, or successful
result without a materially different scope or argument; repeated identical
reads do not add evidence.
When SEARCH_COMMUNITY already succeeded in this Goal, a second search needs an
explicit structured scope change; do not broaden or repeat it implicitly.
"""


def _now_timing() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _activity_payload(state: AgentState, action: AgentAction) -> dict[str, Any]:
    """Business activity payload for a decided semantic action.

    The semantic action must be a capability (SEARCH_COMMUNITY), never a
    concrete tool name; the tool is resolved from the capability by the
    ToolSelector, so the user-facing activity stays product-level.
    """

    goal_id = str(getattr(getattr(state, "goal", None), "goal_id", "") or "")
    current_task = getattr(state, "current_task", None)
    task_id = str(getattr(current_task, "task_id", "") or "")
    capability = str(getattr(current_task, "capability", "") or "")
    tool_name = str(getattr(action, "tool_name", "") or "")
    business_action = ""
    goal_tree = getattr(state, "goal_tree", None)
    if not capability and goal_tree is not None:
        completed = {
            str(value) for value in getattr(state, "completed_task_ids", ()) or ()
        }
        completed_goals = {
            str(value) for value in getattr(state, "completed_goal_ids", ()) or ()
        }
        for node in goal_tree.task_nodes:
            if str(getattr(node, "task_id", "")) in completed:
                continue
            if str(getattr(node, "goal_id", "") or "") in completed_goals:
                continue
            capability = str(getattr(node, "capability", "") or "")
            if task_id:
                task_id = str(getattr(node, "task_id", "") or "")
            break
    if not capability and goal_tree is not None:
        # Fall back to the first unsatisfied executable Goal's capability so
        # the activity stays semantic even without explicit TaskNodes.
        for goal in goal_tree.executable_goals():
            for value in (getattr(goal, "required_capabilities", ()) or ()):
                if str(value):
                    capability = str(value)
                    break
            if capability:
                break
    if not capability and action.action == AgentActionType.TOOL_CALL:
        # Map the concrete tool back to its declared capability via metadata.
        for metadata in getattr(state, "available_tools", ()) or ():
            if str(getattr(metadata, "name", "")) == tool_name:
                capabilities = tuple(getattr(metadata, "capabilities", ()) or ())
                if capabilities:
                    capability = str(capabilities[0])
                semantic_action = getattr(metadata, "semantic_action", None)
                business_action = str(
                    getattr(semantic_action, "value", semantic_action) or ""
                )
                break
    elif action.action == AgentActionType.TOOL_CALL:
        # The model may already be acting on a later GoalTree step while
        # ``current_task`` still points at the completed one (a search finished
        # in the previous iteration, this iteration creates the draft).  The
        # concrete tool's declared capability is the authoritative activity.
        for metadata in getattr(state, "available_tools", ()) or ():
            if str(getattr(metadata, "name", "")) == tool_name:
                capabilities = tuple(getattr(metadata, "capabilities", ()) or ())
                if capabilities:
                    capability = str(capabilities[0])
                semantic_action = getattr(metadata, "semantic_action", None)
                business_action = str(
                    getattr(semantic_action, "value", semantic_action) or ""
                )
                break
    return {
        "semantic_action": capability or str(getattr(action, "tool_name", "") or ""),
        # ``semantic_action`` above remains the legacy capability projection.
        # ``business_action`` is the explicit ToolContract SemanticAction used
        # by the durable UserActivity projector.
        "business_action": business_action or None,
        "capability": capability or None,
        "tool_name": tool_name or None,
        "action_type": str(getattr(action, "action", "") or ""),
        "goal_id": goal_id,
        "task_id": task_id,
        "activity_key": ":".join(
            value
            for value in (
                "agent-action",
                goal_id or "goal",
                task_id or "task",
                str(getattr(state, "iteration", 0)),
                str(getattr(action, "action", "") or ""),
                tool_name or capability or "action",
            )
        ),
    }


def _observation_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    return observation_evidence(result)


def _read_evidence_constraints(state: AgentState) -> dict[str, Any]:
    """Expose consumed read evidence as a model-facing runtime constraint.

    This is deliberately metadata-driven.  It does not map a capability to a
    tool or choose the next action; it only tells Reason/ToolSelector which
    read observations already exist so they do not redispatch an equivalent
    read after the evidence has been consumed.
    """

    metadata_by_name = {
        str(getattr(item, "name", "")): item
        for item in state.available_tools
        if str(getattr(item, "name", ""))
    }
    consumed: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result in state.tool_results:
        tool_name = str(result.get("tool_name") or "")
        metadata = metadata_by_name.get(tool_name)
        policy = getattr(metadata, "policy", None)
        side_effect = getattr(policy, "side_effect", None)
        if not tool_name or policy is None or side_effect is None:
            continue
        if not _is_read_tool_metadata(metadata):
            continue
        evidence = observation_evidence(result)
        if evidence["result_status"] not in {"SUCCESS", "EMPTY"}:
            continue
        consumed.append(
            {
                "tool_name": tool_name,
                "capabilities": list(getattr(metadata, "capabilities", ()) or ()),
                "arguments": dict(result.get("tool_arguments") or {}),
                "result_status": evidence["result_status"],
                "resource_count": evidence["resource_count"],
            }
        )
        seen.add((tool_name, _arguments_signature(result.get("tool_arguments") or {})))
    # A read that ran through CREATE_TASK -> incremental durable execution lands
    # in execution evidence, not tool_results.  Project those COMPLETED read
    # executions as consumed evidence so the model sees the hard boundary
    # instead of re-dispatching an equivalent read.
    for evidence in _execution_read_evidence(state):
        key = (evidence["tool_name"], _arguments_signature(evidence.get("arguments") or {}))
        if key in seen:
            continue
        seen.add(key)
        consumed.append(evidence)
    return {
        "consumed_read_evidence": consumed,
        "same_scope_read_redispatch": "FORBIDDEN",
        "empty_result_requires_material_scope_change": True,
    }


def _is_reasoning_capability(capability: Any) -> bool:
    """A Goal whose capability has no tool is a reasoning-backed step.

    The model produces the result (summary/analysis) from existing evidence in
    the same AgentLoop; it must never be routed through ToolSelector or a
    Worker placeholder execution.
    """

    if getattr(capability, "is_llm_step", False):
        return True
    return not bool(getattr(capability, "tools", ()))


def _is_read_evidence_tool(tool: str) -> bool:
    """Classify a tool result as community read evidence for grounding.

    Only real post-producing reads count as synthesis evidence.  Writes and
    metadata-only reads are excluded so an empty or side-effecting result can
    never satisfy the grounding requirement.
    """
    t = (tool or "").upper()
    return (
        "SEARCH_PUBLIC_POSTS" in t
        or "GET_POST" in t
        or "LIST_OWN_POSTS" in t
    )


def _is_read_tool_metadata(metadata: Any) -> bool:
    policy = getattr(metadata, "policy", None)
    side_effect = getattr(policy, "side_effect", None)
    if policy is None or side_effect is None:
        return False
    return not (
        bool(getattr(policy, "requires_approval", False))
        or bool(getattr(side_effect, "has_side_effect", False))
        or bool(getattr(side_effect, "destructive", False))
        or str(getattr(side_effect, "access_mode", "READ")).upper() != "READ"
    )


def _arguments_signature(arguments: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in dict(arguments or {}).items()},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _execution_read_evidence(state: AgentState) -> list[dict[str, Any]]:
    """Project durable COMPLETED read executions into consumed evidence.

    The durable execution_states carry goal/capability but not concrete tool
    arguments; the goal's declared target supplies the semantic scope so an
    equivalent re-read can be recognized.  No execution is run here.
    """

    read_by_capability: dict[str, ToolMetadata] = {}
    for metadata in state.available_tools:
        if not _is_read_tool_metadata(metadata):
            continue
        for capability in (getattr(metadata, "capabilities", ()) or ()):
            read_by_capability.setdefault(str(capability).upper(), metadata)
    goals_by_id: dict[str, Any] = {}
    if state.goal_tree is not None:
        goals_by_id = {
            str(getattr(goal, "goal_id", "")): goal
            for goal in state.goal_tree.all_goals()
        }
    results: list[dict[str, Any]] = []
    seen_goals: set[str] = set()
    for es in list(state.context_snapshot.get("execution_states") or []):
        status = str(es.get("status") or "").upper()
        if status not in {"COMPLETED", "SUCCESS"}:
            continue
        goal_id = str(es.get("goal_id") or "")
        capability = str(es.get("capability") or "")
        metadata = read_by_capability.get(capability.upper())
        if metadata is None or goal_id in seen_goals:
            continue
        seen_goals.add(goal_id)
        arguments: dict[str, Any] = {}
        goal = goals_by_id.get(goal_id)
        target = getattr(goal, "target", None) or {}
        if isinstance(target, Mapping):
            keyword = str(target.get("keyword") or target.get("topic") or "")
            if keyword:
                arguments["query"] = keyword
        # SEARCH_RESULT executions reference the returned posts; expose their
        # ids as consumed evidence so a follow-up GET_POST_DETAIL step can
        # pick a real post_id instead of degrading into repeated searches.
        # The durable execution projection may carry the posts at the top level
        # (post_ids / post_id) rather than nested steps; both shapes are real
        # verified evidence, so a reasoning/analysis step after a read is
        # grounded regardless of which projection emitted the read.
        post_ids: list[str] = []
        for value in (es.get("post_ids") or []):
            if str(value) and str(value) not in post_ids:
                post_ids.append(str(value))
        single_post = str(es.get("post_id") or "")
        if single_post and single_post not in post_ids:
            post_ids.append(single_post)
        for step in (es.get("steps") or []):
            if not isinstance(step, Mapping):
                continue
            output = step.get("output_artifact") or {}
            if not isinstance(output, Mapping):
                continue
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
                if ref_kind == "POST" and ref_id and ref_id not in post_ids:
                    post_ids.append(ref_id)
        results.append(
            {
                "tool_name": str(getattr(metadata, "name", "")),
                "capabilities": list(getattr(metadata, "capabilities", ()) or ()),
                "arguments": arguments,
                "result_status": "SUCCESS",
                "resource_count": len(post_ids),
                "post_ids": post_ids[:20],
                "source": "EXECUTION_EVIDENCE",
                "goal_id": goal_id,
            }
        )
    return results


def _available_read_fallbacks(
    tools: Sequence[ToolMetadata],
    *,
    failed_tool: str,
) -> list[str]:
    return available_read_fallbacks(tools, failed_tool=failed_tool)


__all__ = ["AgentLoop", "AgentLoopError"]
