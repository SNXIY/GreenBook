"""Cross-turn PARTIAL/FAILED retry closure.

These tests exercise the existing TaskDelta/TargetResolver/TaskManager
boundaries.  They intentionally do not start a queue, Worker, Java service, or
create a second retry runtime.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.models.runtime_result import RuntimeResult
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
    _is_user_triggered_objective_retry,
)
from greenbook_agent_api.services.turn_coordinator import (
    TurnCoordinator,
    _failed_retry_clarification,
)
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolutionStatus, TargetResolver
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import InMemoryTaskRepository, TaskManager
from greenbook_agent_core.task.models import (
    ObjectiveStatus,
    TaskExecutionRef,
    TaskResourceRef,
    TaskStatus,
)


def _retry_delta(
    *,
    objective_id: str = "",
    title: str = "",
    change_id: str = "retry-1",
) -> TaskDelta:
    reference = {"reference_type": "FAILED"}
    if objective_id:
        reference["objective_id"] = objective_id
    desired = {}
    if title:
        desired["title"] = title
    return TaskDelta(
        operation=TaskDeltaOperation.CONTINUE_TASK,
        change_id=change_id,
        target_reference=reference,
        desired_changes=desired,
        source_reference={
            "kind": "FAILED_OBJECTIVE_RETRY",
            "user_triggered_retry": True,
        },
    )


def _candidate(objective_id: str, status: str, task_id: str) -> dict[str, object]:
    return {
        "id": objective_id,
        "goal_id": objective_id,
        "objective_id": objective_id,
        "task_id": task_id,
        "kind": "TASK",
        "label": objective_id,
        "status": status,
        "metadata": {"objective_id": objective_id},
    }


def test_failed_retry_resolution_is_status_scoped() -> None:
    delta = _retry_delta()
    resolution = TargetResolver().resolve_task_delta(
        delta,
        [
            _candidate("failed", "FAILED", "task-failed"),
            _candidate("done", "COMPLETED", "task-done"),
            _candidate("cancelled", "CANCELLED", "task-cancelled"),
            _candidate("superseded", "SUPERSEDED", "task-superseded"),
        ],
        active_task_id="task-done",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "failed"


def test_multiple_failed_retry_targets_clarify_without_latest_fallback() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _retry_delta(),
        [
            _candidate("failed-a", "FAILED", "task-a"),
            _candidate("failed-b", "FAILED", "task-b"),
        ],
        active_task_id="task-a",
    )
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert resolution.target is None


def _labelled_retry_delta(label: str) -> TaskDelta:
    return TaskDelta(
        operation=TaskDeltaOperation.CONTINUE_TASK,
        change_id="retry-grounding",
        target_reference={"reference_type": "FAILED", "label": label},
        desired_changes={},
        source_reference={
            "kind": "FAILED_OBJECTIVE_RETRY",
            "user_triggered_retry": True,
        },
    )


def _labelled_failed(objective_id: str, label: str, task_id: str) -> dict[str, object]:
    return {
        **_candidate(objective_id, "FAILED", task_id),
        "label": label,
        "semantic_label": label,
        "description": label,
    }


def test_provider_label_without_current_turn_grounding_is_ambiguous() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Agent 学习失败澄清验证"),
        [
            _labelled_failed("java", "Java 学习失败澄清验证", "task-java"),
            _labelled_failed("agent", "Agent 学习失败澄清验证", "task-agent"),
        ],
        user_input="失败的那个再试。",
    )
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert resolution.target is None
    assert len(resolution.candidates) == 2


def test_provider_label_with_current_turn_grounding_resolves_only_java() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Java 学习失败澄清验证"),
        [
            _labelled_failed("java", "Java 学习失败澄清验证", "task-java"),
            _labelled_failed("agent", "Agent 学习失败澄清验证", "task-agent"),
        ],
        user_input="失败的 Java 那个再试。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "java"


def test_long_provider_task_label_uses_unique_user_grounding() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("写一篇 Java 学习失败澄清验证短帖并保存为草稿"),
        [
            _labelled_failed("java", "Java 学习失败澄清验证", "task-java"),
            _labelled_failed("agent", "Agent 学习失败澄清验证", "task-agent"),
        ],
        user_input="选择 Java 学习失败澄清验证短帖这个失败任务重试。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "java"


def test_generic_failed_retry_with_one_candidate_still_resolves() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Agent 学习失败澄清验证"),
        [_labelled_failed("agent", "Agent 学习失败澄清验证", "task-agent")],
        user_input="失败的那个再试。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "agent"


def test_two_same_label_failed_resources_remain_ambiguous() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Java 学习路线"),
        [
            {
                **_labelled_failed("java-a", "Java 学习路线", "task-a"),
                "resource_index": [{"resource_id": "draft-a", "resource_kind": "DRAFT"}],
            },
            {
                **_labelled_failed("java-b", "Java 学习路线", "task-b"),
                "resource_index": [{"resource_id": "draft-b", "resource_kind": "DRAFT"}],
            },
        ],
        user_input="Java 那篇再试。",
    )
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert resolution.target is None


def test_unique_semantic_label_candidate_resolves() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Java 学习路线"),
        [_labelled_failed("java", "Java 学习路线", "task-java")],
        user_input="Java 那篇再试。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "java"


def test_provider_specific_target_is_not_authorized_by_context_alone() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Agent 学习路线"),
        [
            _labelled_failed("java", "Java 学习路线", "task-java"),
            _labelled_failed("agent", "Agent 学习路线", "task-agent"),
        ],
        user_input="失败的那个再试。",
    )
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert resolution.target is None


def test_provider_specific_target_supported_by_user_evidence_is_resolved() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _labelled_retry_delta("Agent 学习路线"),
        [
            _labelled_failed("java", "Java 学习路线", "task-java"),
            _labelled_failed("agent", "Agent 学习路线", "task-agent"),
        ],
        user_input="Agent 那篇再试。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "agent"


def test_failed_retry_without_desired_changes_still_surfaces_ambiguity() -> None:
    """A retry marker cannot bypass resolution just because its delta is sparse."""

    command = Command(
        type=CommandType.CREATE,
        goal="重试失败的任务",
        raw_input="失败的那个再试。",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.CREATE_TASK,
                target_reference={"kind": "TASK", "reference_type": "FAILED"},
                desired_changes={},
                source_reference={
                    "kind": "FAILED_OBJECTIVE_RETRY",
                    "user_triggered_retry": True,
                },
            )
        ],
    )
    assembled = SimpleNamespace(
        snapshot=SimpleNamespace(
            active_task_id="",
            active_tasks=[
                {
                    "task_id": "task-java",
                    "goal": "Java 失败任务",
                    "objectives": [
                        {
                            "objective_id": "objective-java",
                            "description": "Java 失败任务",
                            "status": "FAILED",
                        }
                    ],
                },
                {
                    "task_id": "task-agent",
                    "goal": "Agent 失败任务",
                    "objectives": [
                        {
                            "objective_id": "objective-agent",
                            "description": "Agent 失败任务",
                            "status": "FAILED",
                        }
                    ],
                },
            ],
        )
    )
    coordinator = TurnCoordinator.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()

    resolution = coordinator._resolve_delta_objective_target(command, assembled)

    assert resolution is not None
    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert len(resolution.candidates) == 2


def test_failed_retry_uses_bounded_snapshot_when_selected_view_omits_sibling() -> None:
    """Historical failed retries must see the snapshot's failed sibling."""

    command = Command(
        type=CommandType.CREATE,
        raw_input="请重试 Agent 工程实践这个失败目标，仍然保存为草稿。",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.CREATE_TASK,
                target_reference={
                    "kind": "TASK",
                    "label": "Agent 工程实践",
                    "reference_type": "FAILED",
                },
                desired_changes={"semantic_action": "GENERATE_CONTENT"},
                source_reference={
                    "kind": "FAILED_OBJECTIVE_RETRY",
                    "user_triggered_retry": True,
                },
            )
        ],
    )
    selected = SimpleNamespace(
        snapshot=SimpleNamespace(
            active_tasks=[
                {
                    "task_id": "task-agent",
                    "goal": "Agent 工程实践",
                    "objectives": [
                        {
                            "objective_id": "objective-agent",
                            "description": "Agent 工程实践",
                            "status": "FAILED",
                        }
                    ],
                },
                {
                    "task_id": "task-python",
                    "goal": "Python 工程实践",
                    "objectives": [
                        {
                            "objective_id": "objective-python",
                            "description": "Python 工程实践",
                            "status": "FAILED",
                        }
                    ],
                },
            ]
        ),
        selected_tasks=[
            {
                "task_id": "task-python",
                "goal": "Python 工程实践",
                "objectives": [
                    {
                        "objective_id": "objective-python",
                        "description": "Python 工程实践",
                        "status": "FAILED",
                    }
                ],
            }
        ],
    )
    coordinator = TurnCoordinator.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()

    resolution = coordinator._resolve_delta_objective_target(command, selected)

    assert resolution is not None
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "objective-agent"


