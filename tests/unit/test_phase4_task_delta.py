"""Phase 4 dynamic multi-task conversation: TaskDelta apply + validation.

A user sentence mutates existing conversation work (cancel, add goal, update
desired state, create task). Understanding emits TaskDelta list; Python
validates deterministically and applies through TaskManager / GoalTree patch,
without regenerating the whole GoalTree and without a second Understanding LLM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
    _append_delta_goal,
    _bind_semantic_action_resource,
    _command_scoped_to_goal_tree,
    _cancel_delta_goal,
    _context_scoped_to_task,
    _patch_delta_goal,
    _session_scoped_to_task,
)
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    StructuredCommandOutput,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.context import RecentEntity, SessionContext
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.command.target import TargetResolver
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.goal import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.task.manager import TaskManager
from greenbook_agent_core.task.models import TaskResourceRef, TaskStatus

# ── Schema: TaskDelta carries mutation semantics (not tool capabilities) ──


def test_structured_output_accepts_task_changes() -> None:
    parsed = StructuredCommandOutput(
        command=CommandType.MODIFY,
        goal="调整任务",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.CANCEL_TASK,
                change_id="c1",
                target_reference={"label": "Redis 分析"},
            ),
            TaskDelta(
                operation=TaskDeltaOperation.ADD_GOAL,
                target_reference={"label": "Agent 总结"},
                desired_changes={"description": "根据总结写一篇文章"},
                dependency_reference=[{"goal_id": "g2"}],
            ),
        ],
    )
    assert len(parsed.task_changes) == 2
    assert parsed.task_changes[0].operation == TaskDeltaOperation.CANCEL_TASK
    assert parsed.task_changes[1].dependency_reference[0]["goal_id"] == "g2"
    schema = StructuredCommandOutput.model_json_schema()
    assert "task_changes" in schema["properties"]


def test_delta_operation_is_state_mutation_not_tool() -> None:
    operations = {op.value for op in TaskDeltaOperation}
    # Tool-level capabilities must never leak into TaskDelta operations.
    assert "CANCEL_SCHEDULE" not in operations
    assert "SEARCH_POSTS" not in operations
    assert "GENERATE_DRAFT" not in operations
    assert {"CREATE_TASK", "ADD_GOAL", "UPDATE_GOAL",
            "CANCEL_GOAL", "CANCEL_TASK", "CONTINUE_TASK"}.issubset(operations)


# ── GoalTree patch helpers ───────────────────────────────────────────────


def _tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="找 Agent 讨论并总结",
            required_capabilities=["SEARCH_COMMUNITY"],
            children=[
                Goal(
                    goal_id="g2",
                    description="总结共同方法",
                    required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
                )
            ],
        ),
        task_nodes=[
            TaskNode(task_id="t1", goal_id="g1", capability="SEARCH_COMMUNITY"),
        ],
        source="LLM_STRUCTURED_OUTPUT",
    )


def test_add_goal_appends_with_dependency() -> None:
    tree = _tree()
    delta = TaskDelta(
        operation=TaskDeltaOperation.ADD_GOAL,
        desired_changes={
            "description": "根据总结写一篇文章",
            "required_capabilities": ["GENERATE_CONTENT"],
        },
        dependency_reference=[{"goal_id": "g2"}],
    )
    updated = _append_delta_goal(tree, delta)
    goals = updated.all_goals()
    added = next((goal for goal in goals if goal.description == "根据总结写一篇文章"), None)
    assert added is not None
    assert added.required_capabilities == ["GENERATE_CONTENT"]
    assert added.dependencies == ["g2"]
    updated.validate_tree()


def test_add_goal_is_idempotent_within_same_tree_state() -> None:
    delta = TaskDelta(
        operation=TaskDeltaOperation.ADD_GOAL,
        change_id="dup",
        desired_changes={"description": "重复目标"},
    )
    # The append helper is pure; idempotency is enforced by the apply loop via
    # applied_change_ids, so a re-append would only happen if the same change
    # id reached the helper twice.
    assert _append_delta_goal(_tree(), delta).all_goals() != _tree().all_goals()


def test_update_goal_patches_desired_state() -> None:
    tree = _tree()
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"goal_id": "g2"},
        desired_changes={"run_at": "明天下午3点"},
    )
    updated = _patch_delta_goal(tree, delta)
    g2 = next(goal for goal in updated.all_goals() if goal.goal_id == "g2")
    assert g2.temporal_constraint == {"run_at": "明天下午3点"}


def test_update_goal_rejects_invalid_goal_id_without_mutating_tree() -> None:
    tree = _tree()
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"goal_id": "missing-goal"},
        desired_changes={"run_at": "15:00"},
    )

    from greenbook_agent_core.task.manager import TaskManagerError

    with pytest.raises(TaskManagerError):
        _patch_delta_goal(tree, delta)
    assert next(goal for goal in tree.all_goals() if goal.goal_id == "g2").temporal_constraint == {}


def test_update_goal_blank_reference_is_normalized_for_resolver_grounding() -> None:
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"goal_id": "", "label": "   "},
        desired_changes={"run_at": "17:00"},
    )

    assert delta.target_reference == {}
    assert delta.needs_target_resolution is False


def test_update_goal_rejects_ambiguous_label_instead_of_first_match() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Root",
            children=[
                Goal(goal_id="g2", description="Publish the article"),
                Goal(goal_id="g3", description="Publish the article"),
            ],
        )
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Publish the article"},
        desired_changes={"run_at": "15:00"},
    )

    from greenbook_agent_core.task.manager import TaskManagerError

    with pytest.raises(TaskManagerError):
        _patch_delta_goal(tree, delta)
    assert all(goal.temporal_constraint == {} for goal in tree.all_goals())


def test_cancel_goal_removes_target_goal() -> None:
    tree = _tree()
    delta = TaskDelta(
        operation=TaskDeltaOperation.CANCEL_GOAL,
        target_reference={"goal_id": "g2"},
    )
    updated = _cancel_delta_goal(tree, delta)
    assert all(goal.goal_id != "g2" for goal in updated.all_goals())
    updated.validate_tree()


def test_cancel_root_goal_is_rejected() -> None:
    tree = _tree()
    delta = TaskDelta(
        operation=TaskDeltaOperation.CANCEL_GOAL,
        target_reference={"goal_id": "g1"},
    )
    from greenbook_agent_core.task.manager import TaskManagerError

    with pytest.raises(TaskManagerError):
        _cancel_delta_goal(tree, delta)


def test_business_cancel_schedule_never_becomes_a_task_cancellation() -> None:
    adapter = object.__new__(ConversationRuntimeAdapter)
    tree = _tree()
    delta = TaskDelta(
        # Simulate the unsafe interpretation an LLM might emit.  The
        # semantic business action must still win over the Task lifecycle
        # verb, so the draft/task anchor is retained until Java verifies the
        # Schedule cancellation.
        operation=TaskDeltaOperation.CANCEL_TASK,
        target_reference={"goal_id": "g2", "label": "Agent 总结"},
        desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
    )

    updated = adapter._apply_goal_delta(tree, delta)

    # The historical Goal is retained; only a verified Tool operation may
    # change the Java Schedule.  This prevents "cancel task" from being
    # mistaken for "schedule cancelled".
    assert any(goal.goal_id == "g2" for goal in updated.all_goals())
    action = next(
        goal for goal in updated.all_goals()
        if goal.semantic_operation == "CANCEL_SCHEDULE"
    )
    assert action.required_capabilities == ["CANCEL_SCHEDULE"]
    assert action.target == {"goal_id": "g2", "label": "Agent 总结"}
    updated.validate_tree()


def test_business_update_draft_appends_partial_mutation_action() -> None:
    adapter = object.__new__(ConversationRuntimeAdapter)
    updated = adapter._apply_goal_delta(
        _tree(),
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"goal_id": "g2"},
            desired_changes={
                "semantic_action": "UPDATE_DRAFT",
                "title": "更有吸引力的标题",
                "keep_schedule_unchanged": True,
            },
        ),
    )
    action = next(
        goal for goal in updated.all_goals()
        if goal.semantic_operation == "UPDATE_DRAFT"
    )
    assert action.required_capabilities == ["MANAGE_DRAFT"]
    assert action.constraints[0]["title"] == "更有吸引力的标题"
    assert action.constraints[0]["keep_schedule_unchanged"] is True


def test_business_action_binds_only_the_resolved_tasks_schedule() -> None:
    task = SimpleNamespace(
        task_id="task-java",
        resource_index=[
            SimpleNamespace(resource_id="draft-java", resource_kind="DRAFT"),
            SimpleNamespace(resource_id="schedule-java", resource_kind="SCHEDULE"),
        ],
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"goal_id": "g2", "label": "Java 那篇"},
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "明天下午五点",
        },
    )

    bound = _bind_semantic_action_resource(delta, task)
    action_tree = object.__new__(ConversationRuntimeAdapter)._apply_goal_delta(
        _tree(), bound,
    )
    action = next(
        goal for goal in action_tree.all_goals()
        if goal.semantic_operation == "UPDATE_SCHEDULE"
    )

    assert bound.desired_changes["schedule_id"] == "schedule-java"
    assert action.target == {
        "kind": "SCHEDULE",
        "resource_id": "schedule-java",
        "id": "schedule-java",
        "task_id": "task-java",
        "schedule_id": "schedule-java",
    }
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        action_tree,
        task_id="task-java",
    )
    step = next(item for item in plan.steps if item.goal_id == action.goal_id)
    assert step.constraints["schedule_id"] == "schedule-java"


def test_business_action_refuses_ambiguous_historical_schedule_binding() -> None:
    from greenbook_agent_api.services.conversation_runtime_adapter import TaskDeltaGroundingError

    task = SimpleNamespace(
        task_id="task-java",
        resource_index=[
            SimpleNamespace(resource_id="schedule-old", resource_kind="SCHEDULE"),
            SimpleNamespace(resource_id="schedule-current", resource_kind="SCHEDULE"),
        ],
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.CANCEL_GOAL,
        target_reference={"goal_id": "g2"},
        desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
    )

    with pytest.raises(TaskDeltaGroundingError):
        _bind_semantic_action_resource(delta, task)


@pytest.mark.parametrize(
    ("semantic_action", "field_name"),
    [
        ("UPDATE_SCHEDULE", "schedule_id"),
        ("CANCEL_SCHEDULE", "schedule_id"),
        ("GET_SCHEDULE", "schedule_id"),
    ],
)
def test_business_action_repairs_unique_resource_owner_link(
    semantic_action: str,
    field_name: str,
) -> None:
    """A typed Schedule may be labelled by a sibling Objective.

    The resource binding is authoritative when it has one persisted owner;
    the label Objective must not make a correctly grounded schedule look
    unowned.  This exercises three business actions without recency fallback.
    """

    task = SimpleNamespace(
        task_id="task-java",
        objectives=[
            SimpleNamespace(
                objective_id="objective-draft",
                related_resource_ids=["draft-java"],
            ),
            SimpleNamespace(
                objective_id="objective-schedule",
                related_resource_ids=["schedule-java"],
            ),
            # A completed cross-turn mutation may also record the same
            # Schedule as its output.  That lineage reference must not replace
            # the typed ResourceBinding owner above.
            SimpleNamespace(
                objective_id="objective-update-schedule",
                related_resource_ids=["schedule-java"],
                constraints={"target_objective_id": "objective-schedule"},
            ),
        ],
        resource_index=[
            SimpleNamespace(
                resource_id="draft-java",
                resource_kind="DRAFT",
                objective_id="objective-draft",
            ),
            SimpleNamespace(
                resource_id="schedule-java",
                resource_kind="SCHEDULE",
                objective_id="objective-schedule",
            ),
        ],
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={
            "kind": "SCHEDULE",
            "resource_id": "schedule-java",
            "schedule_id": "schedule-java",
            "objective_id": "objective-draft",
        },
        desired_changes={
            "semantic_action": semantic_action,
            "objective_id": "objective-draft",
        },
    )

    bound = _bind_semantic_action_resource(delta, task)

    assert bound.desired_changes[field_name] == "schedule-java"
    assert bound.desired_changes["objective_id"] == "objective-schedule"
    assert bound.desired_changes["target_objective_id"] == "objective-schedule"
    assert bound.target_reference["objective_id"] == "objective-schedule"
    assert bound.target_reference["target_objective_id"] == "objective-schedule"


def test_business_action_rejects_resource_without_task_owner() -> None:
    from greenbook_agent_api.services.conversation_runtime_adapter import TaskDeltaGroundingError

    task = SimpleNamespace(
        task_id="task-java",
        objectives=[SimpleNamespace(objective_id="objective-draft", related_resource_ids=["draft-java"])],
        resource_index=[
            SimpleNamespace(
                resource_id="draft-java",
                resource_kind="DRAFT",
                objective_id="objective-draft",
            )
        ],
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"kind": "SCHEDULE", "schedule_id": "schedule-missing"},
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "objective_id": "objective-draft",
        },
    )

    with pytest.raises(TaskDeltaGroundingError):
        _bind_semantic_action_resource(delta, task)


def test_business_action_rejects_resource_with_multiple_persisted_owners() -> None:
    from greenbook_agent_api.services.conversation_runtime_adapter import TaskDeltaGroundingError

    task = SimpleNamespace(
        task_id="task-java",
        objectives=[],
        resource_index=[
            SimpleNamespace(
                resource_id="schedule-java",
                resource_kind="SCHEDULE",
                objective_id="objective-one",
            ),
            SimpleNamespace(
                resource_id="schedule-java",
                resource_kind="SCHEDULE",
                objective_id="objective-two",
            ),
        ],
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={
            "kind": "SCHEDULE",
            "schedule_id": "schedule-java",
            "objective_id": "objective-draft",
        },
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "objective_id": "objective-draft",
        },
    )

    with pytest.raises(TaskDeltaGroundingError):
        _bind_semantic_action_resource(delta, task)


def test_task_scoped_context_never_inherits_sibling_active_resources() -> None:
    task = SimpleNamespace(
        task_id="task-java",
        resource_index=[
            SimpleNamespace(resource_id="draft-java", resource_kind="DRAFT"),
            SimpleNamespace(resource_id="schedule-java", resource_kind="SCHEDULE"),
        ],
    )
    session = SessionContext(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        active_task_id="task-agent",
        active_draft_id="draft-agent",
        active_schedule_id="schedule-agent",
        recent_entities=[
            RecentEntity(ref="draft:draft-agent", kind="DRAFT", entity_id="draft-agent"),
            RecentEntity(ref="schedule:schedule-agent", kind="SCHEDULE", entity_id="schedule-agent"),
        ],
    )
    context = ContextSnapshot(
        active_task_id="task-agent",
        active_draft_id="draft-agent",
        active_schedule_id="schedule-agent",
        active_tasks=[
            {"task_id": "task-java"}, {"task_id": "task-agent"},
        ],
        artifacts=[
            {"task_id": "task-java", "resource_id": "draft-java"},
            {"task_id": "task-agent", "resource_id": "draft-agent"},
        ],
        execution_states=[
            {"task_id": "task-java", "execution_id": "e-java"},
            {"task_id": "task-agent", "execution_id": "e-agent"},
        ],
        available_resources=[
            {"task_id": "task-java", "resource_id": "draft-java"},
            {"task_id": "task-agent", "resource_id": "draft-agent"},
        ],
        target_candidates=[
            {"kind": "TASK", "id": "task-java"},
            {"kind": "TASK", "id": "task-agent"},
            {"kind": "DRAFT", "task_id": "task-agent", "id": "draft-agent"},
        ],
    )

    scoped_session = _session_scoped_to_task(session, task)
    scoped_context = _context_scoped_to_task(context, task)

    assert scoped_session is not session
    assert scoped_session.active_task_id == "task-java"
    assert scoped_session.active_draft_id == "draft-java"
    assert scoped_session.active_schedule_id == "schedule-java"
    assert all(entity.entity_id != "draft-agent" for entity in scoped_session.recent_entities)
    assert scoped_context.active_task_id == "task-java"
    assert scoped_context.active_draft_id == "draft-java"
    assert scoped_context.active_schedule_id == "schedule-java"
    assert scoped_context.active_tasks == [{"task_id": "task-java"}]
    assert scoped_context.available_resources == [{"task_id": "task-java", "resource_id": "draft-java"}]
    assert scoped_context.target_candidates == [{"kind": "TASK", "id": "task-java"}]


# ── Target grounding through TaskManager (real TaskProvider) ─────────────


@pytest.fixture
def manager() -> TaskManager:
    from greenbook_agent_core.task import InMemoryTaskRepository

    return TaskManager(InMemoryTaskRepository())


_CONV = "00000000-0000-0000-0000-000000000001"
_USER = "00000000-0000-0000-0000-000000000002"
_TENANT = "00000000-0000-0000-0000-000000000003"


async def _task(manager: TaskManager, goal: str, tree: GoalTree) -> Any:
    return await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal=goal,
        goal_tree=tree,
    )


@pytest.mark.asyncio
async def test_cancel_task_via_manager(manager: TaskManager) -> None:
    task = await _task(manager, "Redis 帖子表现分析", _tree())
    cancelled = await manager.cancel_task(task.task_id, reason="user cancelled")
    assert cancelled.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_schedule_cancel_semantic_action_cannot_call_cancel_task() -> None:
    """The runtime must not trust a lifecycle verb over business semantics."""

    tree = _tree()
    task = SimpleNamespace(
        task_id="task-agent",
        status=TaskStatus.READY,
        goal="Agent article",
        version=1,
        created_at="2026-08-15T00:00:00Z",
        updated_at="2026-08-15T00:00:00Z",
        goal_tree_snapshot=tree.model_dump(mode="json"),
        resource_index=[
            SimpleNamespace(resource_id="schedule-agent", resource_kind="SCHEDULE"),
        ],
    )

    class _Manager:
        cancel_calls = 0

        async def get_resolvable_tasks(self, **_: Any) -> list[Any]:
            return [task]

        async def get_required(self, *_: Any, **__: Any) -> Any:
            return task

        async def bind_goal_tree(self, _task_id: str, updated: GoalTree) -> Any:
            task.goal_tree_snapshot = updated.model_dump(mode="json")
            task.version += 1
            return task

        async def cancel_task(self, *_: Any, **__: Any) -> Any:
            self.cancel_calls += 1
            raise AssertionError("CANCEL_SCHEDULE must not cancel the Task")

    manager = _Manager()
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()

    async def fake_loop(**kwargs: Any) -> Any:
        action_tree = kwargs["goal_tree"]
        assert any(
            goal.semantic_operation == "CANCEL_SCHEDULE"
            for goal in action_tree.all_goals()
        )
        return SimpleNamespace(
            status="RUNNING",
            run_id="run-cancel-schedule",
            trace_id="trace-cancel-schedule",
            content="",
            error_code="",
            error_message="",
            partial_results={},
        )

    adapter._run_agent_loop = fake_loop
    session = SimpleNamespace(
        active_task_id="",
        conversation_focus_task_id="",
        record_conversation_focus=lambda *_args, **_kwargs: None,
    )
    command = Command(type=CommandType.MODIFY, goal="cancel schedule")

    result = await adapter._run_task_deltas(
        deltas=[
            TaskDelta(
                operation=TaskDeltaOperation.CANCEL_TASK,
                target_reference={"kind": "SCHEDULE", "id": "schedule-agent"},
                desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
            )
        ],
        command=command,
        context=SimpleNamespace(),
        request_session=session,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-cancel-schedule",
        trace_id="trace-cancel-schedule",
        llm=None,
        model="test",
    )

    assert manager.cancel_calls == 0
    assert task.status == TaskStatus.READY
    assert result.status == "RUNNING"


@pytest.mark.asyncio
async def test_same_task_multi_mutation_resumes_latest_goal_tree(
    manager: TaskManager,
) -> None:
    """All mutations in one turn must survive the Task snapshot hand-off.

    ``_apply_goal_mutation`` persists each delta with CAS.  When two business
    actions target the same Task, the resume must receive the latest returned
    Task, not the first object kept in the affected-task map.
    """
    task = await _task(manager, "Two scheduled articles", GoalTree(
        root=Goal(
            goal_id="root-two-schedules",
            description="Two scheduled articles",
            required_capabilities=["GENERATE_CONTENT"],
        ),
        source="TASK_DELTA",
    ))
    task.resource_index = [
        TaskResourceRef(resource_id="schedule-a", resource_kind="SCHEDULE"),
        TaskResourceRef(resource_id="schedule-b", resource_kind="SCHEDULE"),
    ]
    await manager._repository.update(task)

    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()

    async def resolve_target(*_args: Any, **_kwargs: Any) -> Any:
        return await manager.get_required(task.task_id)

    adapter._resolve_delta_target = resolve_target
    seen_trees: list[GoalTree] = []

    async def fake_loop(**kwargs: Any) -> Any:
        seen_trees.append(kwargs["goal_tree"])
        return SimpleNamespace(
            success=True,
            status="COMPLETED",
            run_id="run-multi-mutation",
            trace_id="trace-multi-mutation",
            content="",
            summary="",
            error_code="",
            error_message="",
            partial_results={},
        )

    adapter._run_agent_loop = fake_loop
    command = Command(type=CommandType.MODIFY, goal="Update both schedules")
    result = await adapter._run_task_deltas(
        deltas=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "SCHEDULE", "id": "schedule-a"},
                desired_changes={
                    "semantic_action": "UPDATE_SCHEDULE",
                    "run_at": "2026-08-22T07:00:00Z",
                },
            ),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "SCHEDULE", "id": "schedule-b"},
                desired_changes={
                    "semantic_action": "UPDATE_SCHEDULE",
                    "run_at": "2026-08-22T07:00:00Z",
                },
            ),
        ],
        command=command,
        context=ContextSnapshot(),
        request_session=SessionContext(
            conversation_id="c1",
            user_id="u1",
            tenant_id="t1",
        ),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-multi-mutation",
        trace_id="trace-multi-mutation",
        llm=None,
        model="test",
    )

    assert result.status == "COMPLETED"
    assert len(seen_trees) == 1
    actions = [
        goal
        for goal in seen_trees[0].all_goals()
        if goal.semantic_operation == "UPDATE_SCHEDULE"
    ]
    assert {goal.target["resource_id"] for goal in actions} == {
        "schedule-a",
        "schedule-b",
    }


@pytest.mark.asyncio
async def test_add_goal_through_bind_persists_snapshot(manager: TaskManager) -> None:
    task = await _task(manager, "Agent 总结", _tree())
    delta = TaskDelta(
        operation=TaskDeltaOperation.ADD_GOAL,
        desired_changes={"description": "追加文章目标"},
    )
    tree = GoalTree.model_validate(task.goal_tree_snapshot)
    updated_tree = _append_delta_goal(tree, delta)
    refreshed = await manager.bind_goal_tree(task.task_id, updated_tree)
    assert refreshed.goal_tree_version == task.goal_tree_version + 1
    snapshot = GoalTree.model_validate(refreshed.goal_tree_snapshot)
    assert any(goal.description == "追加文章目标" for goal in snapshot.all_goals())


@pytest.mark.asyncio
async def test_create_task_deltas_keep_their_own_constraints_and_command_scope(
    manager: TaskManager,
) -> None:
    """Independent TaskDelta outcomes cannot inherit one turn's facts.

    This regression protects the three-article case: the aggregate command is
    intentionally populated with a conflicting time/topic sentinel. Each new
    Objective must retain only the facts declared by its TaskDelta and must not
    create a GoalTree/TaskGoal projection.
    """

    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    command = Command(
        type=CommandType.CREATE,
        goal="Create Java and Agent articles",
        constraints={
            "run_at": "2099-01-01T00:00:00+08:00",
            "turn_only": "must not leak into either task",
        },
        parameters={"turn_topic": "must not leak"},
    )
    java_delta = TaskDelta(
        operation=TaskDeltaOperation.CREATE_TASK,
        desired_changes={
            "description": "Java interview article",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "constraints": {
                "topic": "Java",
                "run_at": "2026-08-16T09:00:00+08:00",
                "style": "practical",
            },
        },
    )
    agent_delta = TaskDelta(
        operation=TaskDeltaOperation.CREATE_TASK,
        desired_changes={
            "description": "Agent development article",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "constraints": {
                "topic": "Agent",
                "run_at": "2026-08-16T14:00:00+08:00",
                "style": "technical",
            },
        },
    )

    java_task = await adapter._delta_create_task(java_delta, command, "c1", "u1", "t1")
    agent_task = await adapter._delta_create_task(agent_delta, command, "c1", "u1", "t1")
    assert java_task.goal_tree_snapshot == {}
    assert agent_task.goal_tree_snapshot == {}
    assert java_task.goals == []
    assert agent_task.goals == []
    java_objective = java_task.objectives[0]
    agent_objective = agent_task.objectives[0]
    assert java_objective.description == "Java interview article"
    assert agent_objective.description == "Agent development article"
    assert java_objective.constraints["topic"] == "Java"
    assert agent_objective.constraints["topic"] == "Agent"
    assert java_objective.constraints["run_at"] == "2026-08-16T09:00:00+08:00"
    assert agent_objective.constraints["run_at"] == "2026-08-16T14:00:00+08:00"
    assert "turn_only" not in java_objective.constraints
    assert "turn_only" not in agent_objective.constraints


# ── Command carries deltas end to end (interpreter contract) ─────────────


@pytest.mark.asyncio
async def test_update_goal_id_resolves_owner_task_not_active_task(manager: TaskManager) -> None:
    first = await _task(manager, "First task", _tree())
    second_tree = _tree()
    second_tree.root_goal.goal_id = "second-root"
    second_tree.root_goal.children[0].parent_goal = "second-root"
    second_tree.task_nodes[0].goal_id = "g2"
    second_tree.root_goal.children.append(
        Goal(goal_id="second-goal", description="Second task child")
    )
    await _task(manager, "Second task", second_tree)

    # Avoid unrelated production wiring in this resolver-focused contract.
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    target = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"goal_id": "second-goal"},
        ),
        type("Session", (), {"active_task_id": first.task_id})(),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
    )
    assert target is not None
    assert target.task_id != first.task_id
    assert any(goal.goal_id == "second-goal" for goal in GoalTree.model_validate(target.goal_tree_snapshot).all_goals())


@pytest.mark.asyncio
async def test_unresolved_update_goal_fails_before_task_apply() -> None:
    class _NoApplyManager:
        def __init__(self) -> None:
            self.calls = 0

        async def get_resolvable_tasks(self, **_: Any) -> list[Any]:
            self.calls += 1
            return []

    manager = _NoApplyManager()
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    command = Command(type=CommandType.MODIFY, goal="修改发布时间")
    result = await adapter._run_task_deltas(
        deltas=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"goal_id": ""},
            desired_changes={"run_at": "17:00"},
        )],
        command=command,
        context=type("Context", (), {"target_candidates": []})(),
        request_session=type("Session", (), {"active_task_id": "active-task"})(),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-invalid-target",
        trace_id="trace-invalid-target",
        llm=None,
        model="test",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "MUTATION_TARGET_REQUIRED"
    assert "UPDATE_GOAL cannot find target" not in result.error_message
    # Contextual grounding performs one read-only candidate lookup; no Task
    # mutation is attempted when the lookup finds no authoritative target.
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_create_delta_without_capabilities_asks_user_instead_of_empty_goal() -> None:
    """Real-chain regression: a CREATE TaskDelta whose desired_changes declares
    no capability used to build an empty GoalTree (required_capabilities=[]);
    AgentLoop then flailed — repeated read tools under a GENERATE_CONTENT label
    and a silent COMPLETED with no user-visible result.  An empty delta must
    fail closed to a clarification the user can see."""
    class _NoApplyManager:
        def __init__(self) -> None:
            self.calls = 0

        async def get_active_tasks(self, **_: Any) -> list[Any]:
            self.calls += 1
            return []

        async def create_task(self, **_: Any) -> Any:
            self.calls += 1
            raise AssertionError("empty CREATE delta must not create a task")

    manager = _NoApplyManager()
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    command = Command(type=CommandType.CREATE, goal="写一篇 Java 学习帖子并发布")
    result = await adapter._run_task_deltas(
        deltas=[TaskDelta(
            operation=TaskDeltaOperation.CREATE_TASK,
            target_reference={"goal_id": ""},
            desired_changes={"description": "Search posts about Java, summarize, write and publish"},
        )],
        command=command,
        context=type("Context", (), {"target_candidates": []})(),
        request_session=type("Session", (), {"active_task_id": ""})(),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        run_id="run-empty-delta",
        trace_id="trace-empty-delta",
        llm=None,
        model="test",
    )

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "DELTA_REQUIRES_CAPABILITIES"
    assert manager.calls == 0


def test_command_roundtrips_task_changes() -> None:
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java"},
        desired_changes={"run_at": "明天下午3点"},
    )
    command = Command(
        type=CommandType.MODIFY,
        goal="调整",
        task_changes=[delta],
    )
    assert command.task_changes[0].target_reference["label"] == "Java"
    payload = command.model_dump(mode="json")
    restored = Command.model_validate(payload)
    assert restored.task_changes[0].operation == TaskDeltaOperation.UPDATE_GOAL


def test_delta_is_meaningful_rejects_echoed_empty_changes() -> None:
    """Real-chain regression: the model repeatedly echoed a full independent
    request as an empty CREATE_TASK delta (no required_capabilities), and the
    run bounced to DELTA_REQUIRES_CAPABILITIES forever — the user was stuck
    re-typing a complete request.  Meaningless changes must fall back to the
    fresh-request path."""
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _delta_is_meaningful,
    )

    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.CREATE_TASK,
        desired_changes={"description": "Search posts about Java, summarize, write and publish"},
    )) is False
    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.CREATE_TASK,
        desired_changes={"required_capabilities": ["SEARCH_COMMUNITY", "GENERATE_CONTENT"]},
    )) is True
    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        desired_changes={"run_at": "17:00"},
    )) is True
    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={},
        desired_changes={},
    )) is False
    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.CANCEL_TASK,
        target_reference={"task_id": "t1"},
    )) is True
    assert _delta_is_meaningful(TaskDelta(
        operation=TaskDeltaOperation.NO_CHANGE,
    )) is False


def test_command_understanding_lists_independent_tasks() -> None:
    """The UNDERSTANDING activity must surface every independently scheduled
    task (description + publish time + search requirement) so the user can
    verify the agent's understanding before it keeps executing."""
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _command_understanding,
    )

    command = Command(
        type=CommandType.CREATE,
        goal="安排三个任务",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.CREATE_TASK,
                desired_changes={
                    "description": "Java 面试 10 问",
                    "required_capabilities": ["SEARCH_COMMUNITY", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    "constraints": {"run_at": "2026-08-15T09:00:00+08:00"},
                },
            ),
            TaskDelta(
                operation=TaskDeltaOperation.CREATE_TASK,
                desired_changes={
                    "description": "直接写轻松的八股帖",
                    "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    "constraints": {"run_at": "2026-08-16T20:00:00+08:00"},
                },
            ),
        ],
    )
    understanding = _command_understanding(command)
    assert understanding["summary"] == "我理解你安排了 2 项内容"
    assert understanding["tasks"][0]["description"] == "Java 面试 10 问"
    assert understanding["tasks"][0]["requires_search"] is True
    assert understanding["tasks"][0]["publish_at"] == "2026-08-15T09:00:00+08:00"
    assert understanding["tasks"][1]["requires_search"] is False

    single = Command(type=CommandType.QUERY, goal="帮我总结一下"),
    if isinstance(single, tuple):
        single = single[0]
    single_understanding = _command_understanding(single)
    assert len(single_understanding["tasks"]) == 1
    assert single_understanding["tasks"][0]["requires_search"] is False
