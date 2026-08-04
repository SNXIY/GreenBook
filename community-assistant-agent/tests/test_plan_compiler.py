from __future__ import annotations

from app.agent_registry import agent_registry
from app.domain import AgentPlan
from app.plan_compiler import PlanCompiler
from app.tools import tool_registry


def compiler() -> PlanCompiler:
    return PlanCompiler(tools=tool_registry, agents=agent_registry)


def test_compiler_rejects_composite_step_without_one_capability_owner() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Analyze active users and topics",
            "intent_detail": {
                "domain": "data_analysis",
                "goal": "Analyze active users and their post topics",
                "required_capabilities": [
                    "analysis",
                    "user_insight",
                    "trend_analysis",
                ],
                "confidence": 0.95,
            },
            "steps": [
                {
                    "task_id": "all-in-one",
                    "agent": "AnalyticsAgent",
                    "capabilities": [
                        "analysis",
                        "user_insight",
                        "trend_analysis",
                    ],
                    "tool": "community.analyze_engagement",
                    "label": "Analyze everything",
                }
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "NEEDS_REPLAN"
    assert "COMPOSITE_STEP_REQUIRES_DECOMPOSITION" in {
        item.code for item in result.diagnostics
    }


def test_compiler_routes_atomic_steps_and_confirms_goal_coverage() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Analyze active users and topics",
            "intent_detail": {
                "domain": "data_analysis",
                "goal": "Analyze active users and their post topics",
                "required_capabilities": [
                    "analysis",
                    "user_insight",
                    "trend_analysis",
                ],
                "confidence": 0.95,
            },
            "steps": [
                {
                    "task_id": "trend",
                    "primary_capability": "trend_analysis",
                    "capabilities": ["trend_analysis"],
                    "tool": "community.analyze_engagement",
                    "label": "Analyze topic trends",
                },
                {
                    "task_id": "users",
                    "primary_capability": "user_insight",
                    "capabilities": ["user_insight", "analysis"],
                    "tool": "community.analyze_engagement",
                    "label": "Analyze active users",
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert [step.agent for step in result.compiled_plan.steps] == [
        "AnalyticsAgent",
        "UserInsightAgent",
    ]
    assert all(step.success_criteria for step in result.compiled_plan.steps)


def test_compiler_returns_diagnostic_for_invented_tool() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Unknown tool",
            "steps": [
                {
                    "task_id": "unknown",
                    "primary_capability": "analysis",
                    "tool": "community.magic_analysis",
                    "label": "Magic analysis",
                }
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "NEEDS_REPLAN"
    assert result.diagnostics[0].code == "UNKNOWN_TOOL"


def test_compiler_rejects_missing_required_static_arguments() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "SEARCH",
            "summary": "Search without a query",
            "steps": [
                {
                    "task_id": "search",
                    "primary_capability": "search",
                    "tool": "community.search_posts",
                    "label": "Search community",
                    "arguments": {},
                }
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "NEEDS_REPLAN"
    assert result.diagnostics[0].code == "INVALID_TOOL_ARGUMENTS"


def test_compiler_accepts_runtime_bound_arguments_from_dependencies() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Analyze active user topics",
            "intent_detail": {
                "domain": "data_analysis",
                "goal": "Analyze active users and their post topics",
                "required_capabilities": ["user_insight", "trend_analysis"],
                "confidence": 0.95,
            },
            "steps": [
                {
                    "task_id": "users",
                    "primary_capability": "user_insight",
                    "tool": "community.list_active_users",
                    "label": "List active users",
                    "arguments": {"days": 30, "limit": 10},
                },
                {
                    "task_id": "posts",
                    "primary_capability": "user_insight",
                    "tool": "community.list_posts_by_users",
                    "label": "Read active users' posts",
                    "arguments": {
                        "days": 30,
                        "limit": 10,
                        "user_ids": ["AUTO"],
                    },
                    "depends_on": ["users"],
                },
                {
                    "task_id": "topics",
                    "primary_capability": "trend_analysis",
                    "tool": "community.aggregate_post_topics",
                    "label": "Aggregate post topics",
                    "arguments": {
                        "days": 30,
                        "limit": 10,
                        "user_ids": [],
                    },
                    "depends_on": ["users"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert "user_ids" not in result.compiled_plan.steps[1].arguments
    assert "user_ids" not in result.compiled_plan.steps[2].arguments


def test_compiler_strips_runtime_draft_fields_and_validates_relative_schedule() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "CREATE_AND_SCHEDULE",
            "summary": "Create and publish after five minutes",
            "steps": [
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create draft",
                    "arguments": {
                        "instruction": "Create from real analysis",
                        "references": [{"id": "invented"}],
                    },
                },
                {
                    "task_id": "schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.schedule",
                    "label": "Publish after five minutes",
                    "arguments": {
                        "delay_seconds": 300,
                        "draft_id": "AUTO",
                    },
                    "depends_on": ["create"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.steps[0].arguments == {
        "instruction": "Create from real analysis"
    }
    assert result.compiled_plan.steps[1].arguments == {"delay_seconds": 300}


def test_compiler_normalizes_model_capability_aliases_for_create_and_schedule() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "CREATE_AND_SCHEDULE",
            "summary": "Create a Redis post and publish it after five minutes",
            "intent_detail": {
                "domain": "content_publish",
                "goal": "Create a Redis post and schedule it",
                "required_capabilities": ["generation", "scheduling"],
                "confidence": 0.95,
            },
            "steps": [
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create draft",
                    "arguments": {"instruction": "Redis high concurrency"},
                },
                {
                    "task_id": "schedule",
                    "primary_capability": "scheduling",
                    "tool": "publication.schedule",
                    "label": "Schedule draft",
                    "arguments": {"delay_seconds": 300},
                    "depends_on": ["create"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.intent_detail is not None
    assert result.compiled_plan.intent_detail.required_capabilities == [
        "generation",
        "schedule_publish",
    ]
    assert result.compiled_plan.steps[1].primary_capability == "schedule_publish"


def test_compiler_supports_cross_turn_schedule_time_update() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "UPDATE_SCHEDULE",
            "summary": "Move the existing publication to ten minutes later",
            "intent_detail": {
                "domain": "content_publish",
                "goal": "Change the existing schedule",
                "required_capabilities": ["schedule_publish"],
                "confidence": 0.98,
            },
            "steps": [
                {
                    "task_id": "read-schedule",
                    "tool": "publication.get_schedule",
                    "label": "Revalidate schedule",
                    "arguments": {"action_id": "action-1"},
                },
                {
                    "task_id": "update-schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.update_schedule",
                    "label": "Update schedule",
                    "arguments": {"delay_seconds": 600},
                    "depends_on": ["read-schedule"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.steps[1].artifact_sources["action_id"] == [
        "read-schedule"
    ]
    assert result.compiled_plan.steps[1].arguments == {"delay_seconds": 600}


def test_compiler_preserves_schedule_when_revising_its_draft() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "REVISE_SCHEDULED_DRAFT",
            "summary": "Revise the draft and keep its publication commitment",
            "intent_detail": {
                "domain": "content_edit",
                "goal": "Add practical MySQL experience to the scheduled post",
                "required_capabilities": ["rewrite_content", "schedule_publish"],
                "confidence": 0.98,
            },
            "steps": [
                {
                    "task_id": "read-draft",
                    "tool": "community.get_own_draft",
                    "label": "Revalidate draft",
                    "arguments": {"draft_id": "draft-1"},
                },
                {
                    "task_id": "read-schedule",
                    "tool": "publication.get_schedule",
                    "label": "Revalidate schedule",
                    "arguments": {"action_id": "action-1"},
                },
                {
                    "task_id": "revise-draft",
                    "primary_capability": "rewrite_content",
                    "tool": "creator.revise_draft",
                    "label": "Revise draft",
                    "arguments": {"instruction": "Add practical MySQL experience"},
                    "depends_on": ["read-draft"],
                },
                {
                    "task_id": "retarget-schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.update_schedule",
                    "label": "Bind schedule to revised draft",
                    "depends_on": ["read-schedule", "revise-draft"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    update = result.compiled_plan.steps[-1]
    assert update.artifact_sources["action_id"] == ["read-schedule"]
    assert update.artifact_sources["draft_id"] == ["revise-draft"]
    assert update.artifact_sources["expected_content_sha256"] == [
        "revise-draft"
    ]


def test_compiler_cancels_existing_schedule_before_immediate_publish() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "PUBLISH_SCHEDULED_DRAFT_NOW",
            "summary": "Cancel delayed publication and publish now",
            "intent_detail": {
                "domain": "content_publish",
                "goal": "Publish the scheduled draft now",
                "required_capabilities": ["publishing", "schedule_publish"],
                "risk": "high",
                "confidence": 0.98,
            },
            "steps": [
                {
                    "task_id": "read-draft",
                    "tool": "community.get_own_draft",
                    "label": "Revalidate draft",
                    "arguments": {"draft_id": "draft-1"},
                },
                {
                    "task_id": "read-schedule",
                    "tool": "publication.get_schedule",
                    "label": "Revalidate schedule",
                    "arguments": {"action_id": "action-1"},
                },
                {
                    "task_id": "cancel-schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.cancel_schedule",
                    "label": "Cancel schedule",
                    "depends_on": ["read-schedule"],
                },
                {
                    "task_id": "publish-now",
                    "primary_capability": "publishing",
                    "tool": "publication.publish_now",
                    "label": "Publish now",
                    "depends_on": ["read-draft", "cancel-schedule"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.steps[-1].artifact_sources["draft_id"] == [
        "read-draft"
    ]


def test_compiler_accepts_revalidated_cross_turn_draft_before_publish() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "PUBLISH_CONTINUATION_DRAFT",
            "summary": "Revalidate and publish the prior draft",
            "intent_detail": {
                "domain": "content_publish",
                "goal": "Publish the immediately prior draft",
                "required_capabilities": ["publishing"],
                "risk": "high",
                "confidence": 1.0,
            },
            "steps": [
                {
                    "task_id": "resolve",
                    "agent": "PublishAgent",
                    "tool": "community.get_own_draft",
                    "label": "Revalidate draft",
                    "arguments": {"draft_id": "342506609282519040"},
                },
                {
                    "task_id": "publish",
                    "agent": "PublishAgent",
                    "primary_capability": "publishing",
                    "capabilities": ["publishing"],
                    "tool": "publication.publish_now",
                    "label": "Publish draft",
                    "arguments": {"draft_id": "AUTO"},
                    "depends_on": ["resolve"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.steps[1].arguments == {}
    assert result.compiled_plan.steps[1].artifact_sources == {
        "draft_id": ["resolve"],
        "expected_content_sha256": ["resolve"],
    }


def test_specific_capabilities_cover_their_general_goal_capabilities() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE_AND_SCHEDULE",
            "summary": "Analyze trends and schedule a post",
            "intent_detail": {
                "domain": "community_operation",
                "goal": "Analyze topics and schedule related content",
                "required_capabilities": [
                    "analysis",
                    "trend_analysis",
                    "publishing",
                    "schedule_publish",
                ],
                "confidence": 0.95,
            },
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
                    "arguments": {"user_ids": ["AUTO"]},
                    "depends_on": ["users"],
                },
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create related draft",
                    "arguments": {"instruction": "Create from topic analysis"},
                    "depends_on": ["topics"],
                },
                {
                    "task_id": "schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.schedule",
                    "label": "Schedule post",
                    "arguments": {
                        "delay_seconds": 300,
                        "draft_id": "AUTO",
                    },
                    "depends_on": ["create"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"


def test_runtime_bound_arguments_require_a_trusted_upstream_tool() -> None:
    plan = AgentPlan.model_validate(
        {
            "intent": "ANALYZE",
            "summary": "Aggregate topics without an active-user source",
            "steps": [
                {
                    "task_id": "topics",
                    "primary_capability": "trend_analysis",
                    "tool": "community.aggregate_post_topics",
                    "label": "Aggregate topics",
                    "arguments": {"user_ids": ["AUTO"]},
                }
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "NEEDS_REPLAN"
    assert result.diagnostics[0].code == "MISSING_RUNTIME_ARGUMENT_SOURCE"


def test_draft_revision_alias_does_not_trigger_replan_loop() -> None:
    """Regression for the real failed 'add Java code' planner output."""
    plan = AgentPlan.model_validate(
        {
            "intent": "APPEND_CONTENT",
            "summary": "Add Java code to the selected draft",
            "intent_detail": {
                "domain": "content_edit",
                "goal": "Add Java code to the current draft",
                "required_capabilities": ["generation", "draft_revision"],
                "confidence": 0.98,
            },
            "steps": [
                {
                    "task_id": "read",
                    "primary_capability": "publishing",
                    "tool": "community.get_own_draft",
                    "label": "Read current draft",
                    "arguments": {"draft_id": "342609701743235072"},
                },
                {
                    "task_id": "revise",
                    "primary_capability": "rewrite_content",
                    "capabilities": ["rewrite_content", "draft_revision"],
                    "tool": "creator.revise_draft",
                    "label": "Add Java code",
                    "arguments": {"instruction": "Add Java code"},
                    "depends_on": ["read"],
                },
            ],
        }
    )

    result = compiler().compile(plan)

    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    assert result.compiled_plan.steps[1].capabilities == ["rewrite_content"]
