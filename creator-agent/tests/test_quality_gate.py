from datetime import datetime, timezone

import pytest

from app.creator.runtime.models import ArtifactKind, ArtifactRef
from app.creator.runtime.supervisor import CreatorSupervisorAgent, SupervisorPolicy


def _ref(kind: ArtifactKind, artifact_id: str, *, revision: int = 1, **metadata: object) -> ArtifactRef:
    return ArtifactRef(
        id=artifact_id,
        kind=kind,
        producer="test",
        step_id="test",
        revision=revision,
        metadata=metadata,
        content_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )


def test_quality_thresholds_are_ordered() -> None:
    policy = SupervisorPolicy(target_quality_threshold=0.70, minimum_publishable_threshold=0.60)
    assert policy.minimum_publishable_threshold < policy.target_quality_threshold


def test_quality_thresholds_reject_equal_values() -> None:
    with pytest.raises(ValueError):
        SupervisorPolicy(target_quality_threshold=0.70, minimum_publishable_threshold=0.70)


def test_target_score_is_passable() -> None:
    policy = SupervisorPolicy()
    assert 0.70 >= policy.target_quality_threshold


def test_between_thresholds_is_degraded() -> None:
    policy = SupervisorPolicy()
    assert policy.minimum_publishable_threshold <= 0.65 < policy.target_quality_threshold


def test_below_minimum_is_not_publishable() -> None:
    policy = SupervisorPolicy()
    assert 0.55 < policy.minimum_publishable_threshold


def test_hard_failure_is_not_publishable_even_with_high_score() -> None:
    draft = _ref(
        ArtifactKind.DRAFT,
        "draft-hard-failure",
        word_count=140,
        unsupported_claim_count=2,
    )
    assessment = CreatorSupervisorAgent._assess_publishability(draft)
    assert assessment["hard_failure"] is True


def test_best_draft_is_selected_by_critique_score() -> None:
    supervisor = CreatorSupervisorAgent(object())
    first = _ref(ArtifactKind.DRAFT, "draft-1", revision=1, word_count=120)
    second = _ref(ArtifactKind.DRAFT, "draft-2", revision=2, word_count=130)
    critique_one = _ref(
        ArtifactKind.CRITIQUE,
        "critique-1",
        reviewed_artifact_id="draft-1",
        overall_score=0.61,
    )
    critique_two = _ref(
        ArtifactKind.CRITIQUE,
        "critique-2",
        reviewed_artifact_id="draft-2",
        overall_score=0.68,
    )
    selected, score = supervisor._best_draft({"artifacts": {
        ref.id: ref for ref in (first, second, critique_one, critique_two)
    }})
    assert selected is not None and selected.id == "draft-2"
    assert score == 0.68
