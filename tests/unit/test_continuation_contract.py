"""Continuation must not restart terminal Tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenbook_agent_api.services.action_loop_executor import ActionLoopExecutor
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskConfirmationState,
    TaskStatus,
)
from greenbook_agent_core.task.semantic_confirmation import confirmation_identity
from greenbook_agent_core.execution.runtime_result import RuntimeResult


class _NeverStartActionLoop:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("terminal Task entered continuation ActionLoop")


class _TerminalTaskManager:
    def __init__(self, task: Task) -> None:
        self.task = task

    async def get_task(self, *_args, **_kwargs):
        return self.task

    async def resume_task(self, *_args, **_kwargs):
        raise AssertionError("terminal Task requested a lifecycle resume")


class _ContinuationTaskManager:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.resume_calls = 0

    async def get_task(self, *_args, **_kwargs):
        return self.task

    async def resume_task(self, *_args, **_kwargs):
        self.resume_calls += 1
        self.task.status = TaskStatus.READY
        return self.task


class _RecordingActionLoop:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, _command, **kwargs):
        self.calls += 1
        request = kwargs["request"]
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=request.run_id,
            task_id=task.task_id,
        )


@pytest.mark.asyncio
async def test_terminal_task_does_not_reenter_continuation() -> None:
    task = Task(
        task_id="terminal-task",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="already complete",
        status=TaskStatus.COMPLETED,
    )
    action_loop = _NeverStartActionLoop()
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=_TerminalTaskManager(task),
        action_loop=action_loop,
    )

    result = await executor.resume_task(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        run_id="run-terminal",
        trace_id="trace-terminal",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
    )

    assert result is None
    assert action_loop.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "objective_intent"),
    [
        (TaskStatus.COMPLETED, "UPDATE_DRAFT"),
        (TaskStatus.COMPLETED, "CREATE_SCHEDULE"),
        (TaskStatus.FAILED, "PUBLISH_NOW"),
    ],
)
async def test_confirmed_new_objective_reopens_terminal_task(
    task_status: TaskStatus,
    objective_intent: str,
) -> None:
    task = Task(
        task_id=f"terminal-{objective_intent.lower()}",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        status=task_status,
        requires_confirmation=True,
        confirmation_state=TaskConfirmationState.CONFIRMED,
        confirmation_version=1,
        confirmed_version=1,
        confirmation_snapshot_hash=f"snapshot-{objective_intent}",
        version=14,
        objectives=[Objective(
            task_id=f"terminal-{objective_intent.lower()}",
            intent=objective_intent,
            status=ObjectiveStatus.PENDING,
            required_capabilities=[objective_intent],
        )],
    )
    manager = _ContinuationTaskManager(task)
    action_loop = _RecordingActionLoop()
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=manager,
        action_loop=action_loop,
    )

    result = await executor.resume_task(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        run_id=f"run-{objective_intent.lower()}",
        trace_id="trace-confirmed-new-objective",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
        expected_confirmation_id=confirmation_identity(task),
        expected_confirmation_version=task.confirmation_version,
        expected_task_version=task.version,
    )

    assert result is not None
    assert result.status == "COMPLETED"
    assert manager.resume_calls == 1
    assert action_loop.calls == 1


@pytest.mark.asyncio
async def test_cancelled_task_with_pending_objective_stays_closed() -> None:
    task = Task(
        task_id="cancelled-with-pending-objective",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        status=TaskStatus.CANCELLED,
        requires_confirmation=True,
        confirmation_state=TaskConfirmationState.CONFIRMED,
        confirmation_version=1,
        confirmed_version=1,
        confirmation_snapshot_hash="snapshot-cancelled",
        version=14,
        objectives=[Objective(
            task_id="cancelled-with-pending-objective",
            intent="CREATE_SCHEDULE",
            status=ObjectiveStatus.PENDING,
            required_capabilities=["CREATE_SCHEDULE"],
        )],
    )
    action_loop = _NeverStartActionLoop()
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=_TerminalTaskManager(task),
        action_loop=action_loop,
    )

    result = await executor.resume_task(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        run_id="run-cancelled",
        trace_id="trace-cancelled",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
        expected_confirmation_id=confirmation_identity(task),
        expected_confirmation_version=task.confirmation_version,
        expected_task_version=task.version,
    )

    assert result is None
    assert action_loop.calls == 0
