"""Phase 2 Goal Runtime decomposition and compilation tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command import (
    Command,
    CommandTarget,
    CommandType,
    TargetKind,
)
from greenbook_agent_core.goal import GoalCompiler, GoalDecomposer
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode

from tests.plan_factory import GoalPlanFactory


class _Completions:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False)),
            )],
        )


class _LLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.chat = SimpleNamespace(completions=_Completions(payload))


async def _decompose(
    payload: dict[str, Any],
    command: Command,
    capabilities: list[str],
) -> GoalTree:
    return await GoalDecomposer(llm=_LLM(payload)).decompose(
        command,
        available_capabilities=capabilities,
    )


@pytest.mark.asyncio
async def test_create_java_article_produces_one_goal() -> None:
    tree = await _decompose(
        {
            "root": {
                "goal_id": "create_java_article",
                "description": "创建一篇 Java 学习文章",
                "goal_type": "CREATE",
                "required_capabilities": ["GENERATE_CONTENT"],
            },
        },
        Command(type=CommandType.CREATE, objective="创建一篇Java学习文章"),
        ["GENERATE_CONTENT"],
    )

    assert tree.root_goal.goal_type == "CREATE"
    assert [goal.goal_id for goal in tree.executable_goals()] == [
        "create_java_article",
    ]
    assert tree.root_goal.children == []


@pytest.mark.asyncio
async def test_multi_goal_decomposition_keeps_publication_semantics_scoped() -> None:
    command = Command(
        type=CommandType.CREATE,
        objective="Create three independently handled content targets",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    )
    tree = await _decompose(
        {
            "root": {
                "goal_id": "root",
                "description": "Three independent content targets",
                "goal_type": "COMPOSITE",
                "children": [
                    {
                        "goal_id": "scheduled-a",
                        "description": "Create content A",
                        "goal_type": "CREATE",
                        "required_capabilities": [
                            "GENERATE_CONTENT",
                            "SCHEDULE_PUBLISH",
                        ],
                        "target": {"topic": "subject-a"},
                        "temporal_constraint": {
                            "run_at": "2026-08-13T13:20:00+08:00",
                        },
                        "publication_intent": "SCHEDULED_PUBLISH",
                    },
                    {
                        "goal_id": "scheduled-b",
                        "description": "Create content B",
                        "goal_type": "CREATE",
                        "required_capabilities": [
                            "GENERATE_CONTENT",
                            "SCHEDULE_PUBLISH",
                        ],
                        "target": {"topic": "subject-b"},
                        "temporal_constraint": {
                            "run_at": "2026-08-14T15:00:00+08:00",
                        },
                        "publication_intent": "SCHEDULED_PUBLISH",
                    },
                    {
                        "goal_id": "draft-only-c",
                        "description": "Create content C",
                        "goal_type": "CREATE",
                        "required_capabilities": ["GENERATE_CONTENT"],
                        "target": {"topic": "subject-c"},
                        "publication_intent": "DRAFT_ONLY",
                    },
                ],
            },
        },
        command,
        ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    )

    goals = {goal.goal_id: goal for goal in tree.executable_goals()}
    assert set(goals) == {"scheduled-a", "scheduled-b", "draft-only-c"}
    assert goals["scheduled-a"].temporal_constraint["run_at"] == (
        "2026-08-13T13:20:00+08:00"
    )
    assert goals["scheduled-b"].temporal_constraint["run_at"] == (
        "2026-08-14T15:00:00+08:00"
    )
    assert goals["draft-only-c"].publication_intent == "DRAFT_ONLY"

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree, command=command)
    assert [step.tool_name for step in plan.steps].count("content.create_draft") == 3
    assert [step.tool_name for step in plan.steps].count("publication.schedule") == 2
    assert "publication.publish_now" not in [step.tool_name for step in plan.steps]


@pytest.mark.asyncio
async def test_complex_goal_produces_explicit_goal_tree_and_task_graph() -> None:
    tree = await _decompose(
        {
            "root": {
                "goal_id": "publish_article",
                "description": "分析 AI 趋势，写文章并发布",
                "goal_type": "PUBLISH",
                "children": [
                    {
                        "goal_id": "research_topic",
                        "description": "研究最近 AI 趋势",
                        "goal_type": "RESEARCH",
                        "required_capabilities": ["SEARCH_COMMUNITY"],
                    },
                    {
                        "goal_id": "analyze_content",
                        "description": "分析趋势内容",
                        "goal_type": "ANALYZE",
                        "required_capabilities": ["ANALYZE_CONTENT_PATTERNS"],
                        "dependencies": ["research_topic"],
                    },
                    {
                        "goal_id": "generate_article",
                        "description": "生成文章",
                        "goal_type": "CREATE",
                        "required_capabilities": ["GENERATE_CONTENT"],
                        "dependencies": ["analyze_content"],
                    },
                    {
                        "goal_id": "schedule_publish",
                        "description": "安排发布",
                        "goal_type": "PUBLISH",
                        "required_capabilities": ["SCHEDULE_PUBLISH"],
                        "dependencies": ["generate_article"],
                    },
                ],
            },
        },
        Command(type=CommandType.CREATE, objective="分析AI趋势并写文章然后发布"),
        [
            "SEARCH_COMMUNITY",
            "ANALYZE_CONTENT_PATTERNS",
            "GENERATE_CONTENT",
            "SCHEDULE_PUBLISH",
        ],
    )

    assert [goal.goal_id for goal in tree.root_goal.children] == [
        "research_topic",
        "analyze_content",
        "generate_article",
        "schedule_publish",
    ]
    graph = GoalCompiler().compile(tree)
    assert [node.node_id for node in graph.topological_order()] == [
        "research_topic",
        "analyze_content",
        "generate_article",
        "schedule_publish",
    ]
    assert graph.edges == [
        ("research_topic", "analyze_content"),
        ("analyze_content", "generate_article"),
        ("generate_article", "schedule_publish"),
    ]
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        tree,
        task_id="publish-ai",
    )
    assert [step.capability for step in plan.steps] == [
        "SEARCH_COMMUNITY",
        "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT",
        "SCHEDULE_PUBLISH",
    ]
    assert plan.steps[1].input_artifact_types == ["SEARCH_RESULT"]
    assert plan.steps[2].input_artifact_types == ["ANALYSIS_REPORT"]


@pytest.mark.asyncio
async def test_modify_yesterday_schedule_produces_modify_goal_with_target() -> None:
    command = Command(
        type=CommandType.MODIFY,
        objective="修改昨天安排的发布任务",
        target=CommandTarget(
            kind=TargetKind.SCHEDULE,
            reference="昨天安排的发布任务",
        ),
    )
    tree = await _decompose(
        {
            "root": {
                "goal_id": "modify_schedule",
                "description": "修改昨天安排的发布任务",
                "goal_type": "MODIFY",
                "required_capabilities": ["MANAGE_SCHEDULE"],
                "constraints": [{"type": "TIME", "value": "昨天"}],
            },
        },
        command,
        ["MANAGE_SCHEDULE"],
    )

    graph = GoalCompiler().compile(tree, command=command)
    assert tree.root_goal.goal_type == "MODIFY"
    assert graph.nodes[0].capabilities == ["MANAGE_SCHEDULE"]
    assert graph.nodes[0].target_hint == "昨天安排的发布任务"
    assert graph.nodes[0].constraints[0]["type"] == "TIME"


@pytest.mark.asyncio
async def test_planner_compiles_goal_tree_into_existing_task_plan_contract() -> None:
    tree = await _decompose(
        {
            "root": {
                "goal_id": "create_java_article",
                "description": "创建一篇 Java 学习文章",
                "goal_type": "CREATE",
                "required_capabilities": ["GENERATE_CONTENT"],
            },
        },
        Command(type=CommandType.CREATE, objective="创建一篇Java学习文章"),
        ["GENERATE_CONTENT"],
    )

    plan = GoalPlanFactory().generate_plan(
        task_id="task-java",
        goal_tree=tree,
    )
    assert plan.plan_source == "GOAL_RUNTIME"
    assert [step.capability for step in plan.steps] == ["GENERATE_CONTENT"]
    assert plan.steps[0].goal_id == "create_java_article"


def test_content_goal_compiles_draft_contract_arguments() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="create_agent_article",
            description="Write a Chinese draft article about Agent",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT"],
            constraints=[{"type": "topic", "value": "Agent"}],
        )
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree)

    assert plan.steps[0].tool_name == "content.create_draft"
    assert plan.steps[0].constraints["title"] == "Agent"
    assert plan.steps[0].constraints["instruction"] == (
        "Write a Chinese draft article about Agent"
    )
    # The assistant-first direct-write contract carries no Creator strategy
    # or task inputs; only title/instruction/references/summary are allowed.
    for key in ("strategy_task_id", "strategy_artifact_id", "creator_task_id"):
        assert key not in plan.steps[0].constraints


def test_content_goal_preserves_command_context_for_nested_generation_step() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="generate_first_article",
            description="Generate the first article in the series",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT"],
        )
    )
    command = Command(
        type=CommandType.CREATE,
        goal="Design a Java engineer Agent learning series and write the first practical article",
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree, command=command)

    instruction = plan.steps[0].constraints["instruction"]
    assert instruction.startswith("Generate the first article in the series")
    assert "Java engineer Agent learning series" in instruction


def test_goal_tree_resolves_flat_child_ids_without_creating_goals() -> None:
    tree = GoalTree.model_validate({
        "root": {
            "goal_id": "root",
            "description": "Root",
            "children": [
                {
                    "goal_id": "child",
                    "description": "Child",
                    "children": [],
                }
            ],
        },
        "goals": [
            {
                "goal_id": "root",
                "description": "Root",
                "children": ["child"],
            },
            {
                "goal_id": "child",
                "description": "Child",
                "children": [],
            },
        ],
    })

    tree.validate_tree()
    assert [goal.goal_id for goal in tree.all_goals()] == ["root", "child"]


def test_schedule_goal_normalizes_structured_target_time_and_draft() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="schedule_draft",
            description="Schedule the active draft tomorrow morning",
            goal_type="PUBLISH",
            required_capabilities=["SCHEDULE_PUBLISH"],
            constraints=[
                {
                    "id": "draft-1",
                    "kind": "DRAFT",
                    "publish_at": "2026-08-13T09:00:00+08:00",
                    "timezone": "Asia/Shanghai",
                }
            ],
        )
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree)

    assert plan.steps[0].constraints["draft_id"] == "draft-1"
    assert plan.steps[0].constraints["run_at"] == "2026-08-13T09:00:00+08:00"


def test_schedule_goal_normalizes_iso_datetime_in_structured_goal_description() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="schedule_from_goal_fact",
            description="安排在北京时间 2026-08-14 20:00 发布修改后的草稿",
            goal_type="PUBLISH",
            required_capabilities=["SCHEDULE_PUBLISH"],
            constraints=[{"type": "draft_id", "value": "draft-1"}],
        )
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree)

    assert plan.steps[0].constraints["run_at"] == "2026-08-14T20:00:00+08:00"


def test_partial_task_nodes_do_not_truncate_leaf_goal_plan() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="operate_column",
            goal_type="COMPOSITE",
            children=[
                Goal(
                    goal_id="research",
                    description="Analyze community interests",
                    required_capabilities=[
                        "SEARCH_COMMUNITY",
                        "ANALYZE_CONTENT_PATTERNS",
                    ],
                ),
                Goal(
                    goal_id="write",
                    description="Generate the first issue draft",
                    required_capabilities=["GENERATE_CONTENT"],
                    dependencies=["research"],
                ),
            ],
        ),
        task_nodes=[TaskNode(
            task_id="research-search",
            goal_id="research",
            capability="SEARCH_COMMUNITY",
        )],
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree)

    caps = [step.capability for step in plan.steps]
    # Evidence-bounded analysis: a GET_POST_DETAIL read is deterministically
    # inserted between SEARCH_COMMUNITY and ANALYZE_CONTENT_PATTERNS.
    assert caps == [
        "SEARCH_COMMUNITY",
        "GET_POST_DETAIL",
        "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT",
    ]
    assert caps.index("SEARCH_COMMUNITY") < caps.index("GET_POST_DETAIL") < caps.index("ANALYZE_CONTENT_PATTERNS")
    assert plan.steps[1].capability == "GET_POST_DETAIL"
    assert plan.steps[2].depends_on == ["research-search", "research:evidence-read"]
    assert plan.steps[3].depends_on == [
        "research-search", "research:evidence-read", "research:1",
    ]
