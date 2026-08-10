"""Phase15-C semantic task graph and read-only Query regression tests."""

from __future__ import annotations

from typing import Any

import pytest

from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.conversation_runtime_adapter import ConversationRuntimeAdapter
from greenbook_assistant_api.services.query_handler import (
    QueryHandlerError,
    QueryRequest,
    QueryResult,
    ReadOnlyQueryHandler,
)
from greenbook_assistant_api.services.task_provider import TaskBinding, TaskScope
from greenbook_assistant_core.task.graph import TaskGraphBuilder
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    IntentAction,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.models import ArtifactRef, ResolvedTaskTarget, Task, TaskStatus
from greenbook_assistant_core.task.multi_task import ConversationTargetResolver


def _intent(goal: str, action: ActionType, resource: ResourceType) -> dict[str, Any]:
    return IntentSpec(
        mode=IntentMode.SIMPLE,
        goal=goal,
        actions=[IntentAction(action=action, resource=resource)],
        confidence=0.95,
    ).model_dump(mode="json")


class _GraphProvider:
    async def resolve_graph(self, message: str, *, existing_tasks=None) -> list[dict[str, Any]]:
        return [
            {
                "text": "分析最近Java帖子",
                "intent": _intent("分析Java帖子", ActionType.ANALYZE, ResourceType.POST),
            },
            {
                "text": "结合社区内容写新人学习指南并发布",
                "intent": IntentSpec(
                    mode=IntentMode.COMPOSITE,
                    goal="生成新人学习指南并发布",
                    actions=[
                        IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT),
                        IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT),
                    ],
                ).model_dump(mode="json"),
                "depends_on": [0],
                "artifact_inputs": ["QUERY_RESULT"],
            },
            {
                "text": "整理Redis缓存面试重点",
                "intent": _intent("整理Redis缓存面试重点", ActionType.CREATE, ResourceType.CONTENT),
            },
        ]


class _UpdateQueryProvider:
    async def resolve_graph(self, message: str, *, existing_tasks=None) -> list[dict[str, Any]]:
        return [
            {
                "text": "Java文章不要动",
                "intent": IntentSpec(
                    goal="保持Java文章不变",
                    actions=[IntentAction(action=ActionType.UPDATE, resource=ResourceType.DRAFT)],
                    target_hint="Java文章",
                ).model_dump(mode="json"),
            },
            {
                "text": "告诉我最近Java帖子趋势",
                "intent": _intent("查询Java帖子趋势", ActionType.QUERY, ResourceType.POST),
            },
        ]


@pytest.mark.asyncio
async def test_task_graph_builder_uses_semantic_proposals_and_dependency_edges() -> None:
    graph = await TaskGraphBuilder(_GraphProvider()).build(
        "帮我分析Java帖子，结合内容写文章，同时整理Redis重点。"
    )
    assert [node.goal for node in graph.nodes] == [
        "分析Java帖子", "生成新人学习指南并发布", "整理Redis缓存面试重点",
    ]
    assert graph.nodes[0].read_only is True
    assert graph.nodes[0].create_task is False
    assert graph.nodes[1].depends_on == ["goal-1"]
    assert graph.edges == [("goal-1", "goal-2")]
    ordered = [node.node_id for node in graph.topological_order()]
    assert ordered.index("goal-1") < ordered.index("goal-2")
    assert set(ordered) == {"goal-1", "goal-2", "goal-3"}


@pytest.mark.asyncio
async def test_update_and_query_can_coexist_without_query_execution() -> None:
    graph = await TaskGraphBuilder(_UpdateQueryProvider()).build(
        "Java文章不要动，Redis增加布隆过滤器，然后告诉我最近Java帖子趋势。"
    )
    assert graph.nodes[0].read_only is False
    assert graph.nodes[0].create_task is True
    assert graph.nodes[1].read_only is True
    assert graph.nodes[1].create_task is False


