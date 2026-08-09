from __future__ import annotations

import pytest

from app.agent_registry import AgentDescriptor, agent_registry
from app.artifact_contracts import ArtifactBinder, ArtifactKind
from app.capability_graph import CapabilityDescriptor, CapabilityGraph
from app.domain import AgentPlan, TargetBinding
from app.plan_compiler import PlanCompiler
from app.tool_runtime import ToolAdapterRuntime, ToolRuntimeContext
from app.tools import tool_registry


def test_capability_graph_expands_specialized_capabilities() -> None:
    expanded = agent_registry.capability_graph.expand(
        {"trend_analysis", "schedule_publish"}
    )

    assert {"trend_analysis", "analysis"}.issubset(expanded)
    assert {"schedule_publish", "publishing"}.issubset(expanded)


def test_capability_graph_normalizes_common_model_synonyms() -> None:
    graph = agent_registry.capability_graph

    assert graph.canonicalize("scheduling") == "schedule_publish"
    assert graph.canonicalize("schedule_update") == "schedule_publish"
    assert graph.canonicalize("update_schedule") == "schedule_publish"
    assert graph.canonicalize("publish") == "publishing"
    assert graph.normalize(["scheduling", "schedule_publish", "schedule_update"]) == [
        "schedule_publish"
    ]


def test_capability_graph_rejects_cycles_at_startup() -> None:
    with pytest.raises(ValueError, match="cycle"):
        CapabilityGraph(
            version="invalid-v1",
            capabilities=[
                CapabilityDescriptor("a", "A", frozenset({"b"})),
                CapabilityDescriptor("b", "B", frozenset({"a"})),
            ],
        )


def test_agent_descriptor_uses_capability_implications() -> None:
    analytics = AgentDescriptor(
        name="TrendOnlyAgent",
        description="Trend specialist",
        capabilities=frozenset({"trend_analysis"}),
        tools=frozenset({"community.aggregate_post_topics"}),
    )
    step = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Analyze",
            "steps": [
                {
                    "task_id": "analysis",
                    "primary_capability": "analysis",
                    "tool": "community.aggregate_post_topics",
                    "label": "Analyze topics",
                }
            ],
        }
    ).steps[0]

    assert analytics.supports(step)


def test_compiler_persists_typed_artifact_lineage() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE_AND_CREATE",
            "summary": "Analyze users and create a related draft",
            "steps": [
                {
                    "task_id": "users",
                    "primary_capability": "user_insight",
                    "tool": "community.list_active_users",
                    "label": "List active users",
                },
                {
                    "task_id": "topics",
                    "primary_capability": "trend_analysis",
                    "tool": "community.aggregate_post_topics",
                    "label": "Aggregate topics",
                    "depends_on": ["users"],
                },
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create a draft",
                    "arguments": {"instruction": "Create from evidence"},
                    "depends_on": ["topics"],
                },
            ],
        }
    )

    compiled = PlanCompiler(
        tools=tool_registry,
        agents=agent_registry,
    ).compile(plan)

    assert compiled.status == "EXECUTABLE"
    assert compiled.compiled_plan is not None
    topics, create = compiled.compiled_plan.steps[1:]
    assert topics.artifact_sources["user_ids"] == ["users"]
    assert set(create.artifact_sources["references"]) == {"users", "topics"}
    assert topics.expected_artifact_type == "topic_analysis"


def test_tool_runtime_binds_only_compiled_artifact_sources() -> None:
    definition = tool_registry.get("community.aggregate_post_topics")
    runtime = ToolAdapterRuntime(ArtifactBinder())
    artifacts = [
        {
            "task_id": "unrelated-users",
            "artifact_type": ArtifactKind.USER_SET,
            "result": {"users": [{"user_id": "wrong"}]},
        },
        {
            "task_id": "authorized-users",
            "artifact_type": ArtifactKind.USER_SET,
            "result": {"users": [{"user_id": "101"}, {"userId": "202"}]},
        },
    ]

    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"user_ids": ["invented"], "days": 7},
        artifacts=artifacts,
        context=ToolRuntimeContext(
            prompt="analyze users",
            context_post_id=None,
            context_comment_id=None,
        ),
        binding_sources={"user_ids": ["authorized-users"]},
    )

    assert arguments["user_ids"] == ["101", "202"]
    assert arguments["days"] == 7


