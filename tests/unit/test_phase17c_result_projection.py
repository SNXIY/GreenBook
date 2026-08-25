"""Phase17-C durable result projection and structured response tests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from greenbook_agent_api.models.runtime_result import RuntimeResult
from greenbook_agent_api.services.completion_projection_coordinator import (
    CompletionProjectionCoordinator,
)
from greenbook_agent_api.services.result_resolver import ResultResolver
from greenbook_agent_api.services.task_provider import TaskProvider, TaskScope
from greenbook_agent_core.artifact.store import (
    MemoryArtifactStore,
    PostgresArtifactStore,
)
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.models import ArtifactHandle
from greenbook_agent_core.execution.result_projection import (
    PostgresExecutionResultProjectionStore,
)
from greenbook_agent_core.task.models import Task, TaskGoal, TaskStatus
from greenbook_contracts.identity import AuthContext


def _draft_result() -> ExecutionResult:
    return ExecutionResult.success(
        capability="GENERATE_CONTENT",
        tool_name="content.create_draft",
        tool_result={
            "data": {
                "draft_id": "draft-java-17c",
                "title": "Java 学习路线：从基础到实践",
                "summary": "覆盖核心语法、项目练习和持续复盘。",
                "content": "正文不允许进入 PostgreSQL Artifact 元数据。",
                "status": "draft",
            }
        },
        artifact=ArtifactHandle(
            artifact_type="DRAFT",
            resource_id="draft-java-17c",
            summary="Java 学习路线：从基础到实践",
        ),
    )


def _schedule_result() -> ExecutionResult:
    return ExecutionResult.success(
        capability="SCHEDULE_PUBLISH",
        tool_name="publication.schedule",
        tool_result={
            "data": {
                "draft_id": "draft-java-17c",
                "schedule_id": "schedule-java-17c",
                "run_at": "2026-08-12T00:00:00Z",
                "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
            }
        },
        artifact=ArtifactHandle(
            artifact_type="SCHEDULE",
            resource_id="schedule-java-17c",
            summary="2026-08-12 08:00",
        ),
    )


def test_typed_artifact_resource_id_never_guesses_from_field_order() -> None:
    schedule = CapabilityExecutor._extract_artifact(
        "SCHEDULE_PUBLISH",
        "SCHEDULE",
        {
            "data": {
                "draft_id": "draft-wrong-for-schedule",
                "schedule_id": "schedule-correct",
                "post_id": "post-wrong-for-schedule",
            }
        },
    )
    draft = CapabilityExecutor._extract_artifact(
        "GENERATE_CONTENT",
        "DRAFT",
        {"data": {"draft_id": "draft-correct", "schedule_id": "schedule-wrong"}},
    )
    post = CapabilityExecutor._extract_artifact(
        "PUBLISH_CONTENT",
        "POST",
        {"data": {"draft_id": "draft-wrong", "post_id": "post-correct"}},
    )

    assert schedule is not None and schedule.resource_id == "schedule-correct"
    assert draft is not None and draft.resource_id == "draft-correct"
    assert post is not None and post.resource_id == "post-correct"


def test_artifact_resource_id_projects_from_canonical_resource_ref() -> None:
    draft = CapabilityExecutor._extract_artifact(
        "GENERATE_CONTENT",
        "DRAFT",
        {
            "data": {"title": "draft"},
            "resource_refs": [
                {"kind": "DRAFT", "resource_id": "draft-from-ref"},
            ],
        },
    )

    assert draft is not None and draft.resource_id == "draft-from-ref"


def test_memory_and_postgres_artifacts_expose_the_same_body_free_result_fields() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    memory = MemoryArtifactStore()
    postgres = PostgresArtifactStore(engine)

    memory_draft = memory.create_from_result(
        _draft_result(), task_id="task-17c", execution_id="execution-17c", step_id="draft"
    )
    postgres_draft = postgres.create_from_result(
        _draft_result(), task_id="task-17c", execution_id="execution-17c", step_id="draft"
    )
    postgres_schedule = postgres.create_from_result(
        _schedule_result(), task_id="task-17c", execution_id="execution-17c", step_id="schedule"
    )

    assert memory_draft is not None and postgres_draft is not None
    fields = ("title", "summary", "resource_type", "resource_id", "status", "run_at", "timezone")
    assert {field: getattr(memory_draft, field) for field in fields} == {
        field: getattr(postgres_draft, field) for field in fields
    }
    restarted = PostgresArtifactStore(engine, create_tables=False)
    restored_draft = restarted.get(postgres_draft.artifact_id)
    restored_schedule = restarted.get(postgres_schedule.artifact_id)
    assert restored_draft is not None
    assert restored_schedule is not None
    assert restored_draft.title == "Java 学习路线：从基础到实践"
    assert restored_draft.resource_id == "draft-java-17c"
    assert restored_schedule.resource_id == "schedule-java-17c"
    assert restored_schedule.run_at == "2026-08-12T00:00:00Z"
    assert restored_schedule.metadata["projection"]["draft_id"] == "draft-java-17c"
    assert "content" not in restored_draft.metadata
    assert "content" not in restored_draft.metadata["projection"]
    engine.dispose()


class _ConversationService:
    def __init__(self) -> None:
        self.messages = [{
            "message_id": "user-message",
            "role": "user",
            "content": "明天上午八点发布一篇关于如何学好 Java 的帖子",
            "trace_id": "trace-17c",
            "parts": [],
        }]
        self.session = SessionContext(
            conversation_id="conversation-17c",
            user_id="user-17c",
            tenant_id="tenant-17c",
            timezone="Asia/Shanghai",
        )

    async def list_messages(self, _conversation_id: str, **_scope):
        return [dict(item) for item in self.messages]

    async def append_message(self, _conversation_id: str, **message):
        self.messages.append(dict(message))

    async def update_message_projection(self, _conversation_id: str, **message):
        existing = next(item for item in self.messages if item.get("role") == "assistant")
        existing.update(message)
        return True

    async def load(self, _conversation_id: str, **_scope):
        return SimpleNamespace(session=self.session)

    async def save_session(self, session: SessionContext):
        self.session = session
        return session.model_dump(mode="json")


class _TaskProvider:
    def __init__(self) -> None:
        self.completions: list[dict] = []

    async def persist_completion_projection(self, _scope, **fields):
        self.completions.append(dict(fields))
        return fields


@pytest.mark.asyncio
async def test_java_post_completion_is_durable_and_publishes_structured_message() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    artifacts = PostgresArtifactStore(engine)
    draft = artifacts.create_from_result(
        _draft_result(), task_id="task-17c", execution_id="execution-17c", step_id="draft"
    )
    schedule = artifacts.create_from_result(
        _schedule_result(), task_id="task-17c", execution_id="execution-17c", step_id="schedule"
    )
    projection_store = PostgresExecutionResultProjectionStore(engine)
    context = _ConversationService()
    tasks = _TaskProvider()
    coordinator = CompletionProjectionCoordinator(
        conversation_service=context,
        result_projection_store=projection_store,
        result_resolver=ResultResolver(artifact_store=artifacts),
        task_provider=tasks,
        run_store={},
    )
    queue_message = ExecutionQueueMessage(
        execution_id="execution-17c",
        trace_id="trace-17c",
        payload={
            "run_id": "run-17c",
            "task_id": "task-17c",
            "conversation_id": "conversation-17c",
            "auth_context": {
                "user_id": "user-17c",
                "tenant_id": "tenant-17c",
                "timezone": "Asia/Shanghai",
            },
        },
    )
    runtime_result = RuntimeResult(
        success=True,
        status="COMPLETED",
        run_id="run-17c",
        task_id="task-17c",
        execution_id="execution-17c",
        trace_id="trace-17c",
        summary="生成 Java 学习帖子并安排发布",
    )
    auth = AuthContext(
        user_id="user-17c",
        tenant_id="tenant-17c",
        timezone="Asia/Shanghai",
        raw_access_token="validated-token",
    )

    published = await coordinator.complete(queue_message, runtime_result, auth)

    assert published is True
    projection = projection_store.get("execution-17c")
    assert projection is not None
    assert projection.task_id == "task-17c"
    assert {item["artifact_id"] for item in projection.artifacts} == {
        draft.artifact_id,
        schedule.artifact_id,
    }
    assistant_message = context.messages[-1]
    serialized_message = json.dumps(assistant_message, ensure_ascii=False)
    assert "Java 学习路线：从基础到实践" in serialized_message
    assert "draft-java-17c" in serialized_message
    assert "schedule-java-17c" in serialized_message
    assert "2026-08-12T00:00:00Z" in serialized_message
    assert assistant_message["parts"][0]["type"] == "execution_result"
    assert assistant_message["parts"][0]["execution"]["business_projection"]["state"] == "SCHEDULED"
    assert tasks.completions[0]["status"] == "COMPLETED"
    assert context.session.active_draft_id == "draft-java-17c"
    assert context.session.active_schedule_id == "schedule-java-17c"
    assert context.session.last_successful_run_id == "run-17c"

    # A new process/store instance recovers the same durable projection.
    restarted = PostgresExecutionResultProjectionStore(engine, create_tables=False)
    recovered = restarted.get("execution-17c")
    assert recovered is not None
    assert recovered.assistant_response["message"] == assistant_message["content"]
    assert recovered.schedule["schedule_id"] == "schedule-java-17c"
    engine.dispose()


def test_phase17c_migrations_are_asyncpg_safe_single_statements() -> None:
    root = Path(__file__).parents[2]
    migration_dir = (
        root / "packages" / "agent_core" / "greenbook_agent_core" / "db" / "migrations"
    )
    for name in (
        "004_structured_assistant_messages.sql",
        "005_artifact_result_projection_fields.sql",
    ):
        sql = "\n".join(
            line for line in (migration_dir / name).read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        ).strip()
        assert sql.count(";") == 1
        assert sql.endswith(";")


@pytest.mark.asyncio
async def test_task_completion_projection_updates_terminal_refs_and_resources() -> None:
    task = Task(
        task_id="task-17c",
        conversation_id="conversation-17c",
        user_id="user-17c",
        tenant_id="tenant-17c",
        goal="生成 Java 学习帖子",
        status=TaskStatus.READY,
    )

    class Registry:
        async def get_task(self, task_id: str):
            return task if task_id == task.task_id else None

        async def update_task(self, task_id: str, **fields):
            assert task_id == task.task_id
            values = task.model_dump(mode="python")
            values.update(fields)
            completed_at = values.get("completed_at")
            if hasattr(completed_at, "isoformat"):
                values["completed_at"] = completed_at.isoformat()
            return Task.model_validate(values)

    @asynccontextmanager
    async def sessions():
        yield object()

    provider = TaskProvider(
        session_context_factory=sessions,
        registry_factory=lambda _session: Registry(),
    )
    completed = await provider.persist_completion_projection(
        TaskScope(
            user_id="user-17c",
            tenant_id="tenant-17c",
            conversation_id="conversation-17c",
        ),
        task_id="task-17c",
        execution_id="execution-17c",
        status="COMPLETED",
        artifacts=[
            {
                "artifact_id": "artifact-draft-17c",
                "artifact_type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": "draft-java-17c",
                "title": "Java 学习路线：从基础到实践",
                "summary": "覆盖核心语法、项目练习和持续复盘。",
            },
            {
                "artifact_id": "artifact-schedule-17c",
                "artifact_type": "SCHEDULE",
                "resource_type": "SCHEDULE",
                "resource_id": "schedule-java-17c",
                "run_at": "2026-08-12T00:00:00Z",
                "status": "SCHEDULED",
            },
        ],
    )

    assert completed is not None
    assert completed.status == TaskStatus.COMPLETED
    assert completed.execution_refs[0].status == "COMPLETED"
    assert {item.resource_id for item in completed.resource_index} == {
        "draft-java-17c",
        "schedule-java-17c",
    }
    assert {item.artifact_id for item in completed.artifacts} == {
        "artifact-draft-17c",
        "artifact-schedule-17c",
    }


@pytest.mark.asyncio
async def test_task_completion_projection_updates_goal_statuses() -> None:
    task = Task(
        task_id="task-goal-projection",
        conversation_id="conversation-goal-projection",
        user_id="user-goal-projection",
        tenant_id="tenant-goal-projection",
        goal="运营 Java 专题",
        status=TaskStatus.RUNNING,
        goals=[
            TaskGoal(
                task_id="task-goal-projection",
                goal_id="research",
                description="分析社区趋势",
            ),
            TaskGoal(
                task_id="task-goal-projection",
                goal_id="publish",
                description="安排发布",
            ),
        ],
    )

    class Registry:
        async def get_task(self, task_id: str):
            return task if task_id == task.task_id else None

        async def update_task(self, task_id: str, **fields):
            assert task_id == task.task_id
            values = task.model_dump(mode="python")
            values.update(fields)
            completed_at = values.get("completed_at")
            if hasattr(completed_at, "isoformat"):
                values["completed_at"] = completed_at.isoformat()
            return Task.model_validate(values)

    @asynccontextmanager
    async def sessions():
        yield object()

    provider = TaskProvider(
        session_context_factory=sessions,
        registry_factory=lambda _session: Registry(),
    )
    completed = await provider.persist_completion_projection(
        TaskScope(
            user_id="user-goal-projection",
            tenant_id="tenant-goal-projection",
            conversation_id="conversation-goal-projection",
        ),
        task_id="task-goal-projection",
        execution_id="execution-goal-projection",
        status="COMPLETED",
        artifacts=[],
    )

    assert completed is not None
    assert {goal.status for goal in completed.goals} == {"COMPLETED"}
    assert {goal.execution_id for goal in completed.goals} == {
        "execution-goal-projection"
    }


@pytest.mark.asyncio
async def test_stale_execution_completion_does_not_overwrite_current_task() -> None:
    task = Task(
        task_id="task-stale-completion",
        conversation_id="conversation-stale-completion",
        user_id="user-stale-completion",
        tenant_id="tenant-stale-completion",
        goal="当前任务",
        status=TaskStatus.FAILED,
        active_execution_id="execution-current",
        last_error="current execution failed",
        goals=[
            TaskGoal(
                task_id="task-stale-completion",
                goal_id="current-goal",
                description="当前目标",
                status="FAILED",
                execution_id="execution-current",
            )
        ],
    )

    class Registry:
        async def get_task(self, task_id: str):
            return task if task_id == task.task_id else None

        async def update_task(self, task_id: str, **fields):
            values = task.model_dump(mode="python")
            values.update(fields)
            completed_at = values.get("completed_at")
            if hasattr(completed_at, "isoformat"):
                values["completed_at"] = completed_at.isoformat()
            return Task.model_validate(values)

    @asynccontextmanager
    async def sessions():
        yield object()

    provider = TaskProvider(
        session_context_factory=sessions,
        registry_factory=lambda _session: Registry(),
    )
    projected = await provider.persist_completion_projection(
        TaskScope(
            user_id="user-stale-completion",
            tenant_id="tenant-stale-completion",
            conversation_id="conversation-stale-completion",
        ),
        task_id="task-stale-completion",
        execution_id="execution-old",
        status="COMPLETED",
        artifacts=[],
    )

    assert projected is not None
    assert projected.status == TaskStatus.FAILED
    assert projected.last_error == "current execution failed"
    assert projected.active_execution_id == "execution-current"
    assert projected.goals[0].status == "FAILED"
    assert projected.goals[0].execution_id == "execution-current"
