from __future__ import annotations

from app.database import Artifact, PolicyAudit, ToolJob
from app.domain import CommunityIntent
from app.evaluation import evaluate_retrieval, evaluate_runtime
from app.policy import (
    PolicyContext,
    PolicyDecisionType,
    community_policy,
)
from app.skill_registry import skill_registry
from app.tools import ExecutionMode, tool_registry


def context_for(action: str, *, approved: bool = False) -> PolicyContext:
    return PolicyContext(
        run_id="run-1",
        user_id="user-1",
        tenant_id="zhiguang",
        principal_role="USER",
        action=action,
        resource={
            "authority": "JAVA",
            "resource_id": "post-1",
            "side_effecting": tool_registry.get(action).side_effecting,
            "risk": tool_registry.get(action).risk.value,
            "open_world": False,
        },
        approval_granted=approved,
    )


def test_policy_allows_reads_and_requires_exact_write_approval() -> None:
    search = tool_registry.get("community.search_posts")
    search_decision = community_policy.evaluate(
        context=context_for("community.search_posts"),
        definition=search,
        registry=tool_registry,
    )
    assert search_decision.decision == PolicyDecisionType.ALLOW

    delete = tool_registry.get("community.delete_post")
    pending = community_policy.evaluate(
        context=context_for("community.delete_post"),
        definition=delete,
        registry=tool_registry,
    )
    approved = community_policy.evaluate(
        context=context_for("community.delete_post", approved=True),
        definition=delete,
        registry=tool_registry,
    )
    assert pending.decision == PolicyDecisionType.REQUIRE_APPROVAL
    assert approved.decision == PolicyDecisionType.ALLOW


def test_policy_keeps_moderation_out_of_user_assistant_scope() -> None:
    definition = tool_registry.get("moderation.check_draft")
    decision = community_policy.evaluate(
        context=context_for("moderation.check_draft"),
        definition=definition,
        registry=tool_registry,
    )
    assert decision.decision == PolicyDecisionType.DENY


def test_skill_registry_activates_only_relevant_capabilities() -> None:
    intent = CommunityIntent(
        domain="content_delete",
        goal="删除我自己的帖子",
        required_capabilities=["list_own_content", "delete_content"],
        risk="high",
        confidence=0.99,
    )
    names = {item.name for item in skill_registry.for_intent(intent)}
    assert names == {"content-management"}
    assert skill_registry.signature()


def test_runtime_models_publish_separate_governance_tables() -> None:
    assert Artifact.__tablename__ == "assistant_artifacts"
    assert PolicyAudit.__tablename__ == "assistant_policy_audits"
    assert ToolJob.__tablename__ == "assistant_tool_jobs"


def test_discovered_mcp_tools_use_durable_async_execution() -> None:
    from app.tools import ToolRegistry

    registry = ToolRegistry([])
    registry.register_mcp_tool(
        name="mcp.creator.get_creator_profile",
        label="creator",
        description="creator profile",
    )
    assert (
        registry.get("mcp.creator.get_creator_profile").execution_mode
        == ExecutionMode.ASYNC
    )


def test_runtime_and_retrieval_metrics_are_deterministic() -> None:
    runtime = evaluate_runtime(
        resumed_tasks=4,
        recovered_tasks=3,
        stale_results=2,
        rejected_stale_results=2,
        approval_decisions=5,
        correct_approval_decisions=5,
        artifact_versions=8,
        correct_artifact_versions=8,
        terminal_tool_jobs=10,
        completed_tool_jobs=9,
    )
    retrieval = evaluate_retrieval(
        relevant_by_query=[{"a"}, {"d"}],
        ranked_results=[["a", "b"], ["c", "d"]],
    )
    assert runtime.task_recovery_rate == 0.75
    assert runtime.stale_result_rejection_rate == 1.0
    assert retrieval.hit_rate == 1.0
    assert retrieval.mrr == 0.75
