"""Frozen business-semantic seeds for control-plane A/B evaluation.

The expected work-item bindings in this module are hand-authored ground truth.
Paraphrases may be added later, but an LLM is never allowed to generate or
rewrite the oracle.  This is an evaluation contract, not a second interpreter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import EvalCase


@dataclass(frozen=True)
class SeedExpression:
    variant: str
    user_message: str
    conversation_turns: tuple[dict[str, str], ...] = ()
    counterfactual: bool = False


@dataclass(frozen=True)
class BusinessSemanticSeed:
    seed_id: str
    category: str
    expected_semantic_state: dict[str, Any]
    expected_work_items: tuple[dict[str, Any], ...]
    expressions: tuple[SeedExpression, ...]
    counterfactual_items: tuple[dict[str, Any], ...] = ()


def _multi_state() -> dict[str, Any]:
    return {
        "action_family": "MULTI_OBJECTIVE",
        "publication_mode": "MIXED",
        "temporal_kind": "MIXED",
        "temporal_resolved": True,
        "target_state": "NONE",
        "clarification_required": False,
        "objective_count": 2,
        "task_expectation": "READY",
    }


def _schedule_state() -> dict[str, Any]:
    return {
        "action_family": "UPDATE_SCHEDULE",
        "publication_mode": "SCHEDULED",
        "temporal_kind": "FUTURE",
        "temporal_resolved": True,
        "target_state": "RESOLVED",
        "clarification_required": False,
        "objective_count": 1,
        "task_expectation": "READY",
    }


def _clarify_state(action_family: str, publication_mode: str, temporal_kind: str) -> dict[str, Any]:
    return {
        "action_family": action_family,
        "publication_mode": publication_mode,
        "temporal_kind": temporal_kind,
        "temporal_resolved": False,
        "target_state": "AMBIGUOUS" if action_family == "DELETE" else "NONE",
        "clarification_required": True,
        "objective_count": 1,
        "task_expectation": "CLARIFY",
    }


BUSINESS_SEMANTIC_SEEDS: tuple[BusinessSemanticSeed, ...] = (
    BusinessSemanticSeed(
        seed_id="multi-publication-binding",
        category="MULTI_OBJECTIVE",
        expected_semantic_state=_multi_state(),
        expected_work_items=(
            {"subject": "Java backend learning", "desired_outcome": "PUBLISHED", "temporal": "NOW"},
            {"subject": "Agent development learning", "desired_outcome": "SCHEDULED", "temporal": "FIVE_MINUTES"},
        ),
        counterfactual_items=(
            {"subject": "Java backend learning", "desired_outcome": "SCHEDULED", "temporal": "FIVE_MINUTES"},
            {"subject": "Agent development learning", "desired_outcome": "PUBLISHED", "temporal": "NOW"},
        ),
        expressions=(
            SeedExpression("standard", "Create Java backend learning now, and Agent development learning in five minutes."),
            SeedExpression("colloquial", "Write the Java one and post it now; let the Agent one go out five minutes later."),
            SeedExpression("reordered", "Five minutes from now publish the Agent development learning post; publish Java backend learning immediately."),
            SeedExpression("list", "1) Java backend learning — publish now. 2) Agent development learning — publish in five minutes."),
            SeedExpression("polite", "Please create both posts: publish Java backend learning right away and schedule Agent development learning for five minutes from now."),
            SeedExpression("referential", "For the Java backend topic, publish it now. For the Agent development topic, wait five minutes before publishing."),
            SeedExpression("counterfactual", "Create Agent development learning now, and Java backend learning in five minutes.", counterfactual=True),
        ),
    ),
    BusinessSemanticSeed(
        seed_id="cross-turn-schedule-update",
        category="CROSS_TURN",
        expected_semantic_state=_schedule_state(),
        expected_work_items=(
            {"subject": "Java backend learning", "desired_outcome": "SCHEDULE_UPDATED", "target": "existing Java schedule", "temporal": "TOMORROW_14_00"},
        ),
        expressions=(
            SeedExpression("standard", "Move the Java post to tomorrow at 14:00.", ({"role": "user", "content": "Create Java backend learning and publish it in five minutes."},)),
            SeedExpression("colloquial", "Actually, put that Java one on for tomorrow afternoon at two.", ({"role": "user", "content": "Schedule the Java post in five minutes."},)),
            SeedExpression("reordered", "Tomorrow at 14:00 is the new publication time for the Java post.", ({"role": "user", "content": "The Java post is scheduled."},)),
            SeedExpression("list", "Update: Java post -> tomorrow 14:00.", ({"role": "user", "content": "Create the Java post."},)),
            SeedExpression("polite", "Could you please reschedule the existing Java post to tomorrow at 2 PM?", ({"role": "user", "content": "Schedule the Java post."},)),
            SeedExpression("referential", "Change that schedule to tomorrow at 2 PM.", ({"role": "user", "content": "Schedule the Java post."},)),
            SeedExpression("counterfactual", "Move the Java post to tomorrow at 16:00.", ({"role": "user", "content": "Create Java backend learning and publish it in five minutes."},), counterfactual=True),
        ),
    ),
    BusinessSemanticSeed(
        seed_id="ambiguous-delete",
        category="CLARIFICATION",
        expected_semantic_state=_clarify_state("DELETE", "NONE", "NONE"),
        expected_work_items=(
            {"subject": "Java post", "desired_outcome": "DELETED", "target": "AMBIGUOUS"},
        ),
        expressions=(
            SeedExpression("standard", "Delete the Java post."),
            SeedExpression("colloquial", "Get rid of that Java one."),
            SeedExpression("reordered", "The Java post should be deleted."),
            SeedExpression("list", "Delete: Java post."),
            SeedExpression("polite", "Please delete the Java post."),
            SeedExpression("referential", "Remove the Java one I mentioned."),
            SeedExpression("counterfactual", "Delete the uniquely identified Java post.", counterfactual=True),
        ),
    ),
    BusinessSemanticSeed(
        seed_id="unresolved-future",
        category="CLARIFICATION",
        expected_semantic_state=_clarify_state("SCHEDULE", "UNRESOLVED", "UNRESOLVED"),
        expected_work_items=(
            {"subject": "Agent post", "desired_outcome": "SCHEDULED", "target": "unresolved time"},
        ),
        expressions=(
            SeedExpression("standard", "Schedule the Agent post sometime later."),
            SeedExpression("colloquial", "Put the Agent one out whenever works."),
            SeedExpression("reordered", "Later, publish the Agent post."),
            SeedExpression("list", "Agent post -> publish later."),
            SeedExpression("polite", "Please schedule the Agent post, but I have not decided the time yet."),
            SeedExpression("referential", "Set the Agent one for some time in the future."),
            SeedExpression("counterfactual", "Schedule the Agent post for tomorrow at 14:00.", counterfactual=True),
        ),
    ),
    BusinessSemanticSeed(
        seed_id="search-create-schedule",
        category="SEARCH_CREATE",
        expected_semantic_state={
            "action_family": "CREATE",
            "publication_mode": "SCHEDULED",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "target_state": "NONE",
            "clarification_required": False,
            "objective_count": 1,
            "task_expectation": "READY",
        },
        expected_work_items=(
            {"subject": "Agent learning", "desired_outcome": "SCHEDULED", "requires_evidence": True, "temporal": "FIVE_MINUTES"},
        ),
        expressions=(
            SeedExpression("standard", "Search Agent learning posts, use them as evidence, then write one and publish it in five minutes."),
            SeedExpression("colloquial", "Look up Agent stuff first, make me a post from it, and send it out five minutes later."),
            SeedExpression("reordered", "In five minutes schedule a new Agent learning post, after researching related posts."),
            SeedExpression("list", "1) Search Agent learning. 2) Write from the results. 3) Publish in five minutes."),
            SeedExpression("polite", "Please research Agent learning posts, create a post based on the evidence, and schedule it five minutes from now."),
            SeedExpression("referential", "Use the Agent learning posts you find, turn them into a new post, and schedule it five minutes later."),
            SeedExpression("counterfactual", "Search Agent learning posts, use them as evidence, then write one and save it as a draft.", counterfactual=True),
        ),
    ),
)


def business_semantic_seed_cases() -> list[EvalCase]:
    """Expand frozen expressions into cases for the existing evaluator."""

    cases: list[EvalCase] = []
    for seed in BUSINESS_SEMANTIC_SEEDS:
        for expression in seed.expressions:
            cases.append(
                EvalCase(
                    case_id=f"seed-{seed.seed_id}-{expression.variant}",
                    category=seed.category,
                    description="Frozen Business Semantic Seed expression",
                    user_message=expression.user_message,
                    conversation_turns=list(expression.conversation_turns),
                    expected_semantic_state=dict(seed.expected_semantic_state),
                    expected_objective_count=None,
                    expected_temporal_resolution=None,
                    expected_clarification=None,
                    expected_task_state=None,
                    initial_state={"seed_id": seed.seed_id, "variant": expression.variant},
                )
            )
    return cases


BUSINESS_SEMANTIC_SEED_CASES = business_semantic_seed_cases()

__all__ = [
    "BUSINESS_SEMANTIC_SEEDS",
    "BUSINESS_SEMANTIC_SEED_CASES",
    "BusinessSemanticSeed",
    "SeedExpression",
    "business_semantic_seed_cases",
]