def test_target_binding_wins_over_other_current_run_drafts() -> None:
    definition = tool_registry.get("creator.revise_draft")
    runtime = ToolAdapterRuntime(ArtifactBinder())
    original_sha = "a" * 64
    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"instruction": "增加 Java 和 Python 代码"},
        artifacts=[
            {
                "task_id": "newer-draft",
                "artifact_type": ArtifactKind.CONTENT_DRAFT,
                "result": {
                    "draft_id": "draft-newer",
                    "content_sha256": "b" * 64,
                },
            }
        ],
        context=ToolRuntimeContext(
            prompt="给它增加 Java 和 Python 代码",
            context_post_id=None,
            context_comment_id=None,
            resolved_targets={"CONTENT": TargetBinding(
                target_type="DRAFT",
                target_id="draft-original",
                artifact_id="artifact-original",
                content_sha256=original_sha,
                version=1,
                confidence=1.0,
                resolution_method="ACTIVE_TARGET",
            ).model_dump(mode="json")},
        ),
    )

    assert arguments["draft_id"] == "draft-original"
    assert arguments["expected_content_sha256"] == original_sha


def test_target_bound_revision_rejects_missing_active_target() -> None:
    definition = tool_registry.get("creator.revise_draft")
    runtime = ToolAdapterRuntime(ArtifactBinder())

    with pytest.raises(ValueError, match="target roles"):
        runtime.prepare_arguments(
            definition=definition,
            planner_arguments={"instruction": "增加代码"},
            artifacts=[
                {
                    "task_id": "latest-draft",
                    "artifact_type": ArtifactKind.CONTENT_DRAFT,
                    "result": {
                        "draft_id": "draft-latest",
                        "content_sha256": "c" * 64,
                    },
                }
            ],
            context=ToolRuntimeContext(
                prompt="给它增加代码",
                context_post_id=None,
                context_comment_id=None,
            ),
        )


def test_scheduled_draft_binding_supplies_schedule_operation_id() -> None:
    definition = tool_registry.get("publication.cancel_schedule")
    runtime = ToolAdapterRuntime(ArtifactBinder())

    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={},
        artifacts=[],
        context=ToolRuntimeContext(
            prompt="取消这个定时发布",
            context_post_id=None,
            context_comment_id=None,
                resolved_targets={"SCHEDULE": TargetBinding(
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id="schedule-1",
                    schedule_id="schedule-1",
                    resolution_method="ACTIVE_TARGET",
                ).model_dump(mode="json")},
        ),
    )

    assert arguments["action_id"] == "schedule-1"


def test_schedule_only_update_skips_optional_draft_bindings() -> None:
    definition = tool_registry.get("publication.update_schedule")
    runtime = ToolAdapterRuntime(ArtifactBinder())

    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"run_at": "2026-08-03T18:10:00+08:00"},
        artifacts=[
            {
                "task_id": "read-existing-schedule",
                "artifact_type": ArtifactKind.SCHEDULE_RECEIPT,
                "result": {
                    "action_id": "schedule-current",
                    "draft_id": "draft-current",
                    "status": "SCHEDULED",
                },
            }
        ],
        context=ToolRuntimeContext(
            prompt="\u53d1\u5e03\u65f6\u95f4\u4fee\u6539\u6210\u4e94\u5206\u949f\u4e4b\u540e",
            context_post_id=None,
            context_comment_id=None,
            resolved_targets={"SCHEDULE": TargetBinding(
                target_type="SCHEDULE",
                target_id="schedule-current",
                schedule_id="schedule-current",
                resolution_method="ACTIVE_TARGET",
            ).model_dump(mode="json")},
        ),
        binding_sources={
            "action_id": ["read-existing-schedule"],
            "draft_id": [],
            "expected_content_sha256": [],
        },
    )

    assert arguments["action_id"] == "schedule-current"
    assert "draft_id" not in arguments
    assert "expected_content_sha256" not in arguments


