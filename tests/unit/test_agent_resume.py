"""Durable Agent resume projection tests."""

from greenbook_agent_core.agent.recovery import (
    AgentRecoveryService,
    RecoveryKind,
)
from greenbook_agent_core.execution.checkpoint import ExecutionCheckpoint
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.task.models import Task, TaskGoal, TaskStatus


def _task() -> Task:
    return Task(
        task_id="task-resume",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="publish an article",
        status=TaskStatus.RUNNING,
        goal_tree_version=3,
        plan_version=4,
        goals=[
            TaskGoal(task_id="task-resume", goal_id="goal-1", status="COMPLETED"),
            TaskGoal(task_id="task-resume", goal_id="goal-2", status="PENDING"),
        ],
    )


def test_resume_context_preserves_completed_steps_and_plan_version() -> None:
    execution = PlanExecution(
        execution_id="execution-resume",
        task_id="task-resume",
        status=ExecutionStatus.RUNNING,
        steps=[
            StepExecution(step_id="search", status=StepStatus.COMPLETED),
            StepExecution(
                step_id="create",
                status=StepStatus.FAILED_RETRYABLE,
                error_code="TIMEOUT",
                retry_count=0,
                max_retries=2,
            ),
        ],
    )
    checkpoint = ExecutionCheckpoint(
        execution_id=execution.execution_id,
        completed_steps=["search"],
        current_step="create",
        snapshot={"iteration": 5, "last_observation_summary": "creator timed out"},
    )

    service = AgentRecoveryService()
    decision = service.decide(task=_task(), execution=execution, checkpoint=checkpoint)
    context = service.build_resume_context(
        task=_task(), execution=execution, checkpoint=checkpoint,
        memory_ids_used=["memory-1"],
    )

    assert decision.action == RecoveryKind.RETRY_STEP
    assert decision.step_id == "create"
    assert context.plan_version == 4
    assert context.goal_tree_version == 3
    assert context.completed_step_ids == ["search"]
    assert context.iteration == 5
    assert context.memory_ids_used == ["memory-1"]


def test_waiting_human_resume_does_not_reexecute_tools() -> None:
    task = _task().model_copy(update={"status": TaskStatus.WAITING_HUMAN})
    execution = PlanExecution(
        execution_id="execution-human",
        task_id=task.task_id,
        status=ExecutionStatus.WAITING_HUMAN,
    )
    decision = AgentRecoveryService().decide(task=task, execution=execution)
    assert decision.action == RecoveryKind.WAIT_FOR_HUMAN
