from types import SimpleNamespace
from datetime import datetime, timezone

from app.agent_registry import AgentRegistry, agent_registry
from app.domain import AgentPlan, CommunityIntent
from app.evaluation import evaluate_plan
from app.graph_runtime import graph_descriptor
from app.tools import RiskLevel, tool_registry
from app.worker import AgentWorker, _resolve_schedule_run_at


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


def test_draft_revalidation_does_not_trigger_semantic_progress_model() -> None:
    worker = object.__new__(AgentWorker)
    worker.registry = tool_registry
    plan = AgentPlan.model_validate(
        {
            "intent": "PUBLISH_CONTINUATION_DRAFT",
            "summary": "Revalidate and publish",
            "steps": [
                {
                    "task_id": "resolve",
                    "tool": "community.get_own_draft",
                    "label": "Revalidate draft",
                    "arguments": {"draft_id": "1"},
                },
                {
                    "task_id": "publish",
                    "primary_capability": "publishing",
                    "tool": "publication.publish_now",
                    "label": "Publish",
                    "depends_on": ["resolve"],
                },
            ],
        }
    )

    assert worker._should_review_progress(plan, completed_layer_index=1) is False


def test_agent_registry_routes_composite_engagement_capabilities() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Analyze active users and the topics they publish",
            "steps": [
                {
                    "task_id": "engagement",
                    "agent": "AnalyticsAgent",
                    "capabilities": [
                        "analysis",
                        "user_insight",
                        "trend_analysis",
                    ],
                    "tool": "community.analyze_engagement",
                    "label": "Analyze active users and post topics",
                }
            ],
        }
    )

    routed = agent_registry.route_plan(plan)

    assert routed.steps[0].agent == "AnalyticsAgent"


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


def test_progress_replan_replaces_pending_steps_and_keeps_completed_steps() -> None:
    current = operation_plan()
    revision = AgentPlan.model_validate(
        {
            "intent": "CREATE_AND_PUBLISH",
            "summary": "Use the observed users to create a revised post",
            "steps": [
                {
                    "task_id": "create-revised",
                    "agent": "ContentCreationAgent",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create revised content",
                    "arguments": {"instruction": "Create from the observations"},
                }
            ],
        }
    )

    merged = AgentWorker._merge_replan(
        current,
        revision,
        completed_task_ids={"trend", "users"},
    )

    assert [str(step.task_id) for step in merged.steps[:2]] == ["trend", "users"]
    assert all(str(step.task_id) != "create" for step in merged.steps)
    assert all(str(step.task_id) != "publish" for step in merged.steps)
    assert merged.steps[-1].tool == "creator.create_draft"


def test_user_scoped_analysis_binds_ids_from_real_upstream_output() -> None:
    worker = AgentWorker.__new__(AgentWorker)
    run = SimpleNamespace(prompt="分析活跃用户", context_post_id=None)

    arguments = worker._resolve_arguments(
        run=run,
        tool="community.aggregate_post_topics",
        arguments={"user_ids": ["forged-user"], "days": 7},
        previous_outputs=[
            {
                "tool": "community.list_active_users",
                "result": {
                    "users": [
                        {"user_id": "101"},
                        {"userId": "202"},
                        {"user_id": "101"},
                    ]
                },
            }
        ],
    )

    assert arguments["user_ids"] == ["101", "202"]
    assert arguments["days"] == 7


def test_progress_supervisor_runs_after_read_chain_not_after_every_read() -> None:
    worker = AgentWorker.__new__(AgentWorker)
    worker.registry = tool_registry
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE_CREATE_SCHEDULE",
            "summary": "Analyze, create and schedule",
            "steps": [
                {
                    "task_id": "users",
                    "primary_capability": "user_insight",
                    "tool": "community.list_active_users",
                    "label": "List active users",
                },
                {
                    "task_id": "posts",
                    "primary_capability": "user_insight",
                    "tool": "community.list_posts_by_users",
                    "label": "Read user posts",
                    "depends_on": ["users"],
                },
                {
                    "task_id": "topics",
                    "primary_capability": "trend_analysis",
                    "tool": "community.aggregate_post_topics",
                    "label": "Aggregate topics",
                    "depends_on": ["posts"],
                },
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create draft",
                    "arguments": {"instruction": "Create from analysis"},
                    "depends_on": ["topics"],
                },
            ],
        }
    )

    assert not worker._should_review_progress(plan, completed_layer_index=1)
    assert not worker._should_review_progress(plan, completed_layer_index=2)
    assert worker._should_review_progress(plan, completed_layer_index=3)
    assert worker._progress_assessment_key(
        plan, ["topics", "users", "posts"]
    ) == worker._progress_assessment_key(
        plan, ["posts", "topics", "users", "posts"]
    )


def test_creator_references_use_real_user_posts_and_topic_artifacts() -> None:
    references = AgentWorker._reference_results(
        {"references": [{"id": "invented"}]},
        [
            {
                "tool": "community.list_posts_by_users",
                "result": {
                    "posts": [
                        {
                            "post_id": "100",
                            "author_id": "8",
                            "title": "Java 学习路线",
                            "description": "从基础到项目",
                            "tags": ["Java"],
                            "type": "image_text",
                        }
                    ]
                },
            },
            {
                "tool": "community.aggregate_post_topics",
                "result": {
                    "topics": [
                        {"topic": "Java", "post_count": 3, "creator_count": 2}
                    ]
                },
            },
        ],
    )

    assert {item["id"] for item in references} == {
        "100",
        "analytics:active-user-topics",
    }


def test_relative_schedule_is_resolved_when_schedule_step_executes() -> None:
    now = datetime(
        2026,
        7,
        31,
        8,
        0,
        tzinfo=timezone.utc,
    )

    resolved = _resolve_schedule_run_at({"delay_seconds": 300}, now=now)

    assert (resolved - now).total_seconds() == 300


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