def test_failed_retry_clarification_uses_business_labels_only() -> None:
    command = Command(
        type=CommandType.CREATE,
        task_changes=[_retry_delta()],
    )
    message = _failed_retry_clarification(
        command,
        [
            {
                "label": "Java 学习路线",
                "status": "FAILED",
                "id": "objective-java-secret",
            },
            {
                "label": "Agent 开发指南",
                "status": "FAILED",
                "id": "objective-agent-secret",
            },
        ],
    )
    assert message == "存在两个失败的内容任务（Java 学习路线、Agent 开发指南），你想重试哪一个？"
    assert "objective-" not in message


def test_failed_retry_label_can_match_owning_task_business_label() -> None:
    """A named retry may quote the Task label rather than its short topic."""

    delta = TaskDelta(
        operation=TaskDeltaOperation.CONTINUE_TASK,
        target_reference={
            "kind": "TASK",
            "reference_type": "FAILED",
            "label": "写一篇 Java 学习失败澄清验证短帖并保存为草稿。",
        },
        source_reference={
            "kind": "FAILED_OBJECTIVE_RETRY",
            "user_triggered_retry": True,
        },
    )
    resolution = TargetResolver().resolve_task_delta(
        delta,
        [{
            **_candidate("objective-java", "FAILED", "task-java"),
            "label": "Java 学习失败澄清验证",
            "task_label": "写一篇 Java 学习失败澄清验证短帖并保存为草稿。",
        }],
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.id == "objective-java"


def test_result_unknown_is_not_a_failed_retry_target() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _retry_delta(objective_id="unknown"),
        [
            {
                **_candidate("unknown", "WAITING", "task-unknown"),
                "execution_statuses": ["RESULT_UNKNOWN"],
            }
        ],
    )
    assert resolution.status == TargetResolutionStatus.NOT_FOUND
    assert resolution.reason == "retry_requires_reconciliation"


