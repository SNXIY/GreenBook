"""Cross-turn ResourceBinding/Object identity invariants.

These tests stop at the canonical Objective/mutation boundary.  No Java or
durable WRITE is invoked: the assertion is that the exact resolved resource
would be the only resource handed to the existing ActionLoop owner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenbook_agent_api.services.action_loop_executor import ActionLoopExecutor
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.actionloop.loop import ActionLoop
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolutionStatus, TargetResolver
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import InMemoryTaskRepository
from greenbook_agent_core.task.manager import TaskManager
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    TaskResourceRef,
    TaskRevision,
    TaskRevisionType,
    TaskExecutionRef,
    TaskStatus,
)


CONVERSATION = "cross-turn-conversation"
USER = "cross-turn-user"
TENANT = "cross-turn-tenant"


async def _persist(manager: TaskManager, task):
    return await manager.repository.update(task, expected_version=task.version)


async def _task(manager: TaskManager, objectives, resources, *, status=TaskStatus.COMPLETED):
    task = await manager.create_task(
        conversation_id=CONVERSATION,
        user_id=USER,
        tenant_id=TENANT,
        goal="historical article work",
    )
    task.status = status
    task.objectives = list(objectives)
    task.resource_index = list(resources)
    return await _persist(manager, task)


def _objective(task_id: str, objective_id: str, title: str, resources, *, status="COMPLETED"):
    return Objective(
        task_id=task_id,
        objective_id=objective_id,
        description=title,
        intent=title,
        status=ObjectiveStatus(status),
        expected_resource_kind="DRAFT",
        required_capabilities=["GENERATE_CONTENT"],
        result_requirement="RESOURCE_MUTATION",
        related_resource_ids=list(resources),
    )


def _resource(resource_id: str, kind: str, objective_id: str, *, title: str = ""):
    return TaskResourceRef(
        resource_id=resource_id,
        resource_kind=kind,
        objective_id=objective_id,
        title=title or None,
    )


def _coordinator() -> ConversationRuntimeAdapter:
    # TurnCoordinator uses the same TargetResolver; the adapter helper is
    # exercised here because it is the compatibility boundary used by direct
    # conversation-runtime callers.
    return object.__new__(ConversationRuntimeAdapter)


def _turn_coordinator():
    from greenbook_agent_api.services.turn_coordinator import TurnCoordinator

    return TurnCoordinator(target_resolver=TargetResolver())


def _assembled(task, *, active_task_id=""):
    return SimpleNamespace(
        snapshot=SimpleNamespace(
            active_tasks=[task.model_dump(mode="json")],
            active_task_id=active_task_id,
        )
    )


async def _resolve(manager: TaskManager, task, changes):
    command = Command(
        type=CommandType.MODIFY,
        goal="cross-turn mutation",
        task_changes=changes,
    )
    resolution = _turn_coordinator()._resolve_delta_objective_target(
        command,
        _assembled(task),
    )
    return command, resolution


async def _compile_mutations(manager: TaskManager, task, changes):
    command, resolution = await _resolve(manager, task, changes)
    assert resolution is not None
    assert resolution.status == TargetResolutionStatus.RESOLVED
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=manager,
        llm=None,
    )
    refreshed = await executor._ensure_mutation_objectives(task, command)
    return command, refreshed


@pytest.mark.asyncio
async def test_case_a_new_schedule_objective_keeps_java_draft_lineage() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    old = _objective("", "objective-java-create", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [old],
        [_resource("draft-A", "DRAFT", old.objective_id, title="Java article")],
    )
    old.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "CREATE_SCHEDULE",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )

    command, updated = await _compile_mutations(manager, task, [change])
    new = [item for item in updated.objectives if item.objective_id != old.objective_id]

    assert len(new) == 1
    assert old.objective_id in {item.objective_id for item in updated.objectives}
    assert next(item for item in updated.objectives if item.objective_id == old.objective_id).status == ObjectiveStatus.COMPLETED
    assert new[0].related_resource_ids == ["draft-A"]
    assert command.task_changes[0].desired_changes["objective_id"] == new[0].objective_id
    assert command.task_changes[0].desired_changes["draft_id"] == "draft-A"
    assert command.task_changes[0].target_reference["resource_id"] == "draft-A"


@pytest.mark.asyncio
async def test_case_b_two_natural_targets_create_two_new_objectives_without_cross_wire() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    java = _objective("", "objective-java", "Java article", ["draft-A"])
    agent = _objective("", "objective-agent", "Agent article", ["draft-B"])
    task = await _task(
        manager,
        [java, agent],
        [
            _resource("draft-A", "DRAFT", java.objective_id, title="Java article"),
            _resource("draft-B", "DRAFT", agent.objective_id, title="Agent article"),
        ],
    )
    for item in task.objectives:
        item.task_id = task.task_id
    task = await _persist(manager, task)
    changes = [
        TaskDelta(
            change_id="publish-java",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={"semantic_action": "PUBLISH_NOW"},
        ),
        TaskDelta(
            change_id="schedule-agent",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Agent article"},
            desired_changes={
                "semantic_action": "CREATE_SCHEDULE",
                "run_at": "2026-08-22T07:05:00Z",
            },
        ),
    ]

    command, updated = await _compile_mutations(manager, task, changes)
    created = [
        item for item in updated.objectives
        if item.objective_id not in {java.objective_id, agent.objective_id}
    ]
    by_action = {
        str(item.intent): item
        for item in created
    }

    assert len(created) == 2
    assert by_action["PUBLISH_NOW"].related_resource_ids == ["draft-A"]
    assert by_action["CREATE_SCHEDULE"].related_resource_ids == ["draft-B"]
    assert command.task_changes[0].desired_changes["draft_id"] == "draft-A"
    assert command.task_changes[1].desired_changes["draft_id"] == "draft-B"
    assert command.task_changes[0].target_reference["resource_id"] == "draft-A"
    assert command.task_changes[1].target_reference["resource_id"] == "draft-B"
    assert command.task_changes[0].desired_changes["objective_id"] != command.task_changes[1].desired_changes["objective_id"]


@pytest.mark.asyncio
async def test_case_c_cancel_agent_schedule_preserves_agent_draft_and_java() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    java = _objective("", "objective-java", "Java article", ["draft-A", "schedule-A"])
    agent = _objective("", "objective-agent", "Agent article", ["draft-B", "schedule-B"])
    task = await _task(
        manager,
        [java, agent],
        [
            _resource("draft-A", "DRAFT", java.objective_id),
            _resource("schedule-A", "SCHEDULE", java.objective_id),
            _resource("draft-B", "DRAFT", agent.objective_id),
            _resource("schedule-B", "SCHEDULE", agent.objective_id),
        ],
    )
    for item in task.objectives:
        item.task_id = task.task_id
    task = await _persist(manager, task)

    change = TaskDelta(
        change_id="cancel-agent-schedule",
        operation=TaskDeltaOperation.CANCEL_GOAL,
        target_reference={"label": "Agent article"},
        desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
    )
    _command, updated = await _compile_mutations(manager, task, [change])
    cancel = [
        item for item in updated.objectives
        if item.intent == "CANCEL_SCHEDULE"
    ][0]

    assert cancel.related_resource_ids == ["schedule-B"]
    assert {item.resource_id for item in updated.resource_index} == {
        "draft-A", "schedule-A", "draft-B", "schedule-B"
    }
    assert next(item for item in updated.objectives if item.objective_id == java.objective_id).related_resource_ids == ["draft-A", "schedule-A"]


@pytest.mark.asyncio
async def test_case_d_duplicate_java_reference_is_ambiguous_without_active_fallback() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    first = _objective("", "objective-java-1", "Java article", ["draft-A"])
    second = _objective("", "objective-java-2", "Java article", ["draft-B"])
    task = await _task(
        manager,
        [first, second],
        [
            _resource("draft-A", "DRAFT", first.objective_id),
            _resource("draft-B", "DRAFT", second.objective_id),
        ],
    )
    for item in task.objectives:
        item.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={"semantic_action": "PUBLISH_NOW"},
    )

    command = Command(
        type=CommandType.MODIFY,
        goal="cross-turn mutation",
        task_changes=[change],
    )
    resolution = _turn_coordinator()._resolve_delta_objective_target(
        command,
        _assembled(task, active_task_id=task.task_id),
    )

    assert resolution is not None
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert command.task_changes[0].desired_changes.get("objective_id") is None


@pytest.mark.asyncio
async def test_case_e_schedule_cancel_publish_preserves_one_draft_lineage() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-java", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id)],
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)

    async def compile_one(task, action, change_id, **extra):
        change = TaskDelta(
            change_id=change_id,
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={"semantic_action": action, **extra},
        )
        _command, current = await _compile_mutations(manager, task, [change])
        return current, next(item for item in current.objectives if item.objective_id not in {original.objective_id, *seen})

    seen: set[str] = set()
    task, schedule_objective = await compile_one(
        task, "CREATE_SCHEDULE", "schedule-java", run_at="2026-08-22T07:05:00Z"
    )
    seen.add(schedule_objective.objective_id)
    schedule_objective.related_resource_ids.append("schedule-A")
    task.resource_index.append(_resource("schedule-A", "SCHEDULE", schedule_objective.objective_id))
    task = await _persist(manager, task)

    task, update_objective = await compile_one(
        task, "UPDATE_SCHEDULE", "update-java", run_at="2026-08-22T08:00:00Z"
    )
    seen.add(update_objective.objective_id)
    assert update_objective.related_resource_ids == ["schedule-A"]
    task = await _persist(manager, task)

    task, cancel_objective = await compile_one(
        task, "CANCEL_SCHEDULE", "cancel-java"
    )
    seen.add(cancel_objective.objective_id)
    assert cancel_objective.related_resource_ids == ["schedule-A"]
    task = await _persist(manager, task)

    task, publish_objective = await compile_one(
        task, "PUBLISH_NOW", "publish-java"
    )
    assert publish_objective.related_resource_ids == ["draft-A"]
    assert {
        str(item.resource_id) for item in task.resource_index
    } == {"draft-A", "schedule-A"}
    assert len({item.objective_id for item in task.objectives}) == len(task.objectives)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
async def test_case_f_terminal_task_remains_resolvable_by_resource_identity(
    terminal_status: TaskStatus,
) -> None:
    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-terminal", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id)],
        status=terminal_status,
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    assert task in await manager.get_resolvable_tasks(
        CONVERSATION,
        user_id=USER,
        tenant_id=TENANT,
    )

    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "Java article v2"},
    )
    _command, updated = await _compile_mutations(manager, task, [change])
    new = [item for item in updated.objectives if item.objective_id != original.objective_id][0]

    assert updated.status == terminal_status
    assert new.related_resource_ids == ["draft-A"]
    assert next(item for item in updated.objectives if item.objective_id == original.objective_id).status == ObjectiveStatus.COMPLETED


@pytest.mark.asyncio
async def test_schedule_publish_capability_alias_creates_new_runtime_objective() -> None:
    """Capability spelling must not finish against a terminal predecessor."""

    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-terminal-schedule", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id, title="Java article")],
        status=TaskStatus.COMPLETED,
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "SCHEDULE_PUBLISH",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )

    command, updated = await _compile_mutations(manager, task, [change])
    created = [
        item for item in updated.objectives
        if item.objective_id != original.objective_id
    ]

    assert len(created) == 1
    assert created[0].intent == "CREATE_SCHEDULE"
    assert created[0].status == ObjectiveStatus.PENDING
    assert created[0].related_resource_ids == ["draft-A"]
    assert command.task_changes[0].desired_changes["semantic_action"] == "CREATE_SCHEDULE"
    assert ActionLoop._current_objective(updated).objective_id == created[0].objective_id
    assert next(
        item for item in updated.objectives
        if item.objective_id == original.objective_id
    ).status == ObjectiveStatus.COMPLETED


@pytest.mark.asyncio
async def test_mutation_objective_uses_resolved_canonical_run_at() -> None:
    """A mutation Objective must consume ResolvedSemanticState time."""

    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-terminal-time", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id, title="Java article")],
        status=TaskStatus.COMPLETED,
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "CREATE_SCHEDULE",
            "run_at": "五分钟后",
        },
    )
    command, resolution = await _resolve(manager, task, [change])
    assert resolution is not None
    command.resolved_semantics = SimpleNamespace(
        items=[SimpleNamespace(
            operation="CREATE_SCHEDULE",
            run_at="2026-08-22T07:05:00Z",
            temporal_kind="FUTURE",
            temporal_resolved=True,
            constraints={"timezone": "Asia/Shanghai"},
            target_reference={"resource_id": "draft-A", "objective_id": original.objective_id},
        )],
        run_at="2026-08-22T07:05:00Z",
    )
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=manager,
        llm=None,
    )
    updated = await executor._ensure_mutation_objectives(task, command)
    created = [item for item in updated.objectives if item.objective_id != original.objective_id]

    assert len(created) == 1
    assert created[0].constraints["run_at"] == "2026-08-22T07:05:00Z"
    assert command.task_changes[0].desired_changes["run_at"] == "2026-08-22T07:05:00Z"


@pytest.mark.asyncio
async def test_publish_mutation_preserves_resolved_immediate_publication_intent() -> None:
    """Immediate publish qualification must use the resolved item facts."""

    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-publish", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id, title="Java article")],
        status=TaskStatus.COMPLETED,
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={"semantic_action": "PUBLISH_NOW"},
    )
    command, resolution = await _resolve(manager, task, [change])
    assert resolution is not None
    command.resolved_semantics = SimpleNamespace(
        items=[SimpleNamespace(
            operation="PUBLISH_NOW",
            publication_intent="IMMEDIATE_PUBLISH",
            temporal_kind="NOW",
            temporal_resolved=True,
            constraints={
                "publication_intent": "IMMEDIATE_PUBLISH",
                "temporal_kind": "NOW",
                "temporal_resolved": True,
            },
            target_reference={
                "resource_id": "draft-A",
                "objective_id": original.objective_id,
            },
        )],
        publication_intent="IMMEDIATE_PUBLISH",
        temporal_kind="NOW",
        temporal_resolved=True,
    )
    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        task_manager=manager,
        llm=None,
    )

    updated = await executor._ensure_mutation_objectives(task, command)
    created = [item for item in updated.objectives if item.objective_id != original.objective_id]

    assert len(created) == 1
    assert created[0].constraints["publication_intent"] == "IMMEDIATE_PUBLISH"
    assert created[0].constraints["temporal_kind"] == "NOW"
    assert created[0].constraints["temporal_resolved"] is True
    assert command.task_changes[0].desired_changes["publication_intent"] == "IMMEDIATE_PUBLISH"


@pytest.mark.asyncio
async def test_legacy_goal_rebind_does_not_erase_objective_first_mutation() -> None:
    """A compatibility GoalTree projection cannot delete a new outcome."""

    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-java", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id)],
    )
    original.task_id = task.task_id
    mutation = Objective(
        task_id=task.task_id,
        objective_id="mutation-schedule",
        description="Schedule Java article",
        intent="CREATE_SCHEDULE",
        required_capabilities=["SCHEDULE_PUBLISH"],
        result_requirement="RESOURCE_MUTATION",
        related_resource_ids=["draft-A"],
        constraints={"target_objective_id": original.objective_id},
    )
    task.objectives.append(mutation)
    task = await _persist(manager, task)

    rebound = await manager.bind_goal_tree(
        task.task_id,
        GoalTree(root=Goal(goal_id=original.objective_id, description="Java article")),
    )

    assert {item.objective_id for item in rebound.objectives} == {
        original.objective_id,
        mutation.objective_id,
    }
    assert next(
        item for item in rebound.objectives
        if item.objective_id == mutation.objective_id
    ).related_resource_ids == ["draft-A"]


def test_mutation_identity_separates_new_objective_from_old_retry() -> None:
    assert ActionLoop._mutation_key("task", "UPDATE_SCHEDULE", "schedule-A", "old") != ActionLoop._mutation_key(
        "task", "UPDATE_SCHEDULE", "schedule-A", "new"
    )
    task = SimpleNamespace(
        execution_refs=[SimpleNamespace(execution_id="execution-old", status="COMPLETED")],
        resource_index=[],
        objectives=[],
        revisions=[
            TaskRevision(
                task_id="task",
                type=TaskRevisionType.MODIFY_GOAL,
                payload={
                    "kind": "ACTION_LOOP_MUTATION_SUBMISSION",
                    "action": "UPDATE_SCHEDULE",
                    "resource_id": "schedule-A",
                    "objective_id": "old",
                    "execution_id": "execution-old",
                },
            )
        ],
    )
    old_change = SimpleNamespace(
        desired_changes={"semantic_action": "UPDATE_SCHEDULE", "objective_id": "old"},
        target_reference={"id": "schedule-A", "objective_id": "old"},
    )
    new_change = SimpleNamespace(
        desired_changes={"semantic_action": "UPDATE_SCHEDULE", "objective_id": "new"},
        target_reference={"id": "schedule-A", "objective_id": "new"},
    )
    assert ActionLoop._mutation_is_verified(task, old_change) is True
    assert ActionLoop._mutation_is_verified(task, new_change) is False


@pytest.mark.asyncio
async def test_new_mutation_reaches_runtime_with_exact_resource_and_objective() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-java", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id)],
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    change = TaskDelta(
        change_id="publish-java",
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "PUBLISH_NOW",
            "publication_intent": "IMMEDIATE",
        },
    )
    command, updated = await _compile_mutations(manager, task, [change])
    mutation = next(
        item for item in updated.objectives
        if item.objective_id != original.objective_id
    )
    submitted: list[dict] = []

    class RuntimeStore:
        async def _record(self, *_args, **_kwargs) -> None:
            return None

        async def record_mutation_submission(self, _task, **kwargs) -> None:
            submitted.append(dict(kwargs))

        async def _record_resource(
            self,
            current_task,
            resource_id,
            resource_kind,
            title="",
            content="",
            objective_id="",
        ) -> None:
            current_task.resource_index.append(
                TaskResourceRef(
                    resource_id=str(resource_id),
                    resource_kind=str(resource_kind),
                    objective_id=str(objective_id) or None,
                    title=str(title or ""),
                )
            )
            owner = next(
                item for item in current_task.objectives
                if item.objective_id == objective_id
            )
            owner.related_resource_ids.append(str(resource_id))

    async def write(**kwargs):
        submitted.append({"write": dict(kwargs)})
        return {
            "ok": True,
            "status": "COMPLETED",
            "execution_id": "execution-java-publish",
            "resource_id": "post-A",
        }

    result = await ActionLoop(
        write_submitter=write,
        task_store=RuntimeStore(),
        max_iterations=3,
    ).run(
        updated,
        command,
        request=SimpleNamespace(run_id="run-java", trace_id="trace-java"),
    )
    assert result.status == "COMPLETED"
    write_call = next(item["write"] for item in submitted if "write" in item)
    assert write_call["arguments"]["draft_id"] == "draft-A"
    assert write_call["objective_id"] == mutation.objective_id


def _mutation_objectives(task, action: str) -> list[Objective]:
    return [item for item in task.objectives if item.intent == action]


async def _task_with_draft_and_schedule(manager: TaskManager):
    original = _objective(
        "",
        "objective-java",
        "Java article",
        ["draft-A", "schedule-A"],
    )
    task = await _task(
        manager,
        [original],
        [
            _resource("draft-A", "DRAFT", original.objective_id),
            _resource("schedule-A", "SCHEDULE", original.objective_id),
        ],
    )
    original.task_id = task.task_id
    return await _persist(manager, task)


@pytest.mark.asyncio
async def test_conflict_case_a_pending_schedule_15_is_superseded_by_17() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)

    first_change = TaskDelta(
        change_id="schedule-15",
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )
    first_command, first_task = await _compile_mutations(manager, task, [first_change])
    old = _mutation_objectives(first_task, "UPDATE_SCHEDULE")[0]
    second_change = TaskDelta(
        change_id="schedule-17",
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "2026-08-22T09:00:00Z",
        },
    )
    second_command, latest = await _compile_mutations(manager, first_task, [second_change])
    new = [item for item in _mutation_objectives(latest, "UPDATE_SCHEDULE") if item.objective_id != old.objective_id][0]

    assert old.constraints["mutation_status"] == "SUPERSEDED"
    assert old.status == ObjectiveStatus.SUPERSEDED
    assert old.constraints["superseded_by"] == new.objective_id
    assert new.constraints["mutation_expected_state"]["run_at"] == "2026-08-22T09:00:00Z"
    assert ActionLoop()._next_pending_mutation_change(latest, first_command) is None
    assert ActionLoop()._next_pending_mutation_change(latest, second_command) is second_command.task_changes[0]


@pytest.mark.asyncio
async def test_conflict_case_b_pending_title_x_is_superseded_by_y() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    original = _objective("", "objective-java", "Java article", ["draft-A"])
    task = await _task(
        manager,
        [original],
        [_resource("draft-A", "DRAFT", original.objective_id)],
    )
    original.task_id = task.task_id
    task = await _persist(manager, task)
    first, task = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="title-x",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "X"},
        )],
    )
    old = _mutation_objectives(task, "UPDATE_DRAFT")[0]
    second, latest = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="title-y",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "Y"},
        )],
    )
    new = [item for item in _mutation_objectives(latest, "UPDATE_DRAFT") if item.objective_id != old.objective_id][0]

    assert old.constraints["mutation_status"] == "SUPERSEDED"
    assert new.constraints["mutation_expected_state"] == {"title": "Y"}
    assert ActionLoop()._next_pending_mutation_change(latest, first) is None
    assert ActionLoop()._next_pending_mutation_change(latest, second) is second.task_changes[0]


@pytest.mark.asyncio
async def test_conflict_case_c_title_and_schedule_are_independent_domains() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)
    command, latest = await _compile_mutations(
        manager,
        task,
        [
            TaskDelta(
                change_id="title-x",
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"label": "Java article"},
                desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "X"},
            ),
            TaskDelta(
                change_id="schedule-15",
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"label": "Java article"},
                desired_changes={
                    "semantic_action": "UPDATE_SCHEDULE",
                    "run_at": "2026-08-22T07:00:00Z",
                },
            ),
        ],
    )
    created = [item for item in latest.objectives if item.objective_id != "objective-java"]

    assert len(created) == 2
    assert {item.constraints["mutation_domain"] for item in created} == {"DRAFT", "PUBLICATION"}
    assert all(item.constraints["mutation_status"] == "ACTIVE" for item in created)
    assert ActionLoop()._next_pending_mutation_change(latest, command) is command.task_changes[0]


@pytest.mark.asyncio
async def test_conflict_case_d_cancel_supersedes_pending_schedule() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)
    _first, task = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="schedule-15",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-22T07:00:00Z",
            },
        )],
    )
    old = _mutation_objectives(task, "UPDATE_SCHEDULE")[0]
    command, latest = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="cancel",
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
        )],
    )
    cancel = _mutation_objectives(latest, "CANCEL_SCHEDULE")[0]

    assert old.constraints["mutation_status"] == "SUPERSEDED"
    assert cancel.constraints["mutation_domain"] == "PUBLICATION"
    assert ActionLoop()._next_pending_mutation_change(latest, command) is command.task_changes[0]


@pytest.mark.asyncio
async def test_conflict_case_e_publish_now_replaces_pending_schedule_without_two_writes() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)
    _first, task = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="schedule-15",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-22T07:00:00Z",
            },
        )],
    )
    old = _mutation_objectives(task, "UPDATE_SCHEDULE")[0]
    command, latest = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="publish-now",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={
                "semantic_action": "PUBLISH_NOW",
                "publication_intent": "IMMEDIATE",
            },
        )],
    )
    publish = _mutation_objectives(latest, "PUBLISH_NOW")[0]
    writes: list[dict] = []

    async def write(**kwargs):
        writes.append(kwargs)
        return {"ok": True, "status": "COMPLETED", "resource_id": "post-A"}

    loop = ActionLoop(write_submitter=write)
    assert loop._next_pending_mutation_change(latest, command) is command.task_changes[0]
    decision = loop._mutation_decision(latest, command)

    class Boundary:
        def record_operation_submitted(self, **_kwargs):
            return None

        def record_read(self):
            return None

    observation = await loop._act(
        "PUBLISH_NOW",
        decision,
        latest,
        command,
        SimpleNamespace(run_id="run-publish", trace_id="trace-publish"),
        Boundary(),
    )

    assert old.constraints["mutation_status"] == "SUPERSEDED"
    assert publish.constraints["mutation_expected_state"] == {"publication": "PUBLISHED"}
    assert observation.outcome == "SUCCESS"
    assert [item["arguments"]["draft_id"] for item in writes] == ["draft-A"], (
        observation.message,
        observation.detail,
    )


@pytest.mark.asyncio
async def test_conflict_case_f_unknown_old_mutation_waits_for_reconciliation() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)
    _first, task = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="schedule-15",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-22T07:00:00Z",
            },
        )],
    )
    old = _mutation_objectives(task, "UPDATE_SCHEDULE")[0]
    task.execution_refs.append(
        TaskExecutionRef(
            execution_id="execution-unknown",
            task_id=task.task_id,
            goal_id=old.objective_id,
            status="RESULT_UNKNOWN",
        )
    )
    task.revisions.append(
        TaskRevision(
            task_id=task.task_id,
            type=TaskRevisionType.MODIFY_GOAL,
            payload={
                "kind": "ACTION_LOOP_MUTATION_SUBMISSION",
                "action": "UPDATE_SCHEDULE",
                "mutation_domain": "PUBLICATION",
                "resource_id": "schedule-A",
                "objective_id": old.objective_id,
                "execution_id": "execution-unknown",
            },
        )
    )
    task = await _persist(manager, task)
    command, latest = await _compile_mutations(
        manager,
        task,
        [TaskDelta(
            change_id="schedule-17",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java article"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-22T09:00:00Z",
            },
        )],
    )
    writes: list[dict] = []

    async def write(**kwargs):
        writes.append(kwargs)
        return {"ok": True, "status": "COMPLETED"}

    result = await ActionLoop(write_submitter=write, max_iterations=2).run(
        latest,
        command,
        request=SimpleNamespace(run_id="run-unknown", trace_id="trace-unknown"),
    )

    assert old.constraints.get("mutation_status") != "SUPERSEDED"
    assert result.status == "WAITING_EXTERNAL"
    assert writes == []


@pytest.mark.asyncio
async def test_conflict_case_g_duplicate_turn_reuses_logical_mutation() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await _task_with_draft_and_schedule(manager)
    change = TaskDelta(
        change_id="schedule-15",
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java article"},
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )
    command, first = await _compile_mutations(manager, task, [change])
    mutation = _mutation_objectives(first, "UPDATE_SCHEDULE")[0]
    duplicate, latest = await _compile_mutations(manager, first, [change.model_copy(deep=True)])

    assert len(_mutation_objectives(latest, "UPDATE_SCHEDULE")) == 1
    assert duplicate.task_changes[0].desired_changes["objective_id"] == mutation.objective_id
    assert ActionLoop()._next_pending_mutation_change(latest, duplicate) is duplicate.task_changes[0]
