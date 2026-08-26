"""Joint evaluation contracts for the four logical Memory types.

These tests intentionally exercise the existing offline harness.  They do not
change production Memory behavior; quality findings are asserted as evidence
for the generated report rather than treated as permission to patch runtime
code during an evaluation-only checkpoint.
"""

from __future__ import annotations

import pytest

from scripts.memory_evaluation_harness import evaluate_long_term_memory_system


@pytest.fixture(scope="module")
def system_result() -> dict:
    return evaluate_long_term_memory_system()


def test_four_type_classification_and_boundaries(system_result: dict) -> None:
    classification = system_result["classification"]
    for metric in classification["metrics"]["per_type"].values():
        assert metric["precision"] == 1.0
        assert metric["recall"] == 1.0
    assert classification["metrics"]["wrong_type_admission_rate"] == 0.0
    assert classification["metrics"]["unsupported_inference_rate"] == 0.0
    assert classification["confusion"] == {}


def test_canonical_architecture_lifecycle_and_isolation(system_result: dict) -> None:
    architecture = system_result["architecture"]
    assert all(architecture["checks"].values())
    assert architecture["out_of_scope_paths"] == []

    lifecycle = system_result["lifecycle"]
    assert lifecycle["failed"] == 0
    assert lifecycle["passed"] == lifecycle["dataset_count"]

    retrieval = system_result["retrieval"]
    assert retrieval["cross_user_leaks"] == 0
    assert retrieval["cross_tenant_leaks"] == 0
    assert retrieval["override_failures"] == 0
    assert all(
        "joint-legacy-episodic" not in case["actual_ids"]
        for case in retrieval["cases"]
    )


def test_duplicate_and_authority_invariants(system_result: dict) -> None:
    duplicates = system_result["duplicates"]["metrics"]
    assert duplicates["duplicate_active_memory_rate"] == 0.0
    assert duplicates["episode_replay_same_id"] is True
    assert duplicates["distinct_episode_not_collapsed"] is True
    assert duplicates["preference_replay_same_id"] is True
    assert duplicates["semantic_replay_same_id"] is True
    assert duplicates["procedure_replay_same_id"] is True

    authority = system_result["authority"]
    assert authority["authority_violation_rate"] == 0.0
    assert all(authority["checks"].values())


def test_context_is_bounded_and_quality_findings_are_preserved(system_result: dict) -> None:
    budget = system_result["context_budget"]
    assert budget["metrics"]["bounded_context_rate"] == 1.0
    assert budget["by_shape"]["1"]["max_selected"] == 1
    assert budget["by_shape"]["4"]["max_selected"] == 4
    assert budget["by_shape"]["12"]["max_selected"] == 5

    # The system evaluation must preserve the current retrieval evidence for
    # diagnosis.  No production change is implied by these findings.
    retrieval = system_result["retrieval"]
    assert retrieval["no_match_false_return_rate"] == 0.0
    assert retrieval["irrelevant_memory_injection_rate"] > 0.0
    assert retrieval["required_memory_miss_rate"] > 0.0