@pytest.mark.asyncio
async def test_user_retry_creates_new_identity_and_reuses_failed_draft() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)
    old = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="tenant-1",
        goal_tree=GoalTree(
            root=Goal(
                goal_id="old-objective",
                description="发布文章",
                required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            )
        ),
    )
    old.objectives[0].status = ObjectiveStatus.FAILED
    old.objectives[0].related_resource_ids = ["draft-123"]
    old.objectives[0].constraints = {"run_at": "2026-09-01T10:00:00Z"}
    old.resource_index = [
        TaskResourceRef(
            resource_id="draft-123",
            resource_kind="DRAFT",
            objective_id="old-objective",
            title="旧标题",
            status="DRAFT",
        )
    ]
    old.status = TaskStatus.FAILED
    old.execution_refs = [
        TaskExecutionRef(
            execution_id="old-execution",
            task_id=old.task_id,
            goal_id="old-objective",
            status="FAILED",
        )
    ]
    await repository.update(old, expected_version=old.version)
    old_before = (await manager.get_required(old.task_id)).model_dump(mode="json")

    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    adapter._external_operation_store = None
    calls: list[str] = []

    async def fake_run_agent_loop(**kwargs):
        calls.append(str(kwargs["existing_task"].task_id))
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            trace_id=kwargs["trace_id"],
            task_id=kwargs["existing_task"].task_id,
        )

    adapter._run_agent_loop = fake_run_agent_loop
    command = Command(type=CommandType.MODIFY, goal="失败的那个再试")
    result = await adapter._run_task_deltas(
        deltas=[_retry_delta(objective_id="old-objective")],
        command=command,
        context=SimpleNamespace(),
        request_session=SimpleNamespace(active_task_id=old.task_id),
        conversation_id="c1",
        user_id="u1",
        tenant_id="tenant-1",
        run_id="run-retry",
        trace_id="trace-retry",
        llm=None,
        model="test",
    )

    assert result.status == "COMPLETED"
    assert len(calls) == 1
    new_id = calls[0]
    assert new_id != old.task_id
    persisted_old = await manager.get_required(old.task_id)
    assert persisted_old.model_dump(mode="json") == old_before
    new_task = await manager.get_required(new_id)
    assert new_task.objectives[0].objective_id != "old-objective"
    assert new_task.objectives[0].status == ObjectiveStatus.PENDING
    assert new_task.objectives[0].related_resource_ids == ["draft-123"]
    assert new_task.resource_index[0].objective_id == new_task.objectives[0].objective_id
    assert "GENERATE_CONTENT" not in new_task.objectives[0].required_capabilities
    assert "SCHEDULE_PUBLISH" in new_task.objectives[0].required_capabilities


