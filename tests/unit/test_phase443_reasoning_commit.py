"""Phase 4.4.3 offline contracts: durable reasoning/search facts and convergence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_core.agent.actions import AgentAction
from greenbook_agent_core.agent.loop import _no_progress_detected
from greenbook_agent_core.agent.state import AgentState
from greenbook_agent_core.artifact.models import Artifact
from greenbook_agent_core.execution.action_observation import ActionObservationStore
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.goal.ready_work import select_ready_work
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.models import Task

from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)


class _Projector:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def persist_completion_projection(self, _scope, **fields):
        self.calls.append(fields)
        return None


def _adapter():
    container = RuntimeContainer.for_testing()
    repository = ExecutionRepository()
    observations = ActionObservationStore()
    projector = _Projector()
    runtime_service = SimpleNamespace(
        container=container,
        _execution_repository=repository,
        _artifact_store=container.artifact_store,
    )
    adapter = ConversationRuntimeAdapter(
        container=container,
        execution_repository=repository,
        observation_store=observations,
        task_provider=projector,
        runtime_service=runtime_service,
    )
    return adapter, container, repository, observations, projector


@pytest.mark.asyncio
async def test_reasoning_result_commits_all_fact_sources_and_replays_idempotently() -> None:
    adapter, container, repository, observations, projector = _adapter()
    source = Artifact(
        artifact_id="search-artifact",
        task_id="task-1",
        execution_id="search-exec",
        owner_task_id="task-1",
        owner_execution_id="search-exec",
        created_by_agent="SEARCH_COMMUNITY",
        step_id="g2:search",
        artifact_type="SEARCH_RESULT",
        summary="18 community posts",
        metadata={"total": 18},
    )
    container.artifact_store.create(source)

    kwargs = {
        "goal_id": "g3",
        "capability": "ANALYZE_CONTENT_PATTERNS",
        "result_type": "CONTENT_ANALYSIS",
        "payload": {
            "summary": "Community discussion centers on agent reliability.",
            "key_points": ["runtime discipline", "evidence"],
        },
        "source_refs": ["search-artifact"],
        "task_id": "task-1",
        "conversation_id": "conversation-1",
        "user_id": "user-1",
        "tenant_id": "tenant-1",
    }
    first = await adapter.record_reasoning_result(**kwargs)
    second = await adapter.record_reasoning_result(**kwargs)

    assert first == second
    assert repository.find_by_id(first["execution_id"]) is not None
    assert container.artifact_store.get(first["artifact_id"]) is not None
    assert observations.get_by_execution(first["execution_id"]) is not None
    assert [item for item in repository.list_all() if item.task_id == "task-1"] == [
        repository.find_by_id(first["execution_id"])
    ]
    task_artifacts = container.artifact_store.find_by_task("task-1")
    assert len(task_artifacts) == 2  # source Search + one Analysis report
    assert sum(item.artifact_type == "ANALYSIS_REPORT" for item in task_artifacts) == 1
    assert len(observations.list_pending()) == 0
    assert len(projector.calls) == 2
    assert projector.calls[0]["goal_id"] == "g3"


@pytest.mark.asyncio
async def test_reasoning_result_accepts_task_resource_source_refs() -> None:
    """Real-chain regression: PRODUCE_RESULT cites the concrete posts
    GET_POST_DETAIL read (resource ids, not artifact ids).  The lineage commit
    must accept resources already recorded on the Task's resource_index;
    otherwise the analysis Goal fails REASONING_RESULT_COMMIT_FAILED with
    unknown source_refs."""
    class _TaskProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def persist_completion_projection(self, _scope, **fields):
            return None

        async def get_task(self, _scope, task_id):
            self.calls += 1
            return SimpleNamespace(
                task_id=task_id,
                resource_index=[
                    SimpleNamespace(resource_id="346113133389156352", resource_kind="POST"),
                    SimpleNamespace(resource_id="346103424238096384", resource_kind="POST"),
                ],
            )

    container = RuntimeContainer.for_testing()
    repository = ExecutionRepository()
    observations = ActionObservationStore()
    runtime_service = SimpleNamespace(
        container=container,
        _execution_repository=repository,
        _artifact_store=container.artifact_store,
    )
    provider = _TaskProvider()
    adapter = ConversationRuntimeAdapter(
        container=container,
        execution_repository=repository,
        observation_store=observations,
        task_provider=provider,
        runtime_service=runtime_service,
    )

    result = await adapter.record_reasoning_result(
        goal_id="analyze_java_patterns",
        capability="ANALYZE_CONTENT_PATTERNS",
        result_type="CONTENT_ANALYSIS",
        payload={"summary": "Java 学习共同方法", "key_points": ["a", "b"]},
        source_refs=["346113133389156352", "346103424238096384"],
        task_id="task-java",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )

    assert result["execution_id"].startswith("reasoning:task-java")
    assert container.artifact_store.get(result["artifact_id"]) is not None
    assert provider.calls >= 1


@pytest.mark.asyncio
async def test_search_direct_result_becomes_durable_execution_and_artifact() -> None:
    adapter, container, repository, observations, projector = _adapter()
    state = AgentState(
        task=Task(
            task_id="task-1",
            conversation_id="conversation-1",
            user_id="user-1",
            tenant_id="tenant-1",
            goal="search",
        ),
        current_task=TaskNode(
            task_id="task-1:g2",
            goal_id="g2",
            capability="SEARCH_COMMUNITY",
        ),
        conversation_context={
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        },
    )
    result = await adapter.record_tool_result(
        state=state,
        action=AgentAction(
            action="TOOL_CALL",
            tool_name="community.search_public_posts",
            tool_args={"query": "Agent"},
        ),
        result={
            "ok": True,
            "tool_name": "community.search_public_posts",
            "tool_arguments": {"query": "Agent"},
            "data": {
                "total": 2,
                "items": [{"post_id": "p1"}, {"post_id": "p2"}],
            },
        },
    )

    assert result["artifact_type"] == "SEARCH_RESULT"
    execution = repository.find_by_id(result["execution_id"])
    assert execution is not None
    assert execution.status == "COMPLETED"
    assert execution.steps[0].goal_id == "g2"
    artifact = container.artifact_store.get(result["artifact_id"])
    assert artifact is not None
    assert artifact.artifact_type == "SEARCH_RESULT"
    assert artifact.metadata["resource_refs"] == [
        {"kind": "POST", "resource_id": "p1"},
        {"kind": "POST", "resource_id": "p2"},
    ]
    assert observations.get_by_execution(result["execution_id"]) is not None
    assert state.context_snapshot["artifacts"]
    assert state.context_snapshot["execution_states"]
    assert projector.calls[0]["goal_id"] == "g2"


def test_analysis_satisfaction_unlocks_dependent_goal_without_workflow_special_case() -> None:
    tree = GoalTree(root=Goal(
        goal_id="root",
        description="research then write",
        children=[
            Goal(
                goal_id="g2",
                description="search",
                required_capabilities=["SEARCH_COMMUNITY"],
            ),
            Goal(
                goal_id="g3",
                description="analyze",
                required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
                dependencies=["g2"],
            ),
            Goal(
                goal_id="g4",
                description="write",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["g3"],
                publication_intent="DRAFT_ONLY",
            ),
        ],
    ))
    facts = {
        "g2": {
            "status": "COMPLETED",
            "completed_capabilities": ["SEARCH_COMMUNITY"],
            "artifact_types": ["SEARCH_RESULT"],
        },
        "g3": {
            "status": "COMPLETED",
            "completed_capabilities": ["ANALYZE_CONTENT_PATTERNS"],
            "artifact_types": ["ANALYSIS_REPORT"],
        },
    }
    ready = select_ready_work(tree, facts)
    assert [item.goal_id for item in ready] == ["g4"]


def test_no_progress_ignores_plan_revision_and_preserves_change_detection() -> None:
    state = AgentState(current_task=TaskNode(
        task_id="task-1:g3",
        goal_id="g3",
        capability="ANALYZE_CONTENT_PATTERNS",
    ))
    failure = {"ok": False, "error_code": "REASONING_RESULT_COMMIT_FAILED"}
    assert _no_progress_detected(state, failure) is False
    assert _no_progress_detected(state, failure) is False
    assert _no_progress_detected(state, failure) is True
    state.context_snapshot["artifacts"] = [{"artifact_id": "new-evidence", "artifact_type": "SEARCH_RESULT"}]
    assert _no_progress_detected(state, failure) is False
