"""Pure Assistant semantic compilation into a Runtime TaskContext."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from greenbook_assistant_core.task.intent_compat import to_task_intent
from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    ResolvedTaskTarget,
    Task,
    TaskIntent,
)

from ..models.runtime_context import TargetContext, TaskContext


class IntentCompilationError(ValueError):
    """Deterministic failure while building a bound TaskContext."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = candidates


class IntentCompiler:
    """Compile already-understood semantics into a detached TaskContext.

    This class deliberately has no access to raw user text and performs no
    keyword or intent inference.  Target selection must already be expressed
    by ``target_context`` or by the Conversation active binding.
    """

    def compile(
        self,
        *,
        intent_spec: IntentSpec | dict[str, Any] | None = None,
        task_intent: TaskIntent | None = None,
        target_context: TargetContext | ResolvedTaskTarget | None = None,
        task: Task | None = None,
        conversation: Any = None,
        artifacts: Iterable[ArtifactRef] = (),
        timezone: str = "Asia/Shanghai",
    ) -> TaskContext:
        del timezone  # Accepted as part of the boundary; time is already semantic.

        spec = self._coerce_spec(intent_spec)
        compiled_intent = self._compile_intent(spec, task_intent)
        task_id = self._resolve_task_id(
            compiled_intent,
            target_context=target_context,
            task=task,
            conversation=conversation,
        )
        self._validate_task(task, task_id)

        refs = self._validate_artifacts(artifacts, task_id)
        target = self._compile_target(
            task_id,
            target_context=target_context,
            conversation=conversation,
            artifacts=refs,
        )

        constraints = tuple(
            deepcopy(item)
            for item in (getattr(compiled_intent, "constraints", []) or [])
        )
        goal = (
            (task.goal if task is not None else "")
            or getattr(compiled_intent, "goal", "")
        )
        if not goal:
            raise IntentCompilationError(
                "GOAL_REQUIRED",
                "A TaskContext requires a non-empty goal.",
            )

        return TaskContext(
            task_id=task_id,
            goal=goal,
            task_intent=compiled_intent,
            target=target,
            constraints=constraints,
            active_artifact_id=target.artifact_id if target else None,
            artifact_refs=refs,
        )

    @staticmethod
    def _coerce_spec(
        intent_spec: IntentSpec | dict[str, Any] | None,
    ) -> IntentSpec | None:
        if intent_spec is None:
            return None
        if isinstance(intent_spec, IntentSpec):
            return intent_spec.model_copy(deep=True)
        if isinstance(intent_spec, dict):
            return IntentSpec.model_validate(deepcopy(intent_spec))
        raise IntentCompilationError(
            "INTENT_SPEC_INVALID",
            "intent_spec must be an IntentSpec or mapping.",
        )

    @staticmethod
    def _compile_intent(
        spec: IntentSpec | None,
        task_intent: TaskIntent | None,
    ) -> TaskIntent:
        if spec is not None:
            compiled = to_task_intent(spec)
            if task_intent is not None:
                # Preserve compatibility-only fields from the already-produced
                # projection, but never mutate that caller-owned object.
                compiled = task_intent.model_copy(deep=True)
            compiled.intent_spec = spec.model_dump(mode="json")
            return compiled
        if task_intent is not None:
            return task_intent.model_copy(deep=True)
        raise IntentCompilationError(
            "INTENT_REQUIRED",
            "IntentSpec or TaskIntent is required.",
        )

    @staticmethod
    def _requires_existing_task(intent: TaskIntent) -> bool:
        return str(intent.relation) not in {"NEW_TASK", "DIRECT"}

    def _resolve_task_id(
        self,
        intent: TaskIntent,
        *,
        target_context: TargetContext | ResolvedTaskTarget | None,
        task: Task | None,
        conversation: Any,
    ) -> str:
        if target_context is not None:
            if getattr(target_context, "is_ambiguous", False):
                candidates = tuple(getattr(target_context, "candidates", ()) or ())
                raise IntentCompilationError(
                    "AMBIGUOUS_TARGET",
                    "The request has multiple possible Task targets.",
                    candidates=candidates,
                )
            resolved_id = str(getattr(target_context, "task_id", "") or "")
            if resolved_id:
                return resolved_id

        active_task_id = str(
            getattr(conversation, "active_task_id", "") or ""
        )
        if self._requires_existing_task(intent):
            if active_task_id:
                return active_task_id
            raise IntentCompilationError(
                "TARGET_CONTEXT_REQUIRED",
                "An existing Task target is required for this operation.",
            )

        if task is not None and task.task_id:
            return task.task_id

        raise IntentCompilationError(
            "TASK_CONTEXT_REQUIRED",
            "A new Task must be created by the Assistant flow before compilation.",
        )

    @staticmethod
    def _validate_task(task: Task | None, task_id: str) -> None:
        if task is None:
            raise IntentCompilationError(
                "TASK_REQUIRED",
                "The resolved Task is required before compilation.",
            )
        if task.task_id != task_id:
            raise IntentCompilationError(
                "TASK_CONTEXT_MISMATCH",
                "Resolved task_id does not match the Task record.",
            )

    @staticmethod
    def _validate_artifacts(
        artifacts: Iterable[ArtifactRef],
        task_id: str,
    ) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        seen: set[str] = set()
        for raw_ref in artifacts:
            try:
                ref = (
                    raw_ref.model_copy(deep=True)
                    if isinstance(raw_ref, ArtifactRef)
                    else ArtifactRef.model_validate(raw_ref)
                )
            except Exception as exc:
                raise IntentCompilationError(
                    "ARTIFACT_REF_INVALID",
                    "Artifact references must conform to ArtifactRef.",
                ) from exc
            if ref.task_id != task_id:
                raise IntentCompilationError(
                    "ARTIFACT_TASK_MISMATCH",
                    "ArtifactRef.task_id must match TaskContext.task_id.",
                )
            if not ref.artifact_id or ref.artifact_id in seen:
                raise IntentCompilationError(
                    "ARTIFACT_REF_INVALID",
                    "Artifact references must have unique artifact_id values.",
                )
            if ref.resource_id and not ref.resource_kind:
                raise IntentCompilationError(
                    "ARTIFACT_REF_INVALID",
                    "resource_id requires resource_kind.",
                )
            seen.add(ref.artifact_id)
            refs.append(ref)
        return tuple(refs)

    @staticmethod
    def _compile_target(
        task_id: str,
        *,
        target_context: TargetContext | ResolvedTaskTarget | None,
        conversation: Any,
        artifacts: tuple[ArtifactRef, ...],
    ) -> TargetContext | None:
        artifact_id = getattr(target_context, "artifact_id", None)
        if not artifact_id:
            active_artifact_id = getattr(conversation, "active_artifact_id", None)
            if active_artifact_id and getattr(
                conversation, "active_task_id", None
            ) == task_id:
                artifact_id = active_artifact_id

        selected = None
        if artifact_id:
            selected = next(
                (ref for ref in artifacts if ref.artifact_id == artifact_id),
                None,
            )
            if selected is None:
                raise IntentCompilationError(
                    "ARTIFACT_NOT_FOUND",
                    "The active artifact does not belong to the resolved Task.",
                )

        resource_id = getattr(target_context, "resource_id", None)
        resource_kind = getattr(target_context, "resource_kind", None)
        if selected is not None:
            if resource_id and selected.resource_id != resource_id:
                raise IntentCompilationError(
                    "ARTIFACT_RESOURCE_MISMATCH",
                    "Resolved resource does not match the ArtifactRef.",
                )
            resource_id = selected.resource_id
            resource_kind = selected.resource_kind

        if target_context is None and selected is None:
            return None
        return TargetContext(
            task_id=task_id,
            artifact_id=artifact_id,
            resource_id=resource_id,
            resource_kind=resource_kind,
        )
