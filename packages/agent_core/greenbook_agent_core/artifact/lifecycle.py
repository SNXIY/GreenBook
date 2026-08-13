"""Explicit Artifact lifecycle transition and input validation rules."""

from __future__ import annotations

from .models import Artifact, ArtifactLifecycle


class ArtifactLifecycleError(ValueError):
    pass


class ArtifactLifecycleValidator:
    """Reject invalid transitions and non-consumable Artifact references."""

    _TRANSITIONS = {
        ArtifactLifecycle.CREATED: {ArtifactLifecycle.AVAILABLE, ArtifactLifecycle.FAILED},
        ArtifactLifecycle.AVAILABLE: {ArtifactLifecycle.CONSUMED, ArtifactLifecycle.ARCHIVED, ArtifactLifecycle.FAILED},
        ArtifactLifecycle.CONSUMED: {ArtifactLifecycle.ARCHIVED},
        ArtifactLifecycle.ARCHIVED: set(),
        ArtifactLifecycle.FAILED: {ArtifactLifecycle.ARCHIVED},
    }

    @classmethod
    def validate_transition(
        cls,
        current: ArtifactLifecycle,
        target: ArtifactLifecycle,
    ) -> None:
        if target not in cls._TRANSITIONS.get(current, set()):
            raise ArtifactLifecycleError(
                f"INVALID_ARTIFACT_TRANSITION:{current.value}->{target.value}"
            )

    @classmethod
    def validate_input(cls, artifact: Artifact, *, reusable: bool = False) -> None:
        if artifact.lifecycle != ArtifactLifecycle.AVAILABLE:
            if artifact.lifecycle == ArtifactLifecycle.CREATED:
                code = "ARTIFACT_NOT_AVAILABLE"
            elif artifact.lifecycle == ArtifactLifecycle.ARCHIVED:
                code = "ARTIFACT_ARCHIVED"
            elif artifact.lifecycle == ArtifactLifecycle.CONSUMED and not reusable:
                code = "ARTIFACT_ALREADY_CONSUMED"
            else:
                code = "ARTIFACT_NOT_CONSUMABLE"
            raise ArtifactLifecycleError(code)


__all__ = ["ArtifactLifecycleError", "ArtifactLifecycleValidator"]
