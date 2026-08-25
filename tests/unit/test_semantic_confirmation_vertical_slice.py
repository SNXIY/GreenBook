"""Semantic Confirmation vertical-slice invariants.

These tests deliberately use a small in-memory Task repository and explicit
runtime doubles.  They prove the admission/CAS boundary without exercising
Java, an LLM, or a second queue implementation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from apps.agent_api.greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from apps.agent_api.greenbook_agent_api.services.action_loop_executor import ActionLoopExecutor
from greenbook_agent_core.actionloop.loop import _semantic_confirmation_blocks_write
from greenbook_agent_core.activity.projector import UserActivityProjector
from greenbook_agent_core.command.models import (
    Command,
    CommandItem,
    CommandType,
    ResolvedSemanticState,
    TaskDelta,
)
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.task import (
    InMemoryTaskRepository,
    TaskConfirmationConflictError,
    TaskConfirmationState,
    TaskManager,
)
from greenbook_agent_core.task.semantic_confirmation import (
    canonical_snapshot_hash,
    confirmation_identity,
    confirmation_policy,
    render_confirmation,
)
from greenbook_agent_core.turn.models import AssembledTurnContext


def _multi_write_command() -> Command:
    return Command(
        type=CommandType.CREATE,
        goal="create two publication outcomes",
        raw_input="create two publication outcomes",
        items=[
            CommandItem(
                title="Java",
                topic="Java",
                capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
            ),
            CommandItem(
                title="Agent",
                topic="Agent",
                capabilities=["GENERATE_CONTENT", "CREATE_SCHEDULE"],
                constraints={
                    "publication_intent": "SCHEDULED_PUBLISH",
                    "run_at": "2026-08-22T06:00:00Z",
                },
            ),
        ],
        required_capabilities=[
            "GENERATE_CONTENT",
            "PUBLISH_NOW",
            "CREATE_SCHEDULE",
        ],
    )


class _Assembler:
    async def assemble(self, **kwargs: Any) -> AssembledTurnContext:
        conversation_id = str(kwargs.get("conversation_id") or "c1")
        return AssembledTurnContext(
            conversation_id=conversation_id,
            user_id="u1",
            tenant_id="t1",
            timezone="Asia/Shanghai",
            snapshot=ContextSnapshot(conversation_id=conversation_id),
        )


class _Interpreter:
    def __init__(self, command: Command) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, *_args: Any, **_kwargs: Any) -> Command:
        self.calls += 1
        return self.command


class _ConfirmationExecutor:
    """Preparation/runtime double with observable side-effect boundaries."""

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager
        self.prepare_calls = 0
        self.run_for_command_calls = 0
        self.resume_calls: list[dict[str, Any]] = []
        self.durable_submissions = 0
        self.java_writes = 0

    async def prepare_for_confirmation(self, *, command: Command, **kwargs: Any):
        self.prepare_calls += 1
        task = await self.manager.create_task(
            conversation_id=kwargs["conversation_id"],
            user_id=kwargs["user_id"],
            tenant_id=kwargs["tenant_id"],
            goal=command.requested_goal,
        )

        # The real executor persists the same canonical Objective projection;
        # the double keeps the identity assertions explicit and deterministic.
        from greenbook_agent_core.task.objective_compat import objectives_from_items

        task.objectives = objectives_from_items(
            command.items,
            task.task_id,
            timezone="Asia/Shanghai",
            resolved_state=command.resolved_semantics,
        )
        repository = self.manager.repository
        await repository.update(task, expected_version=task.version)
        return await self.manager.get_required(task.task_id)

    async def run_for_command(self, **kwargs: Any) -> RuntimeResult:
        self.run_for_command_calls += 1
        self.durable_submissions += 1
        self.java_writes += 1
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=str(kwargs.get("run_id") or ""),
            execution_path="action_loop",
        )

    async def resume_task(self, **kwargs: Any) -> RuntimeResult:
        self.resume_calls.append(dict(kwargs))
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=str(kwargs.get("run_id") or ""),
            task_id=str(kwargs.get("task_id") or ""),
            execution_path="action_loop",
        )


class _NeverStartActionLoop:
    async def run(self, **_kwargs: Any) -> None:
        raise AssertionError("stale semantic confirmation entered ActionLoop")


def _coordinator(
    command: Command,
    executor: _ConfirmationExecutor,
    manager: TaskManager,
    interpreter: _Interpreter | None = None,
) -> TurnCoordinator:
    return TurnCoordinator(
        context_assembler=_Assembler(),
        command_runtime=interpreter or _Interpreter(command),
        action_loop_executor=executor,
        task_manager=manager,
        tool_registry=[],
    )


@pytest.mark.asyncio
async def test_pending_task_never_starts_actionloop_or_submits_write() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    result = await _coordinator(_multi_write_command(), executor, manager).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-pending",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "SEMANTIC_CONFIRMATION_REQUIRED"
    assert executor.prepare_calls == 1
    assert executor.run_for_command_calls == 0
    assert executor.durable_submissions == 0
    assert executor.java_writes == 0

    tasks = await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1")
    assert len(tasks) == 1
    assert tasks[0].confirmation_state == TaskConfirmationState.CONFIRMATION_PENDING


@pytest.mark.asyncio
async def test_confirm_resumes_same_task_with_command_none_and_preserves_identity() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    interpreter = _Interpreter(_multi_write_command())
    result = await _coordinator(
        interpreter.command,
        executor,
        manager,
        interpreter,
    ).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-confirm",
    )
    assert result.status == "WAITING_HUMAN"
    assert interpreter.calls == 1
    pending = (await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1"))[0]
    identity = confirmation_identity(pending)
    objective_identity = [
        (item.objective_id, dict(item.constraints)) for item in pending.objectives
    ]

    confirmed = await manager.confirm_task(
        pending.task_id,
        expected_confirmation_version=pending.confirmation_version,
        expected_task_version=pending.version,
        expected_confirmation_id=identity,
    )
    assert confirmed.confirmation_state == TaskConfirmationState.CONFIRMED

    # This is the same call the typed control path uses after a successful CAS.
    resumed = await executor.resume_task(
        task_id=confirmed.task_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-confirm",
        trace_id="trace-confirm",
        session=None,
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
    )
    assert resumed.status == "COMPLETED"
    assert len(executor.resume_calls) == 1
    assert executor.resume_calls[0]["task_id"] == pending.task_id
    assert executor.resume_calls[0]["command"] is None
    assert interpreter.calls == 1

    after = await manager.get_required(pending.task_id)
    assert [
        (item.objective_id, dict(item.constraints)) for item in after.objectives
    ] == objective_identity


@pytest.mark.asyncio
async def test_duplicate_confirm_does_not_create_a_second_resume_or_write() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    await _coordinator(_multi_write_command(), executor, manager).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-duplicate",
    )
    pending = (await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1"))[0]
    kwargs = {
        "expected_confirmation_version": pending.confirmation_version,
        "expected_task_version": pending.version,
        "expected_confirmation_id": confirmation_identity(pending),
    }
    first = await manager.confirm_task(pending.task_id, **kwargs)
    second = await manager.confirm_task(pending.task_id, **kwargs)
    assert first.task_id == second.task_id
    assert second.confirmation_state == TaskConfirmationState.CONFIRMED

    # A caller retries the control request, but only the first successful CAS
    # is allowed to dispatch the existing Task.
    await executor.resume_task(
        task_id=first.task_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-duplicate",
        trace_id="trace-duplicate",
        session=None,
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
    )
    assert len(executor.resume_calls) == 1
    assert executor.java_writes == 0


@pytest.mark.asyncio
async def test_modify_supersedes_old_version_and_new_version_requires_confirmation() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    await _coordinator(_multi_write_command(), executor, manager).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-modify",
    )
    pending = (await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1"))[0]
    old_identity = confirmation_identity(pending)
    superseded = await manager.supersede_confirmation(
        pending.task_id,
        expected_confirmation_version=pending.confirmation_version,
        expected_task_version=pending.version,
    )
    assert superseded.confirmation_state == TaskConfirmationState.SUPERSEDED
    with pytest.raises(TaskConfirmationConflictError):
        await manager.confirm_task(
            pending.task_id,
            expected_confirmation_version=pending.confirmation_version,
            expected_task_version=superseded.version,
            expected_confirmation_id=old_identity,
        )

    revised = await manager.set_confirmation_pending(
        pending.task_id,
        snapshot_hash="new-canonical-snapshot",
        resume_run_id="run-modify-new",
    )
    assert revised.confirmation_state == TaskConfirmationState.CONFIRMATION_PENDING
    assert revised.confirmation_version > pending.confirmation_version
    with pytest.raises(TaskConfirmationConflictError):
        await manager.confirm_task(
            revised.task_id,
            expected_confirmation_version=pending.confirmation_version,
            expected_task_version=revised.version,
            expected_confirmation_id=old_identity,
        )


@pytest.mark.asyncio
async def test_confirm_cancel_race_has_one_cas_winner_and_no_loser_resume() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    await _coordinator(_multi_write_command(), executor, manager).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-race",
    )
    pending = (await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1"))[0]
    identity = confirmation_identity(pending)

    async def confirm():
        return await manager.confirm_task(
            pending.task_id,
            expected_confirmation_version=pending.confirmation_version,
            expected_task_version=pending.version,
            expected_confirmation_id=identity,
        )

    async def cancel():
        return await manager.cancel_confirmation(
            pending.task_id,
            expected_confirmation_version=pending.confirmation_version,
            expected_task_version=pending.version,
            expected_confirmation_id=identity,
        )

    results = await asyncio.gather(confirm(), cancel(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    winner = await manager.get_required(pending.task_id)
    assert winner.confirmation_state in {
        TaskConfirmationState.CONFIRMED,
        TaskConfirmationState.CANCELLED,
    }
    assert executor.resume_calls == []
    assert executor.java_writes == 0


@pytest.mark.asyncio
async def test_confirm_modify_race_has_one_cas_winner_and_no_execution() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    executor = _ConfirmationExecutor(manager)
    await _coordinator(_multi_write_command(), executor, manager).execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="create two publication outcomes",
        run_id="run-confirm-modify-race",
    )
    pending = (await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1"))[0]
    identity = confirmation_identity(pending)
    kwargs = {
        "expected_confirmation_version": pending.confirmation_version,
        "expected_task_version": pending.version,
        "expected_confirmation_id": identity,
    }

    async def confirm():
        return await manager.confirm_task(pending.task_id, **kwargs)

    async def modify():
        return await manager.supersede_confirmation(pending.task_id, **kwargs)

    results = await asyncio.gather(confirm(), modify(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    winner = await manager.get_required(pending.task_id)
    assert winner.confirmation_state in {
        TaskConfirmationState.CONFIRMED,
        TaskConfirmationState.SUPERSEDED,
    }
    assert executor.resume_calls == []
    assert executor.java_writes == 0


@pytest.mark.asyncio
async def test_simple_search_and_single_write_remain_auto_admitted() -> None:
    manager = TaskManager(InMemoryTaskRepository())

    search = Command(
        type=CommandType.QUERY,
        goal="search posts",
        raw_input="search posts",
        required_capabilities=["SEARCH_COMMUNITY"],
        items=[CommandItem(topic="Java", capabilities=["SEARCH_COMMUNITY"])],
    )
    search_executor = _ConfirmationExecutor(manager)
    search_result = await _coordinator(search, search_executor, manager).execute(
        conversation_id="c-search",
        user_id="u1",
        tenant_id="t1",
        message="search posts",
        run_id="run-search",
    )
    assert search_result.status == "COMPLETED"
    assert search_executor.prepare_calls == 0
    assert search_executor.run_for_command_calls == 0

    single_write = Command(
        type=CommandType.CREATE,
        goal="create one draft",
        raw_input="create one draft",
        required_capabilities=["CREATE_DRAFT"],
        items=[CommandItem(topic="Java", capabilities=["CREATE_DRAFT"])],
    )
    write_executor = _ConfirmationExecutor(manager)
    write_result = await _coordinator(single_write, write_executor, manager).execute(
        conversation_id="c-write",
        user_id="u1",
        tenant_id="t1",
        message="create one draft",
        run_id="run-write",
    )
    assert write_result.status == "COMPLETED"
    assert write_executor.run_for_command_calls == 1


def test_confirmation_renderer_is_deterministic_and_canonical() -> None:
    command = Command(type=CommandType.CREATE, goal="publish Java")
    state = ResolvedSemanticState(
        items=[
            {
                "topic": "Java",
                "capabilities": ["PUBLISH_NOW"],
                "run_at": "2026-08-22T06:00:00Z",
                "constraints": {
                    "run_at": "2026-08-22T06:00:00Z",
                    "timezone": "Asia/Shanghai",
                    "publication_intent": "IMMEDIATE_PUBLISH",
                },
                "target_reference": {
                    "kind": "POST",
                    "label": "Java 原帖",
                    "resource_id": "post-private",
                },
            }
        ]
    )
    task = SimpleNamespace(
        task_id="task-render",
        confirmation_version=1,
        goal_summary="Publish Java",
        objectives=[
            SimpleNamespace(
                description="Java",
                intent="Publish Java",
                constraints={
                    "run_at": "2026-08-22T06:00:00Z",
                    "timezone": "Asia/Shanghai",
                    "publication_intent": "IMMEDIATE_PUBLISH",
                },
                required_capabilities=["PUBLISH_NOW"],
            )
        ],
    )
    first = render_confirmation(
        command,
        state,
        task,
        confirmation_id="stable-confirmation-id",
    )
    second = render_confirmation(
        command,
        state,
        task,
        confirmation_id="stable-confirmation-id",
    )
    assert first == second
    objective = first["objectives"][0]
    assert objective["topic"] == "Java"
    assert objective["desired_outcome"] == "Publish Java"
    assert objective["target"] == {
        "kind": "POST",
        "label": "Java 原帖",
        "resource_id": "post-private",
    }
    assert objective["run_at"] == "2026-08-22T06:00:00Z"
    assert objective["timezone"] == "Asia/Shanghai"
    assert objective["has_real_side_effect"] is True


def test_write_defense_requires_the_current_confirmed_version() -> None:
    pending = type(
        "TaskLike",
        (),
        {
            "requires_confirmation": True,
            "confirmation_state": TaskConfirmationState.CONFIRMED,
            "confirmation_version": 2,
            "confirmed_version": 1,
        },
    )()
    assert _semantic_confirmation_blocks_write(pending) is True

    current = type(
        "TaskLike",
        (),
        {
            "requires_confirmation": True,
            "confirmation_state": TaskConfirmationState.CONFIRMED,
            "confirmation_version": 2,
            "confirmed_version": 2,
        },
    )()
    assert _semantic_confirmation_blocks_write(current) is False


def test_confirmation_activity_projection_has_only_public_business_facts() -> None:
    projected = UserActivityProjector().project_semantic_confirmation(
        conversation_id="c1",
        run_id="run-private",
        task_id="task-private",
        source_key="run-private",
        confirmation={
            "confirmation_id": "opaque-confirmation",
            "confirmation_version": 1,
            "title": "Two posts",
            "objectives": [
                {
                    "topic": "Java",
                    "desired_outcome": "Publish",
                    "action": "PUBLISH_NOW",
                    "execution_id": "exec-private",
                    "operation_id": "op-private",
                    "capability": "PUBLISH_NOW",
                    "target": {"kind": "POST", "label": "Java"},
                }
            ],
        },
    )
    payload = projected.event.safe_payload
    assert payload["confirmation_id"] == "opaque-confirmation"
    objective = payload["objectives"][0]
    assert objective["desired_outcome"] == "Publish"
    assert "action" not in objective
    assert "capability" not in objective
    assert "execution_id" not in objective
    assert "operation_id" not in objective


def test_single_write_is_not_double_counted_across_canonical_views() -> None:
    command = Command(
        type=CommandType.MODIFY,
        goal="publish one post",
        required_capabilities=["PUBLISH_NOW"],
        task_changes=[
            TaskDelta(
                operation="UPDATE_GOAL",
                target_reference={"id": "post-1", "kind": "POST"},
                desired_changes={"semantic_action": "PUBLISH_NOW"},
            )
        ],
    )
    decision = confirmation_policy(
        command,
        ResolvedSemanticState(capabilities=["PUBLISH_NOW"]),
    )
    assert decision.required is False
    assert decision.reason == "single_simple_write"


def test_snapshot_hash_excludes_provenance_and_audit_ids() -> None:
    task = type(
        "TaskLike",
        (),
        {"objectives": [], "resource_index": []},
    )()
    command_one = Command(
        type=CommandType.CREATE,
        goal="publish one post",
        task_changes=[
            TaskDelta(
                change_id="change-1",
                operation="UPDATE_GOAL",
                desired_changes={"semantic_action": "PUBLISH_NOW"},
            )
        ],
    )
    command_two = command_one.model_copy(deep=True)
    command_two.task_changes[0].change_id = "change-2"
    state_one = ResolvedSemanticState(source_command_id="command-1")
    state_two = ResolvedSemanticState(source_command_id="command-2")
    assert canonical_snapshot_hash(command_one, state_one, task) == canonical_snapshot_hash(
        command_two,
        state_two,
        task,
    )


@pytest.mark.asyncio
async def test_policy_false_task_records_auto_admitted_state() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c-admit",
        user_id="u1",
        tenant_id="t1",
        goal="simple search",
    )
    assert task.confirmation_state == TaskConfirmationState.RESOLVED
    admitted = await manager.auto_admit_task(task.task_id)
    assert admitted.confirmation_state == TaskConfirmationState.AUTO_ADMITTED
    assert admitted.requires_confirmation is False
    assert (
        await manager.auto_admit_task(task.task_id)
    ).confirmation_state == TaskConfirmationState.AUTO_ADMITTED


@pytest.mark.asyncio
async def test_stale_confirmed_resume_cannot_start_actionloop() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c-stale-resume",
        user_id="u1",
        tenant_id="t1",
        goal="two writes",
    )
    pending = await manager.set_confirmation_pending(
        task.task_id,
        snapshot_hash="canonical-v1",
        resume_run_id="run-v1",
    )
    confirmed = await manager.confirm_task(
        pending.task_id,
        expected_confirmation_version=pending.confirmation_version,
        expected_task_version=pending.version,
        expected_confirmation_id=confirmation_identity(pending),
    )
    old_marker = {
        "confirmation_id": confirmation_identity(confirmed),
        "confirmation_version": confirmed.confirmation_version,
        "task_version": confirmed.version,
    }
    await manager.set_confirmation_pending(
        confirmed.task_id,
        snapshot_hash="canonical-v2",
        resume_run_id="run-v2",
    )

    executor = ActionLoopExecutor(
        adapter=object(),
        task_manager=manager,
        action_loop=_NeverStartActionLoop(),
    )
    result = await executor.resume_task(
        task_id=confirmed.task_id,
        conversation_id="c-stale-resume",
        user_id="u1",
        tenant_id="t1",
        run_id="run-v1",
        trace_id="trace-v1",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
        **{
            "expected_confirmation_id": old_marker["confirmation_id"],
            "expected_confirmation_version": old_marker["confirmation_version"],
            "expected_task_version": old_marker["task_version"],
        },
    )
    assert result.status == "FAILED"
    assert result.error_code == "SEMANTIC_CONFIRMATION_STALE"
