"""Phase 4.4.x production-wiring contracts for UPDATE_GOAL grounding.

These tests use the real CommandInterpreter, TargetResolver,
ConversationRuntimeAdapter, and TaskManager with fake model responses only.
They never call DeepSeek, Creator, Java, or an external tool.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.command import Command, CommandContext, CommandInterpreter, CommandType
from greenbook_agent_core.command.models import TaskDelta, TaskDeltaOperation
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task import InMemoryTaskRepository, TaskManager

pytestmark = pytest.mark.integration


class _UnderstandingLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(self.payload, ensure_ascii=False),
                ),
            )],
        )


def _tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="Prepare and schedule an article",
            children=[
                Goal(
                    goal_id="content",
                    description="Write the article",
                    required_capabilities=["GENERATE_CONTENT"],
                ),
                Goal(
                    goal_id="schedule",
                    description="Schedule the article",
                    required_capabilities=["SCHEDULE_PUBLISH"],
                ),
            ],
        ),
    )


async def _adapter() -> tuple[ConversationRuntimeAdapter, TaskManager]:
    manager = TaskManager(InMemoryTaskRepository())
    adapter = ConversationRuntimeAdapter(
        task_manager=manager,
        runtime_service=SimpleNamespace(
            container=RuntimeContainer.for_testing(),
            _execution_repository=None,
            _artifact_store=None,
        ),
    )
    return adapter, manager


@pytest.mark.asyncio
async def test_new_complete_work_is_not_mutation_when_active_task_exists() -> None:
    interpreter = CommandInterpreter(llm=_UnderstandingLLM({
        "command": "CREATE",
        "goal": "search, analyze, write, and schedule an article",
        "request_complexity": "COMPLEX",
        "required_capabilities": [
            "SEARCH_COMMUNITY",
            "ANALYZE_CONTENT_PATTERNS",
            "GENERATE_CONTENT",
            "SCHEDULE_PUBLISH",
        ],
        "task_changes": [],
    }))

    command = await interpreter.interpret(
        "找帖子、总结、写文章并明天下午3点发布",
        CommandContext(
            active_tasks=[{"id": "old-task", "kind": "TASK"}],
            active_target={"kind": "TASK", "id": "old-task"},
        ),
    )

    assert command.type == CommandType.CREATE
    assert command.task_changes == []


@pytest.mark.asyncio
async def test_valid_update_goal_resolves_and_applies_through_adapter() -> None:
    adapter, manager = await _adapter()
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=_tree(),
    )

    async def fake_run_agent_loop(**_: Any) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id="run-valid-update",
            trace_id="trace-valid-update",
        )

    adapter._run_agent_loop = fake_run_agent_loop
    result = await adapter._run_task_deltas(
        deltas=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"goal_id": "schedule"},
            desired_changes={"run_at": "17:00"},
        )],
        command=Command(type=CommandType.MODIFY, goal="change schedule"),
        context=SimpleNamespace(target_candidates=[]),
        request_session=SimpleNamespace(active_task_id=""),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-valid-update",
        trace_id="trace-valid-update",
        llm=None,
        model="test",
    )

    assert result.success is True
    refreshed = await manager.get_required(task.task_id)
    snapshot = GoalTree.model_validate(refreshed.goal_tree_snapshot)
    schedule = next(goal for goal in snapshot.all_goals() if goal.goal_id == "schedule")
    assert schedule.temporal_constraint == {"run_at": "17:00"}


@pytest.mark.asyncio
async def test_empty_update_goal_target_is_controlled_before_manager_apply() -> None:
    adapter, manager = await _adapter()
    before = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=_tree(),
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"goal_id": ""},
        desired_changes={"run_at": "17:00"},
    )

    result = await adapter._run_task_deltas(
        deltas=[delta],
        command=Command(type=CommandType.MODIFY, goal="change schedule"),
        context=SimpleNamespace(target_candidates=[]),
        request_session=SimpleNamespace(active_task_id=before.task_id),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-empty-target",
        trace_id="trace-empty-target",
        llm=None,
        model="test",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "MUTATION_TARGET_REQUIRED"
    assert "UPDATE_GOAL cannot find target Goal" not in result.error_message
    assert result.task_id == ""
    unchanged = await manager.get_required(before.task_id)
    assert unchanged.goal_tree_version == before.goal_tree_version


@pytest.mark.asyncio
async def test_unresolved_mutation_does_not_create_clarification_task() -> None:
    adapter, manager = await _adapter()
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "missing goal"},
        desired_changes={"run_at": "17:00"},
        needs_target_resolution=True,
    )

    result = await adapter._run_task_deltas(
        deltas=[delta],
        command=Command(type=CommandType.MODIFY, goal="change it"),
        context=SimpleNamespace(target_candidates=[]),
        request_session=SimpleNamespace(active_task_id=""),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-unresolved-target",
        trace_id="trace-unresolved-target",
        llm=None,
        model="test",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "MUTATION_TARGET_REQUIRED"
    assert result.task_id == ""
    assert await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1") == []


@pytest.mark.asyncio
async def test_weak_cancel_reference_waits_for_user_even_with_active_task() -> None:
    adapter, manager = await _adapter()
    delta = TaskDelta(
        operation=TaskDeltaOperation.CANCEL_TASK,
        target_reference={},
    )

    result = await adapter._run_task_deltas(
        deltas=[delta],
        command=Command(type=CommandType.CANCEL, goal="cancel it"),
        context=SimpleNamespace(target_candidates=[]),
        request_session=SimpleNamespace(active_task_id="old-task"),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-weak-cancel",
        trace_id="trace-weak-cancel",
        llm=None,
        model="test",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "MUTATION_TARGET_REQUIRED"
    assert result.task_id == ""
    assert await manager.get_active_tasks("c1", user_id="u1", tenant_id="t1") == []
