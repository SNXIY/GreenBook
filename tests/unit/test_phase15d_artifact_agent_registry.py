"""Phase15-D Artifact lifecycle and Agent Registry tests."""

from __future__ import annotations

from typing import Any

import pytest

from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.conversation_runtime_adapter import ConversationRuntimeAdapter
from greenbook_assistant_api.services.query_handler import QueryRequest, QueryResult
from greenbook_assistant_api.services.task_provider import TaskBinding, TaskScope
from greenbook_assistant_core.artifact.models import Artifact, ArtifactLifecycle, ArtifactReference
from greenbook_assistant_core.artifact.registry import ArtifactRegistry, ArtifactRegistryError
from greenbook_assistant_core.artifact.repository import ArtifactRepository
from greenbook_assistant_core.orchestration.agent_registry import (
    AgentMetadata,
    AgentRegistry,
    AgentResolutionError,
    SideEffectLevel,
)
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.task.graph import TaskGraphBuilder
from greenbook_assistant_core.task.intent_models import ActionType, IntentAction, IntentMode, IntentSpec, ResourceType
from greenbook_assistant_core.task.models import ResolvedTaskTarget, Task, TaskStatus


def test_artifact_registry_lifecycle_and_reference() -> None:
    repository = ArtifactRepository()
    repository.clear()
    registry = ArtifactRegistry(repository)
    artifact = registry.register(Artifact(
        artifact_id="art-java-posts",
        artifact_type="POST_COLLECTION",
        task_id="task-search",
        execution_id="exec-search",
        created_by_agent="SearchAgent",
        metadata_schema="greenbook.posts.v1",
        metadata={"count": 50},
    ))
    assert artifact.lifecycle == ArtifactLifecycle.CREATED
    assert artifact.owner_task_id == "task-search"
    reference = artifact.to_reference()
    assert isinstance(reference, ArtifactReference)
    assert "count" not in reference.model_dump(mode="json", by_alias=True)

    registry.mark_available(artifact.artifact_id)
    consumed = registry.mark_consumed(artifact.artifact_id, consumer_task_id="task-create")
    assert consumed.lifecycle == ArtifactLifecycle.CONSUMED
    assert consumed.consumed_by_task_ids == ["task-create"]
    registry.archive(artifact.artifact_id)
    assert registry.require(artifact.artifact_id).lifecycle == ArtifactLifecycle.ARCHIVED
    with pytest.raises(ArtifactRegistryError, match="ALREADY_ARCHIVED"):
        registry.mark_available(artifact.artifact_id)


def test_agent_registry_resolution_and_capability_validation() -> None:
    registry = AgentRegistry()
    assert registry.resolve_agent("QUERY").name == "SearchAgent"
    assert registry.resolve_agent("CREATE_CONTENT").name == "CreatorAgent"
    assert registry.resolve_agent("PUBLISH").name == "PublishAgent"
    creator = registry.resolve_agent(
        "CREATE_CONTENT",
        input_artifacts=["POST_ANALYSIS"],
        output_artifact="CONTENT_DRAFT",
    )
    assert creator.side_effect_level == SideEffectLevel.NONE
    with pytest.raises(AgentResolutionError):
        registry.resolve_agent("CREATE_CONTENT", input_artifacts=["SCHEDULE"])
    with pytest.raises(AgentResolutionError, match="ALREADY_REGISTERED"):
        registry.register_agent(AgentMetadata(name="CreatorAgent"))


def test_planner_selects_agents_without_changing_step_capabilities() -> None:
    plan = TaskOrchestrator().generate_plan(
        task_id="task-java",
        goal_category="COMPOSITE",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    assert [step.capability for step in plan.steps] == [
        "SEARCH_COMMUNITY", "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT", "VALIDATE_QUALITY", "SCHEDULE_PUBLISH",
    ]
    assert [step.agent_name for step in plan.steps] == [
        "SearchAgent", "AnalyticsAgent", "CreatorAgent", "QualityAgent", "PublishAgent",
    ]


class _GraphProvider:
    async def resolve_graph(self, message: str, *, existing_tasks=None) -> list[dict[str, Any]]:
        return [
            {
                "text": "分析Java帖子",
                "intent": IntentSpec(
                    mode=IntentMode.SIMPLE,
                    goal="分析Java帖子",
                    actions=[IntentAction(action=ActionType.QUERY, resource=ResourceType.POST)],
                ).model_dump(mode="json"),
            },
            {
                "text": "生成Java文章",
                "intent": IntentSpec(
                    mode=IntentMode.SIMPLE,
                    goal="生成Java文章",
                    actions=[IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT)],
                ).model_dump(mode="json"),
                "depends_on": [0],
            },
            {
                "text": "发布Java文章",
                "intent": IntentSpec(
                    mode=IntentMode.SIMPLE,
                    goal="发布Java文章",
                    actions=[IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT)],
                ).model_dump(mode="json"),
                "depends_on": [1],
            },
        ]


