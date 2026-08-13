"""Observe/Reason/Act/Reflect loop for the GreenBook Agent Intelligence layer."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

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
from greenbook_agent_core.llm_compat import (
    STRUCTURED_OUTPUT_RETRY_MAX_TOKENS,
    add_json_schema_instruction,
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
from .selector import ToolSelectionError, ToolSelector
from .state import AgentState, AgentStatus, Observation


class AgentLoopError(RuntimeError):
    """Raised for an invalid AgentLoop composition or model response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    ) -> AgentRunResult:
        """Run AgentLoop for one canonical Command and GoalTree."""

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
                list(resume.completed_goal_ids) + list(resume.completed_step_ids)
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
            observation = self.observe(state, last_result)
            try:
                if state.preferred_tool_name:
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
                state.history.append({"type": "ACTION", **action.model_dump(mode="json")})
                if action.action == AgentActionType.FINISH:
                    state.finished = True
                    state.status = AgentStatus.COMPLETED
                    return self._result(state, content=action.reason)
                if action.action == AgentActionType.ASK_USER:
                    state.status = AgentStatus.WAITING_HUMAN
                    return self._result(
                        state,
                        question=action.question or action.reason,
                        content=action.reason,
                    )

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
                if action.action == AgentActionType.TOOL_CALL:
                    state.tool_results.append(dict(last_result))
                elif action.action == AgentActionType.CREATE_TASK:
                    state.execution_results.append(dict(last_result))
                state.history.append({"type": "RESULT", "value": last_result})

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
            except (AgentLoopError, ToolSelectionError) as exc:
                state.status = AgentStatus.FAILED
                state.last_error = str(exc)
                return self._result(
                    state,
                    error_code=getattr(exc, "code", "AGENT_LOOP_FAILED"),
                    error_message=str(exc),
                )
            except Exception as exc:
                state.status = AgentStatus.FAILED
                state.last_error = str(exc)
                return self._result(
                    state,
                    error_code="AGENT_LOOP_FAILED",
                    error_message=str(exc),
                )

        state.status = AgentStatus.MAX_ITERATIONS
        state.last_error = "AgentLoop reached its iteration limit."
        return self._result(
            state,
            error_code="AGENT_MAX_ITERATIONS",
            error_message=state.last_error,
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
        request = {
            "command": state.command.model_dump(mode="json") if state.command else {},
            "goal_tree": state.goal_tree.model_dump(mode="json") if state.goal_tree else {},
            "observation": observation.model_dump(mode="json"),
            "available_tool_metadata": [_metadata_payload(item) for item in state.available_tools],
            "memory_snapshot": state.memory_snapshot,
        }
        response = await _structured_call(
            client,
            model,
            _REASON_PROMPT,
            "greenbook_agent_action",
            AgentAction.model_json_schema(),
            request,
        )
        try:
            return AgentAction.model_validate(
                _normalize_agent_action_payload(_response_payload(response)),
            )
        except ValidationError as exc:
            raise AgentLoopError(
                "AGENT_ACTION_SCHEMA_INVALID",
                "Reason output does not match AgentAction schema: "
                + str(exc),
            ) from exc

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
        response = await _structured_call(
            client,
            model,
            _REFLECT_PROMPT,
            "greenbook_agent_reflection",
            Reflection.model_json_schema(),
            request,
        )
        try:
            return Reflection.model_validate(
                _normalize_reflection_payload(_response_payload(response)),
            )
        except ValidationError as exc:
            raise AgentLoopError(
                "AGENT_REFLECTION_SCHEMA_INVALID",
                "Reflection output does not match Reflection schema: "
                + str(exc),
            ) from exc

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
            result.setdefault("queued", True)
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
        state.completed_task_ids.extend(
            task.task_id
            for task in state.goal_tree.task_nodes
            if task.task_id not in state.completed_task_ids
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
            state=state,
        )


async def _structured_call(
    client: Any,
    model: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    request: dict[str, Any],
) -> Any:
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        "temperature": 0.0,
        **structured_provider_options(client, model),
    }
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "response_format" not in str(exc).lower() and "json_schema" not in str(exc).lower():
            raise
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = add_json_schema_instruction(kwargs["messages"], schema)
        response = await client.chat.completions.create(**kwargs)

    # Some OpenAI-compatible providers accept the request but occasionally
    # return an empty ``content`` field after a long reasoning trace.  Retry
    # once with an explicit JSON-object request and a bounded output budget;
    # the caller still validates the returned payload against the typed
    # schema, so this does not create a local or hard-coded action.
    if not _has_structured_payload(response):
        retry_kwargs = dict(kwargs)
        retry_kwargs["response_format"] = {"type": "json_object"}
        retry_kwargs["messages"] = add_json_schema_instruction(
            [dict(message) for message in kwargs["messages"]],
            schema,
        )
        retry_kwargs["max_tokens"] = STRUCTURED_OUTPUT_RETRY_MAX_TOKENS
        response = await client.chat.completions.create(**retry_kwargs)
    return response


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
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise AgentLoopError("AGENT_RESPONSE_INVALID_JSON", "LLM returned invalid Agent JSON.") from exc


def _has_structured_payload(response: Any) -> bool:
    """Return whether a provider response contains parseable response text."""

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False
    if getattr(message, "parsed", None) is not None:
        return True
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return bool(content)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(item, Mapping) and str(item.get("text", "")).strip()
            for item in content
        )
    return False


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


def _normalize_agent_action_payload(value: Any) -> Any:
    """Treat nullable JSON-mode defaults as their typed empty values."""

    if not isinstance(value, Mapping):
        return value
    payload = dict(value)
    for field in ("tool_args", "plan_patch"):
        if payload.get(field) is None:
            payload[field] = {}
    for field in ("tool_name", "question", "reason"):
        if payload.get(field) is None:
            payload[field] = ""
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
    return next(
        (
            task for task in state.goal_tree.task_nodes
            if task.task_id not in state.completed_task_ids
        ),
        None,
    )


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

Do not emit MCP handlers, positional tool choices, business Agent names, or
execution lifecycle operations. Do not reinterpret the raw user message.
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
COMMUNITY_DATA, CREATOR_RESEARCH, and MODEL_INFERENCE, and never describe
community evidence as the user's own historical performance. If personal
evidence is empty, say that the personal sample is insufficient before using
community evidence.
"""


def _observation_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    return observation_evidence(result)


def _available_read_fallbacks(
    tools: Sequence[ToolMetadata],
    *,
    failed_tool: str,
) -> list[str]:
    return available_read_fallbacks(tools, failed_tool=failed_tool)


__all__ = ["AgentLoop", "AgentLoopError"]
