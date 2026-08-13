"""Canonical conversation-to-Agent Runtime composition boundary.

The adapter owns request scope and projections only.  User input enters as a
typed ``Command``, is decomposed into a ``GoalTree``, and is then handed to
``AgentLoop``.  Durable Task and Execution state stay in their respective
repositories; this module never interprets natural language or runs a tool
directly.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from greenbook_agent_core.agent import AgentLoop, AgentRunResult
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
)
from greenbook_agent_core.context import ContextBuilder, ContextSnapshot, SessionContext
from greenbook_agent_core.conversation import (
    ExecutionControlCommand,
)
from greenbook_agent_core.execution.input import ExecutionInput
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.execution.submission import QueueExecutionSubmissionService
from greenbook_agent_core.goal import GoalCompiler, GoalDecomposer
from greenbook_agent_core.goal.models import GoalTree
from greenbook_agent_core.memory import MemoryRetriever
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.models import Task

from ..models.runtime_context import RuntimeContext, TaskContext
from ..models.runtime_result import RuntimeResult
from .runtime_agent_service import RuntimeAgentService
from .task_provider import TaskProvider, TaskProviderError, TaskScope

logger = logging.getLogger(__name__)


class ConversationRuntimeAdapter:
    """Bind one request to the canonical Agent and Reliable Runtime layers."""

    def __init__(
        self,
        *,
        task_provider: TaskProvider | Any | None = None,
        task_manager: Any | None = None,
        runtime_service: RuntimeAgentService | Any | None = None,
        execution_repository: Any | None = None,
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
            execution_repository=execution_repository,
            container=self._container,
        )
        self._execution_repository = execution_repository or getattr(
            self._runtime_service,
            "_execution_repository",
            None,
        )
        self._command_runtime = command_runtime
        self._goal_decomposer = goal_decomposer
        self._agent_loop = agent_loop
        self._goal_compiler = goal_compiler or GoalCompiler(
            registry=self._container.capability_registry,
        )
        self._tool_registry = tool_registry or self._container.tool_registry
        self._target_resolver = target_resolver or TargetResolver()
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
    ) -> RuntimeResult:
        """Run a natural-language turn through the one production path."""

        request_session = self._coerce_session(
            session,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone or "Asia/Shanghai",
        )
        run = run_id or str(uuid.uuid4())
        trace = trace_id or str(uuid.uuid4())
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

            command = await self._command_runtime.interpret(
                message,
                context,
                llm=llm,
                model=model,
            )
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
            if command.needs_clarification:
                return self._clarification_result(
                    command,
                    context=context,
                    run_id=run,
                    trace_id=trace,
                    error_code="COMMAND_CLARIFICATION_REQUIRED",
                )
            if command.target_resolution == TargetResolutionStatus.AMBIGUOUS.value:
                return self._clarification_result(
                    command,
                    context=context,
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
            if command.requires_target:
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

            goal_tree = await self._goal_decomposer.decompose(
                command,
                context,
                available_capabilities=self._container.capability_registry.list_all(),
                llm=llm,
                model=model,
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
                detach=detach,
                completion_callback=completion_callback,
            )
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

    async def _run_agent_loop(
        self,
        *,
        command: Command,
        goal_tree: GoalTree,
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
    ) -> RuntimeResult:
        del detach

        durable_task = await self._bind_task(
            command=command,
            goal_tree=goal_tree,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            session=request_session,
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
                goal=description,
                goal_category="COMPOSITE",
                execution_metadata={
                    "goal_tree": (
                        state.goal_tree.model_dump(mode="json")
                        if getattr(state, "goal_tree", None) is not None
                        else {}
                    ),
                    "context_snapshot_id": getattr(state, "context_snapshot_id", ""),
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
                user_message=description,
                conversation_context=context_snapshot.decision_payload(),
                session=request_session,
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
            )

        async def submit_plan(*, graph: Any, plan: Any, state: Any) -> Mapping[str, Any]:
            del graph
            submit = getattr(self._runtime_service, "submit_plan", None)
            if not callable(submit):
                raise RuntimeError(
                    "RuntimeAgentService.submit_plan is required for queue-native execution."
                )
            result = await submit(
                runtime_context_for(state, plan),
                plan,
                completion_callback=completion_callback,
            )
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
                    )
                ],
            )
            result = dict(await submit_plan(graph=None, plan=plan, state=state))
            execution_id = str(result.get("execution_id") or "")
            current_task = getattr(state, "task", None)
            bind_execution = getattr(self._task_manager, "bind_execution", None)
            if execution_id and current_task is not None and callable(bind_execution):
                updated = bind_execution(
                    current_task.task_id,
                    execution_id,
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
        return self._agent_loop_result(
            result,
            run_id=run_id,
            trace_id=trace_id,
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
    ) -> Task | None:
        manager = self._task_manager
        if manager is None:
            return None
        target_task_id = str(
            getattr(getattr(command, "target", None), "task_id", "")
            or (getattr(command, "resolved_target", None) or {}).get("task_id", "")
        )
        command_type = command.type.value
        # A clarification turn can omit the original Task id even though the
        # conversation has one explicit active binding.  Reuse that binding
        # for a MODIFY/CANCEL command; never apply it to a new task request.
        if not target_task_id and command_type in {"MODIFY", "CANCEL"}:
            target_task_id = str(getattr(session, "active_task_id", "") or "")
        schedule_followup = (
            command_type == "CREATE"
            and "SCHEDULE_PUBLISH" in {
                str(item).upper() for item in command.required_capabilities
            }
            and bool(getattr(session, "active_draft_id", None))
        )
        if schedule_followup and not target_task_id:
            target_task_id = str(getattr(session, "active_task_id", "") or "")
        if target_task_id and (command_type in {"MODIFY", "CANCEL"} or schedule_followup):
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
            return list(await values) if inspect.isawaitable(values) else list(values)
        manager = self._task_manager
        list_active = getattr(manager, "get_active_tasks", None)
        if callable(list_active):
            values = list_active(
                scope.conversation_id,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
            )
            return list(await values) if inspect.isawaitable(values) else list(values)
        return []

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
            "Select one target before continuing."
            if error_code == "AMBIGUOUS_TARGET"
            else "Please clarify the requested outcome."
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
            partial_results={
                "agent_loop": True,
                "iterations": result.iterations,
                "actions": result.actions,
                "observations": result.observations,
                "tool_results": result.tool_results,
                "execution_results": result.execution_results,
            },
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


__all__ = ["ConversationRuntimeAdapter"]
