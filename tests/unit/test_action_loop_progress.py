"""Progress-invariant regressions for Objective-driven ActionLoop execution."""

from __future__ import annotations

import pytest
from greenbook_agent_core.actionloop import ActionDecision, ActionDecisionType, ActionLoop
from greenbook_agent_core.command.models import Command, CommandType, TaskDelta, TaskDeltaOperation
from greenbook_agent_core.task.models import (
    Objective,
    Task,
    TaskExecutionRef,
    TaskResourceRef,
    TaskRevision,
    TaskRevisionType,
)
from greenbook_agent_core.task.objective_reducer import all_objectives_satisfied


class _Store:
    def _record(self, *_args, **_kwargs) -> None:
        return None

    def _record_resource(self, task, resource_id, resource_kind, title, **kwargs) -> None:
        if any(str(item.resource_id) == str(resource_id) and item.resource_kind == resource_kind
               for item in task.resource_index):
            return
        task.resource_index.append(TaskResourceRef(
            resource_id=str(resource_id), resource_kind=resource_kind,
            objective_id=kwargs.get("objective_id"), title=title,
        ))


def _task() -> Task:
    objectives = [
        Objective(
            task_id="task-progress", description=name, intent=name,
            required_capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            constraints={"run_at": "2026-08-22T09:00:00Z"},
            result_requirement="RESOURCE_MUTATION",
        )
        for name in ("Java", "Agent")
    ]
    return Task(
        task_id="task-progress", conversation_id="conv-progress", user_id="user", tenant_id="tenant",
        objectives=objectives,
    )


@pytest.mark.asyncio
async def test_multi_objective_plan_finishes_before_model_can_reselect_completed_work() -> None:
    async def decide(context):
        objective = context["current_objective"]
        objective_id = objective["objective_id"]
        owned_kinds = {
            str(resource.get("resource_kind") or "").upper()
            for resource in context.get("resources", [])
            if str(resource.get("objective_id") or "") == objective_id
        }
        if "SEARCH_RESULT" not in owned_kinds:
            return ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action="SEARCH_POSTS",
                arguments={"query": objective["description"]},
            )
        if "DRAFT" not in owned_kinds:
            return ActionDecision(
                decision=ActionDecisionType.GENERATE_CONTENT,
                semantic_action="CREATE_DRAFT",
                arguments={"title": objective["description"], "instruction": "short"},
            )
        if "SCHEDULE" not in owned_kinds:
            return ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action="CREATE_SCHEDULE",
                arguments={"run_at": "2026-08-22T09:00:00Z"},
            )
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": objective["description"]},
        )

    async def read(**kwargs):
        return {"ok": True, "data": {"items": [{"id": f"search-{kwargs['arguments']['query']}"}]}}

    async def write(**kwargs):
        return {
            "ok": True,
            "status": "COMPLETED",
            "resource_id": f"{kwargs['semantic_action']}-{kwargs['objective_id']}",
        }

    result = await ActionLoop(
        decision_maker=decide, read_handler=read, write_submitter=write,
        task_store=_Store(), max_iterations=8,
    ).run(_task(), Command(type=CommandType.CREATE, goal="create", raw_input="create"))

    assert result.status == "COMPLETED"
    assert result.iterations == 7
    assert result.progress_trace[-1]["semantic_action"] == "FINISH"
    assert all(item["progress"] for item in result.progress_trace)


@pytest.mark.asyncio
async def test_identical_no_progress_state_fails_before_iteration_budget() -> None:
    objective = Objective(task_id="task-stall", description="stall", required_capabilities=["SEARCH_COMMUNITY"])
    task = Task(task_id="task-stall", conversation_id="conv", user_id="user", tenant_id="tenant", objectives=[objective])

    async def decide(_context):
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": "unchanged"},
        )

    async def read(**_kwargs):
        return {"ok": True}

    result = await ActionLoop(
        decision_maker=decide, read_handler=read, task_store=_Store(), max_iterations=8,
    ).run(task, Command(type=CommandType.CREATE, goal="stall", raw_input="stall"))

    assert result.error_code == "ACTION_LOOP_NO_PROGRESS"
    assert result.iterations == 2
    assert result.progress_trace[-1]["progress"] is False


