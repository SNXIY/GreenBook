from agents.moderation.reviewer import (
    reviewer_revision_signature,
    validate_reviewer_route,
)
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    EvidenceReviewerConfig,
    EvidenceReviewerDecision,
    ReviewerNextAction,
    ReviewerProblem,
    ReviewerProblemType,
    ToolCallingConfig,
)


def decision(
    next_action: ReviewerNextAction,
    *,
    confidence: float = 0.9,
    suggested_tools: list[str] | None = None,
    suggested_policy_queries: list[str] | None = None,
) -> EvidenceReviewerDecision:
    if next_action == ReviewerNextAction.FINALIZE:
        return EvidenceReviewerDecision(
            passed=True,
            next_action=next_action,
            confidence=confidence,
            reason="The evidence and action are consistent.",
        )
    return EvidenceReviewerDecision(
        passed=False,
        problems=[
            ReviewerProblem(
                problem_type=(
                    ReviewerProblemType.MISSING_CONTEXT
                    if next_action == ReviewerNextAction.COLLECT_MORE_EVIDENCE
                    else ReviewerProblemType.POLICY_INSUFFICIENT
                    if next_action == ReviewerNextAction.RETRIEVE_MORE_POLICY
                    else ReviewerProblemType.RISK_SCORE_MISMATCH
                ),
                description="The current decision requires a bounded correction.",
                affected_fields=["agent_decision"],
                severity="HIGH",
            )
        ],
        next_action=next_action,
        missing_evidence=(
            ["The parent comment is missing."]
            if next_action == ReviewerNextAction.COLLECT_MORE_EVIDENCE
            else []
        ),
        suggested_tools=suggested_tools or [],
        suggested_policy_queries=suggested_policy_queries or [],
        judgment_revision_instructions=(
            ["Recalibrate the score using only verified evidence."]
            if next_action == ReviewerNextAction.REVISE_JUDGMENT
            else []
        ),
        confidence=confidence,
        reason="The current decision is not ready to finalize.",
    )


def test_valid_finalize_requires_deterministic_evidence_check() -> None:
    accepted = decision(ReviewerNextAction.FINALIZE)
    assert validate_reviewer_route({"evidence_check_passed": True}, accepted) == (
        ReviewerNextAction.FINALIZE
    )
    assert validate_reviewer_route({"evidence_check_passed": False}, accepted) == (
        ReviewerNextAction.HUMAN_REVIEW
    )


def test_valid_tool_policy_and_judgment_revisions_are_preserved() -> None:
    tool_decision = decision(
        ReviewerNextAction.COLLECT_MORE_EVIDENCE,
        suggested_tools=["get_parent_comment"],
    )
    policy_decision = decision(
        ReviewerNextAction.RETRIEVE_MORE_POLICY,
        suggested_policy_queries=["third-party phone number authorization policy"],
    )
    judge_decision = decision(ReviewerNextAction.REVISE_JUDGMENT)

    assert validate_reviewer_route({}, tool_decision) == tool_decision.next_action
    assert validate_reviewer_route({}, policy_decision) == policy_decision.next_action
    assert validate_reviewer_route({}, judge_decision) == judge_decision.next_action


def test_illegal_suggested_tool_is_forced_to_human_review() -> None:
    result = validate_reviewer_route(
        {},
        decision(
            ReviewerNextAction.COLLECT_MORE_EVIDENCE,
            suggested_tools=["execute_sql"],
        ),
    )

    assert result == ReviewerNextAction.HUMAN_REVIEW


def test_low_reviewer_confidence_is_forced_to_human_review() -> None:
    result = validate_reviewer_route(
        {"evidence_check_passed": True},
        decision(ReviewerNextAction.FINALIZE, confidence=0.64),
    )

    assert result == ReviewerNextAction.HUMAN_REVIEW


def test_tool_budget_exhaustion_is_forced_to_human_review() -> None:
    requested = decision(
        ReviewerNextAction.COLLECT_MORE_EVIDENCE,
        suggested_tools=["get_conversation_context"],
    )
    config = ToolCallingConfig(max_total_calls=2)

    assert (
        validate_reviewer_route({"tool_call_count": 2}, requested, tool_config=config)
        == ReviewerNextAction.HUMAN_REVIEW
    )


def test_policy_budget_or_duplicate_query_is_forced_to_human_review() -> None:
    query = "unauthorized third-party phone publication"
    requested = decision(
        ReviewerNextAction.RETRIEVE_MORE_POLICY,
        suggested_policy_queries=[query],
    )
    config = AgenticPolicyRAGConfig(max_retrieval_rounds=1)

    assert (
        validate_reviewer_route({"policy_retrieval_round": 1}, requested, policy_config=config)
        == ReviewerNextAction.HUMAN_REVIEW
    )
    assert (
        validate_reviewer_route({"policy_query_history": [{"queries": [query]}]}, requested)
        == ReviewerNextAction.HUMAN_REVIEW
    )


def test_per_route_and_global_revision_budgets_are_enforced() -> None:
    tool_request = decision(
        ReviewerNextAction.COLLECT_MORE_EVIDENCE,
        suggested_tools=["get_parent_comment"],
    )
    judge_request = decision(ReviewerNextAction.REVISE_JUDGMENT)

    assert (
        validate_reviewer_route({"reviewer_tool_revision_count": 1}, tool_request)
        == ReviewerNextAction.HUMAN_REVIEW
    )
    assert (
        validate_reviewer_route({"reviewer_judgment_revision_count": 2}, judge_request)
        == ReviewerNextAction.HUMAN_REVIEW
    )
    assert (
        validate_reviewer_route({"reviewer_revision_count": 2}, judge_request)
        == ReviewerNextAction.HUMAN_REVIEW
    )


def test_repeated_revision_signature_and_no_progress_are_forced_to_human_review() -> None:
    requested = decision(ReviewerNextAction.REVISE_JUDGMENT)
    signature = reviewer_revision_signature(requested)

    assert (
        validate_reviewer_route({"reviewer_revision_signatures": [signature]}, requested)
        == ReviewerNextAction.HUMAN_REVIEW
    )
    assert (
        validate_reviewer_route({"reviewer_no_progress": True}, requested)
        == ReviewerNextAction.HUMAN_REVIEW
    )


def test_explicit_human_review_remains_human_review() -> None:
    requested = decision(ReviewerNextAction.HUMAN_REVIEW)
    assert validate_reviewer_route({}, requested) == ReviewerNextAction.HUMAN_REVIEW


def test_zero_route_budget_is_supported_when_global_budget_remains() -> None:
    config = EvidenceReviewerConfig(
        max_iterations=2,
        max_tool_revisions=0,
        max_policy_revisions=1,
        max_judgment_revisions=2,
    )
    requested = decision(
        ReviewerNextAction.COLLECT_MORE_EVIDENCE,
        suggested_tools=["get_parent_comment"],
    )

    assert validate_reviewer_route({}, requested, reviewer_config=config) == (
        ReviewerNextAction.HUMAN_REVIEW
    )