def test_ambiguous_natural_reference_requires_clarification() -> None:
    tasks = [
        Task(
            task_id="java-1", conversation_id="c1", user_id="u1", tenant_id="t1",
            goal="Java文章", goal_summary="Java文章", status=TaskStatus.READY,
        ),
        Task(
            task_id="java-2", conversation_id="c1", user_id="u1", tenant_id="t1",
            goal="Java学习规划", goal_summary="Java学习规划", status=TaskStatus.READY,
        ),
    ]
    resolution = ConversationTargetResolver().resolve("帮我优化一下", tasks)
    assert resolution.task is None
    assert resolution.is_ambiguous is True
    assert {task.task_id for task in resolution.candidates} == {"java-1", "java-2"}


class _MCP:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(tool_name)
        return {"ok": True, "data": {"items": [{"title": "Java Hot"}]}}


@pytest.mark.asyncio
async def test_query_handler_only_calls_read_tool() -> None:
    mcp = _MCP()
    handler = ReadOnlyQueryHandler()
    result = await handler.handle(QueryRequest(
        message="分析最近Java帖子",
        intent=IntentSpec(
            goal="分析Java帖子",
            actions=[IntentAction(action=ActionType.ANALYZE, resource=ResourceType.POST)],
        ),
        conversation_id="c1",
        run_id="r1",
        trace_id="t1",
        mcp=mcp,
    ))
    assert result.data["read_only"] is True
    assert result.data["operation"] == "community.analyze_posts"
    assert mcp.calls == ["community.search_public_posts"]
    assert result.artifacts[0].artifact_type == "QUERY_RESULT"

    with pytest.raises(QueryHandlerError, match="WRITE_ACTION"):
        await handler.handle(QueryRequest(
            message="写一篇文章",
            intent=IntentSpec(
                goal="写文章",
                actions=[IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT)],
            ),
            conversation_id="c1", run_id="r2", trace_id="t2", mcp=mcp,
        ))
    assert mcp.calls == ["community.search_public_posts"]


class _TaskProvider:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def list_tasks(self, scope: TaskScope) -> list[Task]:
        return list(self.tasks)

    async def create_task(self, scope: TaskScope, intent_spec: IntentSpec) -> Task:
        task = Task(
            task_id=f"task-{len(self.tasks) + 1}",
            conversation_id=scope.conversation_id,
            user_id=scope.user_id,
            tenant_id=scope.tenant_id,
            goal=intent_spec.goal,
            goal_category="CREATE_CONTENT",
            status=TaskStatus.READY,
        )
        self.tasks.append(task)
        return task

    async def resolve_task(self, scope: TaskScope, intent: Any) -> TaskBinding:
        task = self.tasks[0]
        return TaskBinding(task, ResolvedTaskTarget(task_id=task.task_id))


class _Runtime:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def execute(self, context: Any, **kwargs: Any) -> RuntimeResult:
        self.contexts.append(context)
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            task_id=context.task_id,
            execution_id=f"exec-{context.task_id}",
            content=context.task_context.goal,
        )


class _QueryHandler:
    async def handle(self, request: QueryRequest) -> QueryResult:
        return QueryResult(
            content="Java帖子分析结果",
            artifacts=[ArtifactRef(
                task_id=f"query:{request.run_id}",
                artifact_type="QUERY_RESULT",
                summary="Java帖子分析结果",
            )],
        )


@pytest.mark.asyncio
async def test_graph_execution_passes_query_artifact_to_dependent_task() -> None:
    runtime = _Runtime()
    adapter = ConversationRuntimeAdapter(
        intent_provider=_GraphProvider(),
        task_provider=_TaskProvider(),
        runtime_service=runtime,
        query_handler=_QueryHandler(),
    )
    result = await adapter.execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="帮我分析Java帖子并写文章，同时整理Redis重点。",
    )
    assert result.success is True
    assert result.partial_results["task_graph"] is True
    assert len(result.partial_results["nodes"]) == 3
    assert len(runtime.contexts) == 2  # query has no Runtime execution
    dependent_contexts = [
        context for context in runtime.contexts
        if context.task_context.artifact_refs
    ]
    assert len(dependent_contexts) == 1
    assert dependent_contexts[0].task_context.artifact_refs[0].artifact_type == "QUERY_RESULT"