@pytest.mark.asyncio
async def test_empty_search_result_finishes_without_fabricating_resource() -> None:
    objective = Objective(
        task_id="task-empty-search",
        description="search an unavailable topic",
        required_capabilities=["SEARCH_COMMUNITY"],
    )
    task = Task(
        task_id="task-empty-search",
        conversation_id="conv",
        user_id="user",
        tenant_id="tenant",
        objectives=[objective],
    )
    decisions = 0

    async def decide(_context):
        nonlocal decisions
        decisions += 1
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": "no matching topic"},
        )

    async def read(**_kwargs):
        return {
            "ok": True,
            "message": "没有找到相关内容。",
            "data": {"items": [], "total": 0},
        }

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        task_store=_Store(),
        max_iterations=8,
    ).run(
        task,
        Command(type=CommandType.QUERY, goal="search", raw_input="search"),
    )

    assert result.status == "COMPLETED"
    assert result.content == "没有找到相关内容。"
    assert result.iterations == 1
    assert decisions == 1
    assert result.observations[0].resource_id is None
    assert task.resource_index == []
    assert objective.status == "COMPLETED"
    assert objective.constraints["discovery_result"]["count"] == 0


@pytest.mark.asyncio
async def test_completed_task_still_executes_explicit_objective_mutation() -> None:
    objective = Objective(
        task_id="task-cross",
        objective_id="objective-java",
        description="Java article",
        status="COMPLETED",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        result_requirement="RESOURCE_MUTATION",
        related_resource_ids=["draft-java", "schedule-java"],
        constraints={"run_at": "2026-08-19T01:00:00Z"},
    )
    task = Task(
        task_id="task-cross", conversation_id="conv", user_id="user", tenant_id="tenant",
        status="COMPLETED", objectives=[objective],
        resource_index=[
            TaskResourceRef(resource_id="draft-java", resource_kind="DRAFT", objective_id="objective-java"),
            TaskResourceRef(resource_id="schedule-java", resource_kind="SCHEDULE", objective_id="objective-java"),
        ],
    )
    submitted: list[dict] = []

    async def write(**kwargs):
        submitted.append(kwargs)
        return {"ok": True, "status": "COMPLETED", "resource_id": "schedule-java"}

    change = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={
            "id": "schedule-java",
            "label": "Java article",
            "objective_id": "objective-java",
        },
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "2026-08-19T08:00:00Z",
            "objective_id": "objective-java",
        },
    )
    result = await ActionLoop(
        decision_maker=lambda _context: ActionDecision(
            decision=ActionDecisionType.FINISH,
            semantic_action="FINISH",
        ),
        write_submitter=write,
        task_store=_Store(),
        max_iterations=4,
    ).run(
        task,
        Command(type=CommandType.MODIFY, goal="update", task_changes=[change], raw_input="update"),
    )

    assert result.status == "COMPLETED"
    assert len(submitted) == 1
    assert submitted[0]["objective_id"] == "objective-java"