class _Query:
    async def handle(self, request: QueryRequest) -> QueryResult:
        from greenbook_assistant_core.task.models import ArtifactRef
        return QueryResult(
            content="Java帖子分析",
            artifacts=[ArtifactRef(
                artifact_id="art-analysis",
                task_id="query:run",
                artifact_type="POST_ANALYSIS",
                summary="Java帖子分析",
            )],
        )


class _Tasks:
    def __init__(self) -> None:
        self.items: list[Task] = []

    async def list_tasks(self, scope: TaskScope) -> list[Task]:
        return list(self.items)

    async def create_task(self, scope: TaskScope, intent_spec: IntentSpec) -> Task:
        task = Task(
            task_id=f"task-{len(self.items) + 1}",
            conversation_id=scope.conversation_id,
            user_id=scope.user_id,
            tenant_id=scope.tenant_id,
            goal=intent_spec.goal,
            goal_category="CREATE_CONTENT",
            status=TaskStatus.READY,
        )
        self.items.append(task)
        return task

    async def resolve_task(self, scope: TaskScope, intent: Any) -> TaskBinding:
        task = self.items[0]
        return TaskBinding(task, ResolvedTaskTarget(task_id=task.task_id))


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, context: Any, **kwargs: Any) -> RuntimeResult:
        self.calls.append(context)
        artifact_type = "PUBLISHED_POST" if "发布" in context.task_context.goal else "CONTENT_DRAFT"
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            task_id=context.task_id,
            execution_id=f"exec-{context.task_id}",
            content=context.task_context.goal,
            artifacts=[{
                "artifact_id": f"artifact-{context.task_id}",
                "artifact_type": artifact_type,
                "summary": context.task_context.goal,
            }],
        )


@pytest.mark.asyncio
async def test_query_creator_publish_exchange_uses_references_and_publishes_once() -> None:
    repository = ArtifactRepository()
    repository.clear()
    artifact_registry = ArtifactRegistry(repository)
    tasks = _Tasks()
    runtime = _Runtime()
    adapter = ConversationRuntimeAdapter(
        intent_provider=_GraphProvider(),
        task_provider=tasks,
        runtime_service=runtime,
        query_handler=_Query(),
        artifact_registry=artifact_registry,
    )
    result = await adapter.execute(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="分析Java帖子，生成文章，然后发布。",
    )
    assert result.success is True
    assert [node["agent_name"] for node in result.partial_results["nodes"]] == [
        "SearchAgent", "CreatorAgent", "PublishAgent",
    ]
    creator_node = next(
        node for node in result.partial_results["nodes"] if node["agent_name"] == "CreatorAgent"
    )
    assert creator_node["input_artifacts"][0]["artifact_type"] == "POST_ANALYSIS"
    assert creator_node["output_artifacts"][0]["artifact_type"] == "CONTENT_DRAFT"
    assert len(runtime.calls) == 2
    creator_context = next(context for context in runtime.calls if "生成" in context.task_context.goal)
    assert creator_context.task_context.artifact_refs[0].artifact_id == "art-analysis"
    publish_context = next(context for context in runtime.calls if "发布" in context.task_context.goal)
    assert publish_context.task_context.artifact_refs[0].artifact_type == "CONTENT_DRAFT"
    assert len([context for context in runtime.calls if "发布" in context.task_context.goal]) == 1
    assert artifact_registry.require("art-analysis").lifecycle == ArtifactLifecycle.CONSUMED