@pytest.mark.asyncio
async def test_retry_title_change_reuses_draft_and_orders_update_before_publish() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)
    old = await manager.create_task(
        conversation_id="c1", user_id="u1", tenant_id="tenant-1",
        goal_tree=GoalTree(root=Goal(
            goal_id="failed-publish",
            description="发布文章",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        )),
    )
    old.objectives[0].status = ObjectiveStatus.FAILED
    old.objectives[0].related_resource_ids = ["draft-1"]
    old.resource_index = [TaskResourceRef(
        resource_id="draft-1", resource_kind="DRAFT",
        objective_id="failed-publish", title="旧标题",
    )]
    old.status = TaskStatus.FAILED
    await repository.update(old, expected_version=old.version)
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    adapter._external_operation_store = None
    new_task = await adapter._create_user_objective_retry_task(
        manager,
        old,
        _retry_delta(objective_id="failed-publish", title="新标题"),
        Command(type=CommandType.MODIFY, goal="改标题再发"),
        conversation_id="c1", user_id="u1", tenant_id="tenant-1",
    )
    caps = new_task.objectives[0].required_capabilities
    assert caps[:2] == ["MANAGE_DRAFT", "SCHEDULE_PUBLISH"]
    assert new_task.objectives[0].constraints["title"] == "新标题"
    assert new_task.objectives[0].constraints["draft_id"] == "draft-1"
    assert old.objectives[0].status == ObjectiveStatus.FAILED


@pytest.mark.asyncio
async def test_failed_before_resource_creation_allows_fresh_create() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)
    old = await manager.create_task(
        conversation_id="c1", user_id="u1", tenant_id="tenant-1",
        goal_tree=GoalTree(root=Goal(
            goal_id="failed-create",
            description="创建草稿",
            required_capabilities=["GENERATE_CONTENT"],
        )),
    )
    old.objectives[0].status = ObjectiveStatus.FAILED
    old.status = TaskStatus.FAILED
    await repository.update(old, expected_version=old.version)
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._external_operation_store = None
    new_task = await adapter._create_user_objective_retry_task(
        manager,
        old,
        _retry_delta(objective_id="failed-create"),
        Command(type=CommandType.MODIFY, goal="再创建"),
        conversation_id="c1", user_id="u1", tenant_id="tenant-1",
    )
    assert new_task.task_id != old.task_id
    assert new_task.objectives[0].required_capabilities == ["GENERATE_CONTENT"]
    assert new_task.resource_index == []


@pytest.mark.asyncio
async def test_explicit_failed_retries_fan_out_without_touching_successful_sibling() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)

    async def make_task(objective_id: str, status: ObjectiveStatus) -> object:
        task = await manager.create_task(
            conversation_id="c1",
            user_id="u1",
            tenant_id="tenant-1",
            goal_tree=GoalTree(root=Goal(
                goal_id=objective_id,
                description=objective_id,
                required_capabilities=["GENERATE_CONTENT"],
            )),
        )
        task.objectives[0].status = status
        task.status = (
            TaskStatus.FAILED
            if status == ObjectiveStatus.FAILED
            else TaskStatus.COMPLETED
        )
        await repository.update(task, expected_version=task.version)
        return task

    failed_a = await make_task("failed-a", ObjectiveStatus.FAILED)
    failed_b = await make_task("failed-b", ObjectiveStatus.FAILED)
    completed = await make_task("completed", ObjectiveStatus.COMPLETED)

    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    adapter._external_operation_store = None
    resumed: list[str] = []
    physical_writes: list[str] = []

    async def fake_run_agent_loop(**kwargs):
        resumed.append(str(kwargs["existing_task"].task_id))
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            trace_id=kwargs["trace_id"],
            task_id=kwargs["existing_task"].task_id,
        )

    adapter._run_agent_loop = fake_run_agent_loop
    result = await adapter._run_task_deltas(
        deltas=[
            _retry_delta(objective_id="failed-a", change_id="retry-a"),
            _retry_delta(objective_id="failed-b", change_id="retry-b"),
        ],
        command=Command(type=CommandType.MODIFY, goal="只重试没成功的"),
        context=SimpleNamespace(),
        request_session=SimpleNamespace(active_task_id=completed.task_id),
        conversation_id="c1",
        user_id="u1",
        tenant_id="tenant-1",
        run_id="run-batch-retry",
        trace_id="trace-batch-retry",
        llm=None,
        model="test",
    )

    assert result.status == "COMPLETED"
    assert len(resumed) == 2
    assert not physical_writes
    assert (await manager.get_required(failed_a.task_id)).status == TaskStatus.FAILED
    assert (await manager.get_required(failed_b.task_id)).status == TaskStatus.FAILED
    assert (await manager.get_required(completed.task_id)).status == TaskStatus.COMPLETED


def test_runtime_retry_marker_is_not_generic_continue() -> None:
    assert not _is_user_triggered_objective_retry(TaskDelta(
        operation=TaskDeltaOperation.CONTINUE_TASK,
        target_reference={"reference_type": "ACTIVE"},
    ))