def test_same_turn_mutation_continuation_keeps_target_identity() -> None:
    """A submitted UPDATE for A must not be selected again before B."""
    task = Task(
        task_id="task-case10",
        conversation_id="conv-case10",
        user_id="user",
        tenant_id="tenant",
    )
    changes = [
        TaskDelta(
            change_id="mutation-A",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-A", "objective_id": "objective-A"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-A",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
        TaskDelta(
            change_id="mutation-B",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-B", "objective_id": "objective-B"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-B",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
    ]
    task.revisions = [
        TaskRevision(
            task_id=task.task_id,
            type=TaskRevisionType.MODIFY_GOAL,
            payload={
                "kind": "ACTION_LOOP_MUTATION_PLAN",
                "task_changes": [change.model_dump(mode="json") for change in changes],
            },
            previous_version=task.version,
        )
    ]
    command = Command(
        type=CommandType.MODIFY,
        goal="update both schedules",
        task_changes=changes,
    )
    loop = ActionLoop()

    first = loop._mutation_decision(task, command)
    assert first.arguments["schedule_id"] == "schedule-A"
    # The private decision builder does not mark progress until the write
    # boundary returns; emulate A's successful submission before resuming.
    loop._mutation_done.add(
        loop._mutation_key("task-case10", "UPDATE_SCHEDULE", "schedule-A", "objective-A")
    )

    # The resumed iteration has no command payload; it reconstructs the same
    # durable plan and must select the remaining target, not the first action.
    second = loop._mutation_decision(task, None)
    assert second.arguments["schedule_id"] == "schedule-B"
    assert second.arguments["objective_id"] == "objective-B"


def test_one_verified_mutation_does_not_complete_two_mutation_task() -> None:
    task = Task(
        task_id="task-case10-completion",
        conversation_id="conv-case10",
        user_id="user",
        tenant_id="tenant",
        objectives=[
            Objective(
                task_id="task-case10-completion",
                objective_id="objective-A",
                required_capabilities=["MANAGE_SCHEDULE"],
                related_resource_ids=["schedule-A"],
                related_operations=["operation-A"],
                result_requirement="RESOURCE_MUTATION",
            ),
            Objective(
                task_id="task-case10-completion",
                objective_id="objective-B",
                required_capabilities=["MANAGE_SCHEDULE"],
                related_resource_ids=["schedule-B"],
                result_requirement="RESOURCE_MUTATION",
            ),
        ],
        resource_index=[
            TaskResourceRef(
                resource_id="schedule-A",
                resource_kind="SCHEDULE",
                objective_id="objective-A",
            ),
            TaskResourceRef(
                resource_id="schedule-B",
                resource_kind="SCHEDULE",
                objective_id="objective-B",
            ),
        ],
    )

    loop = ActionLoop()
    assert loop._verify_finish(task) is False
    assert all_objectives_satisfied(task) is False
    assert task.objectives[0].status == "COMPLETED"
    assert task.objectives[1].status != "COMPLETED"


@pytest.mark.asyncio
async def test_two_mutations_submit_once_across_continuations_then_complete() -> None:
    """Case 10 shape: A waits, resume submits B, then both verify."""
    task_id = "task-case10-runtime"
    task = Task(
        task_id=task_id,
        conversation_id="conv-case10",
        user_id="user",
        tenant_id="tenant",
        objectives=[
            Objective(
                task_id=task_id,
                objective_id="objective-A",
                required_capabilities=["MANAGE_SCHEDULE"],
                result_requirement="RESOURCE_MUTATION",
                related_resource_ids=["schedule-A"],
                constraints={"run_at": "2026-08-22T07:00:00Z", "timezone": "UTC"},
            ),
            Objective(
                task_id=task_id,
                objective_id="objective-B",
                required_capabilities=["MANAGE_SCHEDULE"],
                result_requirement="RESOURCE_MUTATION",
                related_resource_ids=["schedule-B"],
                constraints={"run_at": "2026-08-22T07:00:00Z", "timezone": "UTC"},
            ),
        ],
        resource_index=[
            TaskResourceRef(
                resource_id="schedule-A",
                resource_kind="SCHEDULE",
                objective_id="objective-A",
                scheduled_at="2026-08-21T07:00:00Z",
            ),
            TaskResourceRef(
                resource_id="schedule-B",
                resource_kind="SCHEDULE",
                objective_id="objective-B",
                scheduled_at="2026-08-21T07:00:00Z",
            ),
        ],
    )
    changes = [
        TaskDelta(
            change_id="mutation-A",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-A", "objective_id": "objective-A"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-A",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
        TaskDelta(
            change_id="mutation-B",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-B", "objective_id": "objective-B"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-B",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
    ]

    class _DurableStore(_Store):
        async def persist_mutation_plan(self, current_task, task_changes) -> None:
            current_task.revisions.append(
                TaskRevision(
                    task_id=current_task.task_id,
                    type=TaskRevisionType.MODIFY_GOAL,
                    payload={
                        "kind": "ACTION_LOOP_MUTATION_PLAN",
                        "task_changes": [
                            change.model_dump(mode="json") for change in task_changes
                        ],
                    },
                    previous_version=current_task.version,
                )
            )

        async def record_mutation_submission(
            self, current_task, *, action, arguments, execution_id
        ) -> None:
            current_task.revisions.append(
                TaskRevision(
                    task_id=current_task.task_id,
                    type=TaskRevisionType.MODIFY_GOAL,
                    payload={
                        "kind": "ACTION_LOOP_MUTATION_SUBMISSION",
                        "action": action,
                        "resource_id": arguments["schedule_id"],
                        "execution_id": execution_id,
                    },
                    previous_version=current_task.version,
                )
            )

    store = _DurableStore()
    submitted: list[str] = []

    async def submit(**kwargs):
        target = kwargs["arguments"]["schedule_id"]
        submitted.append(target)
        return {
            "ok": True,
            "status": "SUBMITTED",
            "execution_id": f"execution-{target[-1]}",
        }

    command = Command(
        type=CommandType.MODIFY,
        goal="update both schedules",
        task_changes=changes,
    )
    first = await ActionLoop(
        write_submitter=submit,
        task_store=store,
        max_iterations=4,
    ).run(task, command)
    assert first.status == "WAITING_EXTERNAL", (
        first.error_code,
        first.error_message,
        [(item.action, item.outcome, item.message) for item in first.observations],
    )

    # Durable completion of A opens the continuation.  The second ActionLoop
    # instance must reconstruct the plan and select B from its verified state.
    task.execution_refs = [
        TaskExecutionRef(
            execution_id="execution-A",
            task_id=task_id,
            status="COMPLETED",
        )
    ]
    task.objectives[0].related_operations.append("execution-A")
    second = await ActionLoop(
        write_submitter=submit,
        task_store=store,
        max_iterations=4,
    ).run(task, None)
    assert second.status == "WAITING_EXTERNAL", (
        second.error_code,
        second.error_message,
        [(item.action, item.outcome, item.message) for item in second.observations],
    )

    task.execution_refs.append(
        TaskExecutionRef(
            execution_id="execution-B",
            task_id=task_id,
            status="COMPLETED",
        )
    )
    task.objectives[1].related_operations.append("execution-B")
    final = await ActionLoop(
        write_submitter=submit,
        task_store=store,
        max_iterations=4,
    ).run(task, None)

    assert submitted == ["schedule-A", "schedule-B"]
    assert final.status == "COMPLETED"
    assert all_objectives_satisfied(task) is True


@pytest.mark.asyncio
async def test_idempotent_duplicate_A_still_advances_to_B() -> None:
    """A ledger dedupe of a replayed A must not consume B's mutation slot."""
    task_id = "task-case10-dedupe"
    task = Task(
        task_id=task_id,
        conversation_id="conv-case10",
        user_id="user",
        tenant_id="tenant",
        objectives=[
            Objective(
                task_id=task_id,
                objective_id="objective-A",
                status="COMPLETED",
                required_capabilities=["MANAGE_SCHEDULE"],
                result_requirement="RESOURCE_MUTATION",
                related_resource_ids=["schedule-A"],
                related_operations=["prior-A"],
                constraints={"run_at": "2026-08-22T07:00:00Z", "timezone": "UTC"},
            ),
            Objective(
                task_id=task_id,
                objective_id="objective-B",
                status="COMPLETED",
                required_capabilities=["MANAGE_SCHEDULE"],
                result_requirement="RESOURCE_MUTATION",
                related_resource_ids=["schedule-B"],
                related_operations=["prior-B"],
                constraints={"run_at": "2026-08-22T07:00:00Z", "timezone": "UTC"},
            ),
        ],
        resource_index=[
            TaskResourceRef(
                resource_id="schedule-A",
                resource_kind="SCHEDULE",
                objective_id="objective-A",
                scheduled_at="2026-08-21T07:00:00Z",
            ),
            TaskResourceRef(
                resource_id="schedule-B",
                resource_kind="SCHEDULE",
                objective_id="objective-B",
                scheduled_at="2026-08-21T07:00:00Z",
            ),
        ],
    )
    changes = [
        TaskDelta(
            change_id="mutation-A",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-A", "objective_id": "objective-A"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-A",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
        TaskDelta(
            change_id="mutation-B",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"id": "schedule-B", "objective_id": "objective-B"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": "objective-B",
                "run_at": "2026-08-22T07:00:00Z",
            },
        ),
    ]
    task.revisions = [
        TaskRevision(
            task_id=task_id,
            type=TaskRevisionType.MODIFY_GOAL,
            payload={
                "kind": "ACTION_LOOP_MUTATION_PLAN",
                "task_changes": [change.model_dump(mode="json") for change in changes],
            },
            previous_version=task.version,
        ),
        # A prior submission is known to the ledger, but this deliberately
        # models a stale Task snapshot with no projected ExecutionRef.
        TaskRevision(
            task_id=task_id,
            type=TaskRevisionType.MODIFY_GOAL,
            payload={
                "kind": "ACTION_LOOP_MUTATION_SUBMISSION",
                "action": "UPDATE_SCHEDULE",
                "resource_id": "schedule-A",
                "execution_id": "execution-A",
            },
            previous_version=task.version + 1,
        ),
    ]
    logical_attempts = ["schedule-A"]
    physical_updates = ["schedule-A"]

    async def submit(**kwargs):
        target = kwargs["arguments"]["schedule_id"]
        logical_attempts.append(target)
        if target not in physical_updates:
            physical_updates.append(target)
        return {"ok": True, "status": "COMPLETED", "resource_id": target}

    result = await ActionLoop(
        write_submitter=submit,
        task_store=_Store(),
        max_iterations=5,
    ).run(task, None)

    assert logical_attempts == ["schedule-A", "schedule-A", "schedule-B"]
    assert physical_updates == ["schedule-A", "schedule-B"]
    assert result.status == "COMPLETED"