def test_schedule_rebind_uses_verified_schedule_and_revised_draft_artifacts() -> None:
    definition = tool_registry.get("publication.update_schedule")
    runtime = ToolAdapterRuntime(ArtifactBinder())
    revised_sha = "d" * 64

    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={},
        artifacts=[
            {
                "task_id": "read-current-schedule",
                "artifact_type": ArtifactKind.SCHEDULE_RECEIPT,
                "result": {
                    "action_id": "schedule-verified",
                    "draft_id": "draft-1",
                    "status": "SCHEDULED",
                },
            },
            {
                "task_id": "revise-current-draft",
                "artifact_type": ArtifactKind.CONTENT_DRAFT,
                "result": {
                    "draft_id": "draft-1",
                    "content_sha256": revised_sha,
                    "title": "Revised title",
                },
            },
        ],
        context=ToolRuntimeContext(
            prompt="Add Java code",
            context_post_id=None,
            context_comment_id=None,
                resolved_targets={
                    "CONTENT": TargetBinding(
                    target_type="DRAFT",
                    target_id="draft-1",
                    content_sha256="a" * 64,
                    resolution_method="ACTIVE_TARGET",
                    ).model_dump(mode="json"),
                    "SCHEDULE": TargetBinding(
                        target_type="SCHEDULE",
                        role="SCHEDULE",
                        target_id="schedule-verified",
                        resolution_method="ACTIVE_TARGET",
                    ).model_dump(mode="json"),
                },
        ),
        binding_sources={
            "action_id": ["read-current-schedule"],
            "draft_id": ["revise-current-draft"],
            "expected_content_sha256": ["revise-current-draft"],
        },
    )

    assert arguments == {
        "action_id": "schedule-verified",
        "draft_id": "draft-1",
        "expected_content_sha256": revised_sha,
    }


def test_optional_artifact_binding_does_not_capture_parallel_output() -> None:
    definition = tool_registry.get("creator.create_draft")
    runtime = ToolAdapterRuntime(ArtifactBinder())

    arguments = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"instruction": "Write an original post"},
        artifacts=[
            {
                "task_id": "parallel-search",
                "artifact_type": ArtifactKind.POST_SEARCH_RESULTS,
                "result": {"results": [{"id": "must-not-leak"}]},
            }
        ],
        context=ToolRuntimeContext(
            prompt="Write an original post",
            context_post_id=None,
            context_comment_id=None,
        ),
        binding_sources={"references": []},
    )

    assert arguments["references"] == []


def test_comment_reply_can_consume_a_summary_artifact_or_planner_text() -> None:
    definition = tool_registry.get("community.reply_comment")
    runtime = ToolAdapterRuntime(ArtifactBinder())
    context = ToolRuntimeContext(
        prompt="Summarize and reply",
        context_post_id="post-1",
        context_comment_id="comment-1",
    )

    from_summary = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"content": "placeholder"},
        artifacts=[
            {
                "task_id": "summary",
                "artifact_type": ArtifactKind.POST_SUMMARY,
                "result": {"summary": "Three verified conclusions"},
            }
        ],
        context=context,
        binding_sources={"content": ["summary"]},
    )
    standalone = runtime.prepare_arguments(
        definition=definition,
        planner_arguments={"content": "A direct reply"},
        artifacts=[],
        context=context,
        binding_sources={"content": []},
    )

    assert from_summary["content"] == "Three verified conclusions"
    assert standalone["content"] == "A direct reply"


@pytest.mark.parametrize(
    ("steps", "expected_sources"),
    [
        (
            [
                {
                    "task_id": "search",
                    "primary_capability": "search",
                    "tool": "community.search_posts",
                    "label": "Search evidence",
                    "arguments": {"query": "Java"},
                },
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create draft",
                    "arguments": {"instruction": "Create from evidence"},
                    "depends_on": ["search"],
                },
                {
                    "task_id": "publish",
                    "primary_capability": "publishing",
                    "tool": "publication.publish_now",
                    "label": "Publish draft",
                    "depends_on": ["create"],
                },
            ],
            {
                "create": {"references": ["search"]},
                "publish": {
                    "draft_id": ["create"],
                    "expected_content_sha256": ["create"],
                },
            },
        ),
        (
            [
                {
                    "task_id": "inventory",
                    "primary_capability": "list_own_content",
                    "tool": "community.list_own_posts",
                    "label": "List own posts",
                },
                {
                    "task_id": "delete",
                    "primary_capability": "delete_content",
                    "tool": "community.delete_own_posts_batch",
                    "label": "Delete owned posts",
                    "depends_on": ["inventory"],
                },
            ],
            {"delete": {"post_ids": ["inventory"]}},
        ),
    ],
)
def test_different_community_workflows_compile_from_the_same_contracts(
    steps: list[dict[str, object]],
    expected_sources: dict[str, dict[str, list[str]]],
) -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "COMMUNITY_TASK",
            "summary": "Execute a community-scoped task",
            "steps": steps,
        }
    )

    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    compiled = {str(step.task_id): step for step in result.compiled_plan.steps}
    for task_id, sources in expected_sources.items():
        assert compiled[task_id].artifact_sources == sources
