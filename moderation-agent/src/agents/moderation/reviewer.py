from collections.abc import Mapping
from typing import Any, get_args

from moderation.schemas import (
    AgenticPolicyRAGConfig,
    EvidenceReviewerConfig,
    EvidenceReviewerDecision,
    ModerationToolName,
    ReviewerNextAction,
    ToolCallingConfig,
    revision_signature_from_decision,
)
from rag.policy.text import normalize_policy_query


def validate_reviewer_route(
    state: Mapping[str, Any],
    decision: EvidenceReviewerDecision,
    *,
    reviewer_config: EvidenceReviewerConfig | None = None,
    tool_config: ToolCallingConfig | None = None,
    policy_config: AgenticPolicyRAGConfig | None = None,
) -> ReviewerNextAction:
    reviewer_config = reviewer_config or EvidenceReviewerConfig()
    tool_config = tool_config or ToolCallingConfig()
    policy_config = policy_config or AgenticPolicyRAGConfig()

    if decision.confidence < reviewer_config.min_reviewer_confidence:
        return ReviewerNextAction.HUMAN_REVIEW

    if decision.next_action == ReviewerNextAction.FINALIZE:
        if not decision.passed or not state.get("evidence_check_passed", False):
            return ReviewerNextAction.HUMAN_REVIEW
        return ReviewerNextAction.FINALIZE

    if state.get("reviewer_no_progress", False):
        return ReviewerNextAction.HUMAN_REVIEW

    revision_count = int(state.get("reviewer_revision_count", 0))
    if revision_count >= reviewer_config.max_iterations:
        return ReviewerNextAction.HUMAN_REVIEW

    signature = revision_signature_from_decision(decision).digest()
    previous_signatures = list(state.get("reviewer_revision_signatures", []))
    if previous_signatures and previous_signatures[-1] == signature:
        return ReviewerNextAction.HUMAN_REVIEW

    if decision.next_action == ReviewerNextAction.COLLECT_MORE_EVIDENCE:
        if not _valid_suggested_tools(decision.suggested_tools):
            return ReviewerNextAction.HUMAN_REVIEW
        if int(state.get("reviewer_tool_revision_count", 0)) >= (
            reviewer_config.max_tool_revisions
        ):
            return ReviewerNextAction.HUMAN_REVIEW
        if (
            state.get("tool_budget_exceeded", False)
            or int(state.get("tool_call_count", 0)) >= tool_config.max_total_calls
            or int(state.get("tool_call_round", 0)) >= tool_config.max_rounds
        ):
            return ReviewerNextAction.HUMAN_REVIEW
        return decision.next_action

    if decision.next_action == ReviewerNextAction.RETRIEVE_MORE_POLICY:
        if not policy_config.enabled:
            return ReviewerNextAction.HUMAN_REVIEW
        if int(state.get("reviewer_policy_revision_count", 0)) >= (
            reviewer_config.max_policy_revisions
        ):
            return ReviewerNextAction.HUMAN_REVIEW
        if (
            state.get("policy_rag_budget_exceeded", False)
            or int(state.get("policy_retrieval_round", 0)) >= policy_config.max_retrieval_rounds
        ):
            return ReviewerNextAction.HUMAN_REVIEW
        if not _has_new_policy_query(state, decision.suggested_policy_queries):
            return ReviewerNextAction.HUMAN_REVIEW
        return decision.next_action

    if decision.next_action == ReviewerNextAction.REVISE_JUDGMENT:
        if int(state.get("reviewer_judgment_revision_count", 0)) >= (
            reviewer_config.max_judgment_revisions
        ):
            return ReviewerNextAction.HUMAN_REVIEW
        return decision.next_action

    return ReviewerNextAction.HUMAN_REVIEW


def reviewer_revision_signature(decision: EvidenceReviewerDecision) -> str:
    return revision_signature_from_decision(decision).digest()


def _valid_suggested_tools(values: list[str]) -> bool:
    allowed = set(get_args(ModerationToolName))
    return bool(values) and all(value in allowed for value in values)


def _has_new_policy_query(state: Mapping[str, Any], queries: list[str]) -> bool:
    previous = {
        normalize_policy_query(str(query))
        for entry in state.get("policy_query_history", [])
        for query in entry.get("queries", [])
    }
    return any(normalize_policy_query(query) not in previous for query in queries)
