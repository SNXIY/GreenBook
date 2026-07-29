import pytest
from pydantic import ValidationError

from agents.moderation.reviewer_observability import safe_reviewer_metadata
from moderation.schemas import (
    EvidenceReviewerConfig,
    EvidenceReviewerDecision,
    ReviewerNextAction,
    ReviewerProblem,
    ReviewerProblemType,
    revision_signature_from_decision,
)


def problem(
    problem_type: ReviewerProblemType = ReviewerProblemType.MISSING_CONTEXT,
) -> ReviewerProblem:
    return ReviewerProblem(
        problem_type=problem_type,
        description="The missing context could change the decision.",
        affected_fields=["context_evidence"],
        severity="HIGH",
        supporting_evidence=["The text contains an unresolved pronoun."],
    )


def test_reviewer_acceptance_requires_finalize() -> None:
    decision = EvidenceReviewerDecision(
        passed=True,
        next_action=ReviewerNextAction.FINALIZE,
        confidence=0.91,
        reason="The decision is supported by current evidence and Policy.",
    )

    assert decision.problems == []

    with pytest.raises(ValidationError, match="if and only if"):
        EvidenceReviewerDecision(
            passed=True,
            next_action=ReviewerNextAction.HUMAN_REVIEW,
            confidence=0.91,
            reason="This output is contradictory.",
        )


def test_failed_review_requires_a_problem() -> None:
    with pytest.raises(ValidationError, match="at least one problem"):
        EvidenceReviewerDecision(
            passed=False,
            next_action=ReviewerNextAction.HUMAN_REVIEW,
            confidence=0.8,
            reason="The decision cannot be accepted.",
        )


@pytest.mark.parametrize(
    ("next_action", "extra", "message"),
    [
        (
            ReviewerNextAction.COLLECT_MORE_EVIDENCE,
            {},
            "missing_evidence and suggested_tools",
        ),
        (
            ReviewerNextAction.RETRIEVE_MORE_POLICY,
            {},
            "suggested_policy_queries",
        ),
        (
            ReviewerNextAction.REVISE_JUDGMENT,
            {},
            "judgment_revision_instructions",
        ),
    ],
)
def test_revision_actions_require_executable_feedback(next_action, extra, message) -> None:
    with pytest.raises(ValidationError, match=message):
        EvidenceReviewerDecision(
            passed=False,
            problems=[problem()],
            next_action=next_action,
            confidence=0.8,
            reason="A correction is required.",
            **extra,
        )


def test_revision_signature_is_stable_for_equivalent_feedback() -> None:
    first = EvidenceReviewerDecision(
        passed=False,
        problems=[problem()],
        next_action=ReviewerNextAction.COLLECT_MORE_EVIDENCE,
        missing_evidence=["Parent comment"],
        suggested_tools=["get_parent_comment"],
        confidence=0.8,
        reason="Context is missing.",
    )
    second = first.model_copy(
        update={
            "problems": [
                problem().model_copy(
                    update={"description": "  THE missing context could change the decision.  "}
                )
            ]
        }
    )

    assert revision_signature_from_decision(first).digest() == (
        revision_signature_from_decision(second).digest()
    )


def test_reviewer_config_rejects_route_budget_above_global_budget() -> None:
    with pytest.raises(ValidationError, match="cannot exceed max_iterations"):
        EvidenceReviewerConfig(max_iterations=1, max_judgment_revisions=2)


def test_reviewer_langsmith_metadata_is_allowlisted_bounded_and_redacted() -> None:
    metadata = safe_reviewer_metadata(
        {
            "trace_name": "evidence_reviewer",
            "moderation_task_id": "task-13812345678",
            "problem_types": ["MISSING_CONTEXT"],
            "next_action": "HUMAN_REVIEW",
            "reviewer_confidence": 0.8,
            "untrusted": "alice@example.com",
        }
    )

    assert metadata["trace_name"] == "evidence_reviewer"
    assert metadata["next_action"] == "HUMAN_REVIEW"
    assert "untrusted" not in metadata
    assert "13812345678" not in str(metadata)
    assert "alice@example.com" not in str(metadata)
