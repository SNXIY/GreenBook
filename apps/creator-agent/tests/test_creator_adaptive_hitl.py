from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.creator.domain.models import CreatorGoal
from app.creator.runtime.models import ArtifactKind, ArtifactRef
from app.creator.runtime.supervisor import CreatorSupervisorAgent


def _ref(kind: ArtifactKind, confidence: float) -> ArtifactRef:
    return ArtifactRef(
        id=f"artifact-{kind.value.lower()}",
        kind=kind,
        producer="test",
        step_id="test-step",
        revision=1,
        confidence=confidence,
        content_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )


def _state(
    *,
    mode: str,
    topic_confidence: float = 0.8,
    evidence_confidence: float = 0.8,
    outline_confidence: float = 0.82,
) -> dict:
    refs = (
        _ref(ArtifactKind.TOPIC_OPTIONS, topic_confidence),
        _ref(ArtifactKind.EVIDENCE_PACK, evidence_confidence),
        _ref(ArtifactKind.CONTENT_OUTLINE, outline_confidence),
    )
    return {
        "goal": CreatorGoal(
            text="测试自适应审批",
            constraints={"approval_mode": mode},
        ),
        "artifacts": {ref.id: ref for ref in refs},
    }


class CreatorAdaptiveHitlTests(unittest.TestCase):
    def test_adaptive_skips_high_confidence_topic_and_outline_gates(self) -> None:
        state = _state(mode="ADAPTIVE")

        self.assertTrue(CreatorSupervisorAgent._topic_is_approved(state))
        self.assertTrue(CreatorSupervisorAgent._outline_is_approved(state))

    def test_adaptive_stops_when_evidence_or_outline_confidence_is_low(self) -> None:
        weak_evidence = _state(mode="ADAPTIVE", evidence_confidence=0.3)
        weak_outline = _state(mode="ADAPTIVE", outline_confidence=0.6)

        self.assertFalse(CreatorSupervisorAgent._topic_is_approved(weak_evidence))
        self.assertFalse(CreatorSupervisorAgent._outline_is_approved(weak_outline))

    def test_guided_keeps_gates_and_auto_skips_them(self) -> None:
        guided = _state(mode="GUIDED")
        automatic = _state(
            mode="AUTO",
            topic_confidence=0.1,
            evidence_confidence=0.1,
            outline_confidence=0.1,
        )

        self.assertFalse(CreatorSupervisorAgent._topic_is_approved(guided))
        self.assertFalse(CreatorSupervisorAgent._outline_is_approved(guided))
        self.assertTrue(CreatorSupervisorAgent._topic_is_approved(automatic))
        self.assertTrue(CreatorSupervisorAgent._outline_is_approved(automatic))


if __name__ == "__main__":
    unittest.main()
