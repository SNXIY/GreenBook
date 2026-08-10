"""Runtime migration E2E coverage from HTTP message entry to PlanExecution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from greenbook_contracts.identity import AuthContext
from greenbook_assistant_core.compatibility.history import RunExecutionAdapter
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    IntentAction,
    IntentConstraint,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.intent_spec_provider import IntentSpecProvider
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus

from apps.assistant_api.greenbook_assistant_api.main import create_app
from apps.assistant_api.greenbook_assistant_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from apps.assistant_api.greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from apps.assistant_api.greenbook_assistant_api.services.task_provider import TaskProvider


AUTH_HEADERS = {"Authorization": "Bearer runtime-e2e-token"}
CREATE_MESSAGE = "帮我写一篇AI Agent学习路线帖子"
UPDATE_MESSAGE = "把刚才那个帖子改短一点"
SCHEDULE_MESSAGE = "五分钟之后发布一篇关于如何学好 Java 的帖子"


class _InMemoryTaskRegistry:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.create_count = 0

    async def create_task(self, **kwargs: Any) -> Task:
        self.create_count += 1
        task = Task(**kwargs)
        self.tasks[task.task_id] = task
        return task

    async def list_tasks(self, conversation_id: str) -> list[Task]:
        return [
            task
            for task in self.tasks.values()
            if task.conversation_id == conversation_id
        ]

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)


class _ScenarioUnderstanding:
    """Deterministic L1/L2 understanding for the HTTP contract tests."""

    async def understand(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> TaskIntent:
        del existing_tasks
        if "改短" in user_message:
            spec = IntentSpec(
                mode=IntentMode.SIMPLE,
                goal=user_message,
                actions=[
                    IntentAction(
                        action=ActionType.UPDATE,
                        resource=ResourceType.CONTENT,
                        confidence=0.95,
                    )
                ],
                target_hint="帖子",
                confidence=0.95,
                source="L2",
            )
            return TaskIntent(
                relation="MODIFY_TASK",
                goal=user_message,
                goal_category="IMPROVE_CONTENT",
                target_task_hint="帖子",
                requirements=[{"type": "IMPROVE"}],
                source="L2",
                intent_spec=spec.model_dump(mode="json"),
            )

        if "五分钟" in user_message:
            spec = IntentSpec(
                mode=IntentMode.COMPOSITE,
                goal=user_message,
                actions=[
                    IntentAction(
                        action=ActionType.CREATE,
                        resource=ResourceType.CONTENT,
                        confidence=0.95,
                    ),
                    IntentAction(
                        action=ActionType.PUBLISH,
                        resource=ResourceType.SCHEDULE,
                        confidence=0.95,
                    ),
                ],
                constraints=[
                    IntentConstraint(type="TIME", value="五分钟之后"),
                ],
                confidence=0.95,
                source="L2",
            )
            return TaskIntent(
                relation="NEW_TASK",
                goal=user_message,
                goal_category="CREATE_CONTENT",
                requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
                constraints=[{"type": "TIME", "value": "五分钟之后"}],
                source="L2",
                intent_spec=spec.model_dump(mode="json"),
            )

        return TaskIntent(
            relation="NEW_TASK",
            goal=user_message,
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}],
            source="L1",
            confidence=0.95,
        )


class _FakeMCP:
    def __init__(self) -> None:
        self.fail_create = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "content.create_draft",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["title", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "content.revise_draft",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_id": {"type": "string"},
                            "revision_instruction": {"type": "string"},
                        },
                        "required": ["draft_id", "revision_instruction"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "publication.schedule",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "draft_id": {"type": "string"},
                            "run_at": {"type": "string"},
                            "timezone": {"type": "string"},
                        },
                        "required": ["draft_id", "run_at"],
                    },
                },
            },
        ]

    async def execute_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, dict(kwargs)))
        if name == "content.create_draft" and self.fail_create:
            return {
                "ok": False,
                "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                "message": "title and content are required",
                "user_message": "The draft arguments are invalid.",
            }
        if name == "content.create_draft":
            return {
                "ok": True,
                "code": "",
                "data": {
                    "draft_id": "draft-runtime-e2e",
                    "title": "AI Agent 学习路线",
                    "content": "从基础概念到项目实战的学习路线。",
                },
            }
        if name == "content.revise_draft":
            return {
                "ok": True,
                "code": "",
                "data": {
                    "draft_id": kwargs.get("draft_id"),
                    "title": "AI Agent 学习路线（精简版）",
                    "content": "精简后的学习路线。",
                },
            }
        return {
            "ok": True,
            "code": "",
            "data": {
                "schedule_id": "schedule-runtime-e2e",
                "draft_id": kwargs.get("draft_id") or "draft-runtime-e2e",
                "run_at": kwargs.get("run_at"),
                "timezone": kwargs.get("timezone", "Asia/Shanghai"),
                "status": "SCHEDULED",
            },
        }


class _Harness:
    def __init__(self) -> None:
        ExecutionRepository.clear()
        self.repository = ExecutionRepository()
        self.event_store = ExecutionEventStore()
        self.state_manager = ExecutionStateManager(
            repository=self.repository,
            event_store=self.event_store,
        )
        self.registry = _InMemoryTaskRegistry()
        self.mcp = _FakeMCP()

        @asynccontextmanager
        async def session_context():
            yield object()

        task_provider = TaskProvider(
            session_context_factory=session_context,
            registry_factory=lambda _session: self.registry,
        )
        intent_provider = IntentSpecProvider(_ScenarioUnderstanding())
        runtime_service = RuntimeAgentService(
            repository=self.repository,
            event_store=self.event_store,
        )
        self.adapter = ConversationRuntimeAdapter(
            intent_provider=intent_provider,
            task_provider=task_provider,
            runtime_service=runtime_service,
            execution_repository=self.repository,
        )

        def validate(token: str) -> AuthContext:
            return AuthContext(
                user_id="user-runtime-e2e",
                tenant_id="tenant-runtime-e2e",
                raw_access_token=token,
            )

        self.app = create_app(auth_validator=validate)
        self.app.state.runtime_enabled = True
        self.app.state.execution_mode = "runtime"
        self.app.state.conversation_runtime_adapter = self.adapter
        self.app.state.runtime_agent_service = runtime_service
        self.app.state.execution_repository = self.repository
        self.app.state.execution_event_store = self.event_store
        self.app.state.execution_state_manager = self.state_manager
        self.app.state.execution_runtime_manager = RuntimeManager(
            state_manager=self.state_manager,
        )
        self.app.state.execution_authorizer = (
            lambda auth, execution: any(
                task.task_id == execution.task_id
                and task.user_id == auth.user_id
                and task.tenant_id == auth.tenant_id
                for task in self.registry.tasks.values()
            )
        )
        self.app.state.mcp = self.mcp
        self.app.state.model = "runtime-e2e"
        self.app.state.llm = None
        self.app.state.conversation_store = {}
        self.app.state.message_store = {}
        self.app.state.run_store = {}
        self.app.state.approval_store = {}
        self.app.state.run_execution_adapter = RunExecutionAdapter()
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def harness() -> Any:
    value = _Harness()
    try:
        yield value
    finally:
        value.close()


def _conversation(harness: _Harness) -> str:
    response = harness.client.post(
        "/api/v1/assistant/conversations",
        json={"title": "Runtime migration E2E"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation_id"]


def _message(harness: _Harness, conversation_id: str, content: str) -> dict[str, Any]:
    response = harness.client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_create_content_http_chain_reaches_runtime_execution(harness: _Harness) -> None:
    conversation_id = _conversation(harness)
    accepted = _message(harness, conversation_id, CREATE_MESSAGE)

    assert accepted["execution_id"]
    assert accepted["status"] == "COMPLETED"
    run = harness.app.state.run_store[accepted["run_id"]]
    assert run["intent_spec"]["source"] == "L1"
    assert run["intent_spec"]["actions"][0]["action"] == "CREATE"
    assert run["task_id"]
    assert run["plan_id"]
    assert len(harness.registry.tasks) == 1
    task = next(iter(harness.registry.tasks.values()))
    assert task.status == TaskStatus.READY

    execution = harness.repository.find_by_id(accepted["execution_id"])
    assert execution is not None
    assert execution.status.value == "COMPLETED"
    assert execution.task_id == task.task_id
    assert any(step.capability == "GENERATE_CONTENT" for step in execution.steps)


def test_update_existing_task_resolves_without_creating_new_task(harness: _Harness) -> None:
    conversation_id = _conversation(harness)
    first = _message(harness, conversation_id, CREATE_MESSAGE)
    task_id = next(iter(harness.registry.tasks))

    # The legacy conversation projection still carries this resource binding;
    # Runtime remains the source of execution state and task resolution.
    harness.app.state.conversation_store[conversation_id]["active_draft_id"] = (
        "draft-runtime-e2e"
    )
    second = _message(harness, conversation_id, UPDATE_MESSAGE)

    assert first["execution_id"] != second["execution_id"]
    assert second["status"] == "COMPLETED"
    assert len(harness.registry.tasks) == 1
    assert harness.registry.create_count == 1
    update_run = harness.app.state.run_store[second["run_id"]]
    assert update_run["task_id"] == task_id
    assert update_run["intent_spec"]["actions"][0]["action"] == "UPDATE"
    update_execution = harness.repository.find_by_id(second["execution_id"])
    assert update_execution is not None
    assert update_execution.task_id == task_id
    assert any(step.capability == "IMPROVE_CONTENT" for step in update_execution.steps)


def test_schedule_publish_binds_canonical_run_at_and_execution(harness: _Harness) -> None:
    conversation_id = _conversation(harness)
    accepted = _message(harness, conversation_id, SCHEDULE_MESSAGE)

    assert accepted["status"] == "COMPLETED"
    run = harness.app.state.run_store[accepted["run_id"]]
    assert run["intent_spec"]["constraints"][0]["type"] == "TIME"
    execution = harness.repository.find_by_id(accepted["execution_id"])
    assert execution is not None
    schedule_step = next(
        step for step in execution.steps if step.capability == "SCHEDULE_PUBLISH"
    )
    run_at = schedule_step.checkpoint_data["constraints"].get("run_at")
    assert run_at
    parsed = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    schedule_calls = [
        kwargs for name, kwargs in harness.mcp.calls
        if name == "publication.schedule"
    ]
    assert schedule_calls
    assert schedule_calls[-1]["run_at"] == run_at


def test_failed_runtime_is_returned_as_failed_and_not_overwritten(harness: _Harness) -> None:
    harness.mcp.fail_create = True
    conversation_id = _conversation(harness)
    accepted = _message(harness, conversation_id, CREATE_MESSAGE)

    assert accepted["status"] == "FAILED"
    assert accepted["error_code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert accepted["execution_id"]
    run = harness.app.state.run_store[accepted["run_id"]]
    assert run["status"] == "FAILED"
    assert run["error_code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"

    # A compatibility-shaped success record must not become a Runtime source.
    harness.app.state.assistant_runs = {
        accepted["run_id"]: {"status": "COMPLETED", "final_response": "已完成"}
    }
    status_response = harness.client.get(
        f"/api/v1/executions/{accepted['execution_id']}",
        headers=AUTH_HEADERS,
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "FAILED"
    steps = harness.client.get(
        f"/api/v1/executions/{accepted['execution_id']}/steps",
        headers=AUTH_HEADERS,
    )
    assert steps.status_code == 200
    assert steps.json()["steps"][0]["error_code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"


def test_http_execution_status_steps_and_events_contract(harness: _Harness) -> None:
    conversation_id = _conversation(harness)
    accepted = _message(harness, conversation_id, CREATE_MESSAGE)
    execution_id = accepted["execution_id"]

    status_response = harness.client.get(
        f"/api/v1/executions/{execution_id}",
        headers=AUTH_HEADERS,
    )
    steps_response = harness.client.get(
        f"/api/v1/executions/{execution_id}/steps",
        headers=AUTH_HEADERS,
    )
    events_response = harness.client.get(
        f"/api/v1/executions/{execution_id}/events",
        headers=AUTH_HEADERS,
    )
    list_response = harness.client.get(
        "/api/v1/executions",
        headers=AUTH_HEADERS,
    )

    assert status_response.status_code == 200
    assert steps_response.status_code == 200
    assert events_response.status_code == 200
    assert list_response.status_code == 200
    assert status_response.json()["execution_id"] == execution_id
    assert steps_response.json()["execution_id"] == execution_id
    assert steps_response.json()["steps"]
    assert events_response.json()["execution_id"] == execution_id
    event_types = {event["event_type"] for event in events_response.json()["events"]}
    # RuntimeAgentService starts the PlanExecution after Worker initialisation;
    # the public event contract must still expose the lifecycle start/end.
    assert "EXECUTION_COMPLETED" in event_types
    assert "EXECUTION_STARTED" in event_types
    assert any(item["execution_id"] == execution_id for item in list_response.json()["items"])
