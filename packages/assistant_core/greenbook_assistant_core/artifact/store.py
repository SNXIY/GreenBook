"""ArtifactStore — create, resolve, and query Artifacts across an execution.

Bridges the CapabilityExecutor output (ExecutionResult → ArtifactHandle)
with the StepExecution's declared input_artifact_types.
"""

from __future__ import annotations

import logging

from greenbook_assistant_core.execution.invocation import ExecutionResult
from greenbook_assistant_core.execution.models import StepExecution

from .models import Artifact
from .repository import ArtifactRepository

logger = logging.getLogger(__name__)


class ArtifactStore:
    """Manage artifact persistence and cross-step resolution."""

    def __init__(self, repository: ArtifactRepository | None = None) -> None:
        self._repo = repository or ArtifactRepository()

    # ── create ───────────────────────────────────────────────────

    def create_from_result(
        self,
        result: ExecutionResult,
        *,
        task_id: str = "",
        execution_id: str = "",
        step_id: str = "",
    ) -> Artifact | None:
        """Create an Artifact from a successful step ExecutionResult.

        Returns None when *result* has no artifact to persist.
        """
        if not result.ok:
            return None
        if result.artifact is None:
            return None
        if not result.artifact.artifact_type:
            return None

        artifact = Artifact(
            task_id=task_id,
            execution_id=execution_id,
            owner_task_id=task_id,
            owner_execution_id=execution_id,
            created_by_agent=result.capability or result.tool_name,
            step_id=step_id,
            artifact_type=result.artifact.artifact_type,
            resource_id=result.artifact.resource_id,
            resource_kind=_kind_from_type(result.artifact.artifact_type),
            summary=result.artifact.summary or "",
            metadata={
                "tool_name": result.tool_name,
                "capability": result.capability,
                "tool_result": result.tool_result.get("data") if result.tool_result else None,
            },
            metadata_schema="greenbook.tool_result.v1",
        )
        return self._repo.save(artifact)

    # ── resolve ──────────────────────────────────────────────────

    def resolve_inputs(
        self,
        step: StepExecution,
        execution_id: str,
    ) -> list[Artifact]:
        """Find the Artifacts a *step* needs based on its input_artifact_types.

        Walks the execution's artifact store looking for artifacts whose
        type matches one of the step's input_artifact_types.  When
        multiple candidates exist the most recent one is preferred.

        Returns an empty list when the step needs no inputs.
        """
        needed = step.input_artifact_types
        if not needed:
            return []

        all_artifacts = self._repo.find_by_execution(execution_id)
        resolved: list[Artifact] = []

        for art_type in needed:
            candidates = [a for a in all_artifacts if a.artifact_type == art_type]
            if candidates:
                # Prefer most recent (by created_at)
                candidates.sort(key=lambda a: a.created_at, reverse=True)
                resolved.append(candidates[0])
            else:
                logger.debug(
                    "Artifact type '%s' needed by step %s not found in "
                    "execution %s", art_type, step.step_id, execution_id,
                )

        return resolved

    def resolve_for_step_type(
        self,
        execution_id: str,
        artifact_type: str,
    ) -> Artifact | None:
        """Return the most recent artifact of *artifact_type* in *execution_id*."""
        candidates = self._repo.find_by_type(execution_id, artifact_type)
        if not candidates:
            return None
        candidates.sort(key=lambda a: a.created_at, reverse=True)
        return candidates[0]

    # ── queries ──────────────────────────────────────────────────

    def find_by_execution(self, execution_id: str) -> list[Artifact]:
        return self._repo.find_by_execution(execution_id)

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return self._repo.find_by_task(task_id)

    def find_by_step(self, step_id: str) -> Artifact | None:
        return self._repo.find_by_step(step_id)

    def count(self, execution_id: str) -> int:
        return len(self._repo.find_by_execution(execution_id))


# ── helpers ──────────────────────────────────────────────────────────

def _kind_from_type(artifact_type: str) -> str | None:
    mapping: dict[str, str] = {
        "DRAFT": "DRAFT",
        "SEARCH_RESULT": "POST",
        "SCHEDULE": "SCHEDULE",
        "ANALYSIS_REPORT": "ARTIFACT",
        "VALIDATION_REPORT": "ARTIFACT",
        "PERFORMANCE_DATA": "ARTIFACT",
        "COMMENT": "COMMENT",
    }
    return mapping.get(artifact_type)
