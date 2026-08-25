from types import SimpleNamespace

import pytest

from greenbook_agent_core.execution.result_projection import ExecutionResultProjection
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskResourceRef,
    TaskStatus,
)
from greenbook_contracts.identity import AuthContext

from greenbook_agent_api.api.routes import _final_response_projection


class _Provider:
    def __init__(self, task):
        self.task = task

    async def get_task(self, _scope, _task_id):
        return self.task


def _request(task):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_provider=_Provider(task))))


def _auth():
    return AuthContext(user_id="u", tenant_id="t", roles=[], raw_access_token="")


def _task():
    return Task(
        task_id="task-1",
        conversation_id="c",
        user_id="u",
        tenant_id="t",
        status=TaskStatus.COMPLETED,
        objectives=[
            Objective(
                task_id="task-1",
                objective_id="java",
                intent="Java",
                status=ObjectiveStatus.COMPLETED,
                required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                related_resource_ids=["d-java", "s-java"],
                constraints={"timezone": "Asia/Shanghai"},
            ),
            Objective(
                task_id="task-1",
                objective_id="agent",
                intent="Agent",
                status=ObjectiveStatus.COMPLETED,
                required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                related_resource_ids=["d-agent", "s-agent"],
                constraints={"timezone": "Asia/Shanghai"},
            ),
        ],
        resource_index=[
            TaskResourceRef(resource_id="d-java", resource_kind="DRAFT"),
            TaskResourceRef(resource_id="s-java", resource_kind="SCHEDULE", scheduled_at="2026-08-19T01:00:00Z"),
            TaskResourceRef(resource_id="d-agent", resource_kind="DRAFT"),
            TaskResourceRef(resource_id="s-agent", resource_kind="SCHEDULE", scheduled_at="2026-08-19T06:00:00Z"),
        ],
    )


@pytest.mark.asyncio
async def test_terminal_run_summarizes_all_touched_objectives():
    projections = [
        ExecutionResultProjection(
            execution_id="e-java", task_id="task-1", conversation_id="c", run_id="r",
            objective_id="java", artifacts=[{"resource_type": "DRAFT", "resource_id": "d-java"}],
        ),
        ExecutionResultProjection(
            execution_id="e-agent", task_id="task-1", conversation_id="c", run_id="r",
            objective_id="agent", artifacts=[{"resource_type": "DRAFT", "resource_id": "d-agent"}],
        ),
        ExecutionResultProjection(
            execution_id="e-agent-schedule", task_id="task-1", conversation_id="c", run_id="r",
            objective_id="agent", artifacts=[{"resource_type": "SCHEDULE", "resource_id": "s-agent"}],
        ),
    ]
    content = await _final_response_projection(
        {"run_id": "r", "conversation_id": "c", "task_id": "task-1", "status": "COMPLETED", "content": "last step"},
        projections,
        request=_request(_task()),
        auth=_auth(),
    )
    assert "Java内容已创建并安排" in content
    assert "Agent内容已创建并安排" in content


@pytest.mark.asyncio
async def test_cross_turn_only_reports_changed_objective():
    task = _task()
    projections = [
        ExecutionResultProjection(
            execution_id="e-java-update", task_id="task-1", conversation_id="c", run_id="r2",
            objective_id="java", artifacts=[{"resource_type": "SCHEDULE", "resource_id": "s-java"}],
        ),
    ]
    content = await _final_response_projection(
        {"run_id": "r2", "conversation_id": "c", "task_id": "task-1", "status": "COMPLETED", "content": "last step"},
        projections,
        request=_request(task),
        auth=_auth(),
    )
    assert "Java的发布时间已更新为" in content
    assert "Agent" not in content


@pytest.mark.asyncio
async def test_terminal_response_uses_verified_published_resource_state():
    task = Task(
        task_id="task-published-response",
        conversation_id="c",
        user_id="u",
        tenant_id="t",
        status=TaskStatus.COMPLETED,
        objectives=[Objective(
            task_id="task-published-response",
            objective_id="publish",
            intent="Java post",
            status=ObjectiveStatus.COMPLETED,
            related_resource_ids=["post-java"],
        )],
        resource_index=[TaskResourceRef(
            resource_id="post-java",
            resource_kind="POST",
            status="PUBLISHED",
            title="Java post",
        )],
    )
    content = await _final_response_projection(
        {
            "run_id": "r-published",
            "conversation_id": "c",
            "task_id": task.task_id,
            "status": "COMPLETED",
            "content": "fallback",
        },
        [ExecutionResultProjection(
            execution_id="e-published",
            task_id=task.task_id,
            conversation_id="c",
            run_id="r-published",
            objective_id="publish",
            artifacts=[{"resource_type": "POST", "resource_id": "post-java"}],
        )],
        request=_request(task),
        auth=_auth(),
    )

    assert "Java post已发布" in content


@pytest.mark.asyncio
async def test_superseded_objective_is_not_rendered_as_user_facing_work():
    task = Task(
        task_id="task-supersede-response",
        conversation_id="c",
        user_id="u",
        tenant_id="t",
        status=TaskStatus.COMPLETED,
        objectives=[
            Objective(
                task_id="task-supersede-response",
                objective_id="old",
                intent="Old schedule",
                status=ObjectiveStatus.SUPERSEDED,
                constraints={"mutation_status": "SUPERSEDED"},
                related_resource_ids=["schedule-old"],
            ),
            Objective(
                task_id="task-supersede-response",
                objective_id="new",
                intent="New draft",
                status=ObjectiveStatus.COMPLETED,
                related_resource_ids=["draft-new"],
            ),
        ],
        resource_index=[
            TaskResourceRef(resource_id="schedule-old", resource_kind="SCHEDULE"),
            TaskResourceRef(resource_id="draft-new", resource_kind="DRAFT"),
        ],
    )
    projections = [
        ExecutionResultProjection(
            execution_id="e-old",
            task_id=task.task_id,
            conversation_id="c",
            run_id="r-supersede",
            objective_id="old",
            artifacts=[{"resource_type": "SCHEDULE", "resource_id": "schedule-old"}],
        ),
        ExecutionResultProjection(
            execution_id="e-new",
            task_id=task.task_id,
            conversation_id="c",
            run_id="r-supersede",
            objective_id="new",
            artifacts=[{"resource_type": "DRAFT", "resource_id": "draft-new"}],
        ),
    ]

    content = await _final_response_projection(
        {
            "run_id": "r-supersede",
            "conversation_id": "c",
            "task_id": task.task_id,
            "status": "COMPLETED",
            "content": "fallback",
        },
        projections,
        request=_request(task),
        auth=_auth(),
    )

    assert "Old schedule" not in content
    assert "New draft" in content


@pytest.mark.asyncio
async def test_nonterminal_run_keeps_step_projection():
    content = await _final_response_projection(
        {"run_id": "r", "conversation_id": "c", "task_id": "task-1", "status": "WAITING_USER", "content": "请确认"},
        [],
        request=_request(_task()),
        auth=_auth(),
    )
    assert content == "请确认"
