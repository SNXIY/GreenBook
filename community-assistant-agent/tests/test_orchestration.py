from app.agent_registry import AgentRegistry, agent_registry
from app.domain import AgentPlan, CommunityIntent
from app.evaluation import evaluate_plan
from app.graph_runtime import graph_descriptor
from app.tools import RiskLevel, tool_registry


def operation_plan() -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "intent": "CREATE_AND_PUBLISH",
            "summary": "运营 AI 专区",
            "steps": [
                {
                    "task_id": "trend",
                    "agent": "AnalyticsAgent",
                    "capabilities": ["analysis", "trend_analysis"],
                    "tool": "community.analyze_engagement",
                    "label": "分析趋势",
                },
                {
                    "task_id": "users",
                    "agent": "UserInsightAgent",
                    "capabilities": ["analysis", "user_insight"],
                    "tool": "community.analyze_engagement",
                    "label": "分析互动用户",
                },
                {
                    "task_id": "create",
                    "agent": "ContentCreationAgent",
                    "capabilities": ["generation"],
                    "tool": "creator.create_draft",
                    "label": "创作运营内容",
                    "depends_on": ["trend", "users"],
                },
                {
                    "task_id": "publish",
                    "agent": "PublishAgent",
                    "capabilities": ["publishing"],
                    "tool": "publication.publish_now",
                    "label": "发布内容",
                    "depends_on": ["create"],
                },
            ],
        }
    )


def test_operation_dag_has_parallel_analysis_frontier() -> None:
    plan = operation_plan()
    assert graph_descriptor(plan)["layers"] == [
        ["trend", "users"],
        ["create"],
        ["publish"],
    ]
    assert [step.agent for step in agent_registry.route_plan(plan).steps] == [
        "AnalyticsAgent",
        "UserInsightAgent",
        "ContentCreationAgent",
        "PublishAgent",
    ]


def test_answer_only_plan_compiles_as_start_to_end_graph() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANSWER",
            "summary": "直接回答日期",
            "steps": [],
        }
    )
    descriptor = graph_descriptor(plan)
    assert descriptor["layers"] == []
    assert "__answer__" in descriptor["mermaid"]


def test_evaluation_scores_complete_operation_plan() -> None:
    intent = CommunityIntent(
        domain="community_operation",
        goal="提高 AI 专区活跃度",
        priority="high",
        constraints=["最近一周"],
        required_capabilities=[
            "analysis",
            "trend_analysis",
            "user_insight",
            "generation",
            "publishing",
        ],
        confidence=0.95,
    )
    plan = operation_plan()
    result = evaluate_plan(
        intent=intent,
        plan=plan,
        expected_domain="community_operation",
        required_capabilities=set(intent.required_capabilities),
        required_tools={
            "community.analyze_engagement",
            "creator.create_draft",
            "publication.publish_now",
        },
        forbidden_tools={"community.delete_post"},
        expected_agents={
            "AnalyticsAgent",
            "UserInsightAgent",
            "ContentCreationAgent",
            "PublishAgent",
        },
    )
    assert result.intent_accuracy == 1
    assert result.task_coverage == 1
    assert result.agent_selection_accuracy == 1


def test_batch_schedule_is_one_exact_external_write_approval_boundary() -> None:
    arguments = tool_registry.validate(
        "publication.schedule_batch",
        {
            "run_at": "2026-07-30T08:00:00+08:00",
            "interval_minutes": 60,
            "items": [
                {
                    "draft_id": "101",
                    "expected_content_sha256": "a" * 64,
                },
                {
                    "draft_id": "102",
                    "expected_content_sha256": "b" * 64,
                },
            ],
        },
    )
    definition = tool_registry.get("publication.schedule_batch")
    assert definition.risk == RiskLevel.EXTERNAL_WRITE
    assert definition.side_effecting is True
    assert len(arguments["items"]) == 2


def test_agent_registry_manifest_can_extend_without_worker_changes(tmp_path) -> None:
    manifest = tmp_path / "agents.json"
    manifest.write_text(
        '[{"name":"BookmarkAgent","description":"Bookmark posts",'
        '"capabilities":["bookmark"],"tools":["community.bookmark"]}]',
        encoding="utf-8",
    )
    registry = AgentRegistry.from_manifest(manifest)
    plan = AgentPlan.model_validate(
        {
            "intent": "ANSWER",
            "summary": "bookmark",
            "steps": [
                {
                    "task_id": "bookmark",
                    "agent": "AutoRouter",
                    "capabilities": ["bookmark"],
                    "tool": "community.bookmark",
                    "label": "bookmark",
                }
            ],
        }
    )
    assert registry.route(plan.steps[0]).name == "BookmarkAgent"
