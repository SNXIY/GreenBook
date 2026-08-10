"""Formal IntentSpec boundary for Runtime message migrations.

The legacy ``TaskUnderstanding`` API intentionally continues to return a
``TaskIntent``.  Runtime callers use this provider instead so that every
successful result is a schema-validated and semantically validated
``IntentSpec``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .intent_models import (
    ActionType,
    ConstraintType,
    IntentAction,
    IntentConstraint,
    IntentSpec,
    ResourceType,
)
from .intent_validator import IntentValidator
from .models import TaskIntent
from .understanding import TaskUnderstanding


class IntentSpecProviderError(ValueError):
    """Stable error raised when no valid formal IntentSpec is available."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        validation_result: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.validation_result = validation_result


class IntentSpecProvider:
    """Resolve user text to one validated formal IntentSpec.

    This is a semantic boundary only.  It does not create Tasks, resolve
    TaskContext, generate plans, or execute Runtime work.
    """

    def __init__(
        self,
        understanding: TaskUnderstanding | None = None,
        *,
        validator: IntentValidator | None = None,
        llm: Any | None = None,
        model: str = "",
    ) -> None:
        self._understanding = understanding or TaskUnderstanding(
            llm=llm,
            model=model,
        )
        self._validator = validator or IntentValidator()
        self._last_validation_result: Any | None = None

    @property
    def last_validation_result(self) -> Any | None:
        """Return the most recent validation result for diagnostics/tests."""

        return self._last_validation_result

    async def resolve(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec:
        """Return a schema- and semantic-validated IntentSpec.

        Direct L2 already performs its parse/validation/targeted-repair cycle
        inside TaskUnderstanding.  This boundary validates the resulting
        snapshot again and projects only the explicitly supported, unambiguous
        L1 CREATE_CONTENT shape.  A legacy L2 TaskIntent without a formal
        snapshot is rejected instead of being silently converted.
        """

        intent = await self._understanding.understand(
            user_message,
            existing_tasks=existing_tasks,
        )

        if intent.intent_spec is not None:
            candidate: Any = intent.intent_spec
        elif intent.source == "L1":
            candidate = self._project_l1_create_content(intent)
        else:
            raise IntentSpecProviderError(
                "INTENT_SPEC_UNAVAILABLE",
                "Formal IntentSpec is unavailable for the L2 understanding result.",
            )

        return self._validate(candidate, user_message, source=intent.source)

    async def provide(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec:
        """Alias for callers that use provider terminology."""

        return await self.resolve(
            user_message,
            existing_tasks=existing_tasks,
        )

    def _project_l1_create_content(self, intent: TaskIntent) -> IntentSpec:
        """Project only the clear single CREATE_CONTENT L1 contract.

        L1 currently also emits composite resource requests (for example a
        content draft plus a schedule).  Until those semantics have their own
        formal mapping, accepting them here would silently drop actions.  The
        provider therefore fails closed for anything beyond a single content
        creation request.
        """

        if str(intent.goal_category) != "CREATE_CONTENT":
            raise IntentSpecProviderError(
                "INTENT_UNSUPPORTED",
                "This L1 intent has no lossless formal IntentSpec projection.",
            )
        if str(intent.relation) != "NEW_TASK":
            raise IntentSpecProviderError(
                "INTENT_UNSUPPORTED",
                "CREATE_CONTENT L1 projection requires a NEW_TASK relation.",
            )
        if not intent.goal.strip():
            raise IntentSpecProviderError(
                "INTENT_SPEC_INVALID",
                "A formal IntentSpec requires a non-empty goal.",
            )

        requirement_types = {
            str(item.get("type", "")).upper()
            for item in intent.requirements
            if isinstance(item, dict)
        }
        if requirement_types - {"CREATE"}:
            raise IntentSpecProviderError(
                "INTENT_UNSUPPORTED",
                "L1 CREATE_CONTENT contains actions not covered by the simple projection.",
            )

        resource_types = {
            str(item.get("resource_type", "")).upper()
            for item in intent.resource_requests
            if isinstance(item, dict)
        }
        if resource_types - {"CONTENT_DRAFT"}:
            raise IntentSpecProviderError(
                "INTENT_UNSUPPORTED",
                "L1 CREATE_CONTENT contains resources not covered by the simple projection.",
            )

        constraints = self._project_constraints(intent)
        return IntentSpec(
            mode="SIMPLE",
            goal=intent.goal,
            actions=[
                IntentAction(
                    action=ActionType.CREATE,
                    resource=ResourceType.CONTENT,
                    confidence=intent.confidence,
                ),
            ],
            conditions=[],
            constraints=constraints,
            target_hint=intent.target_task_hint,
            confidence=intent.confidence,
            source="L1",
        )

    @staticmethod
    def _project_constraints(intent: TaskIntent) -> list[IntentConstraint]:
        constraints: list[IntentConstraint] = []
        for raw in intent.constraints:
            if not isinstance(raw, dict):
                raise IntentSpecProviderError(
                    "INTENT_SPEC_INVALID",
                    "L1 constraints must be mappings.",
                )
            raw_type = str(raw.get("type", "")).upper()
            try:
                constraint_type = ConstraintType(raw_type)
            except ValueError as exc:
                raise IntentSpecProviderError(
                    "INTENT_SPEC_INVALID",
                    f"Unsupported L1 constraint type: {raw_type or '<empty>'}.",
                ) from exc
            constraints.append(
                IntentConstraint(
                    type=constraint_type,
                    value=str(raw.get("value", "")),
                )
            )
        return constraints

    def _validate(
        self,
        candidate: Any,
        original_text: str,
        *,
        source: str,
    ) -> IntentSpec:
        """Apply the schema gate and the deterministic semantic gate."""

        try:
            spec = IntentSpec.model_validate(candidate)
        except ValidationError as exc:
            raise IntentSpecProviderError(
                "INTENT_SPEC_INVALID",
                "IntentSpec does not conform to the formal schema.",
            ) from exc

        # TaskUnderstanding's Direct L2 snapshot predates the explicit source
        # field in the LLM output schema.  Keep the actual route provenance
        # visible without changing any intent semantics.
        if source == "L2" and spec.source == "L1":
            spec = spec.model_copy(update={"source": "L2"})

        result = self._validator.validate(spec, original_text)
        self._last_validation_result = result
        if not result.is_valid:
            details = "; ".join(result.errors or result.suggested_fixes)
            raise IntentSpecProviderError(
                "INTENT_VALIDATION_FAILED",
                details or "IntentSpec failed deterministic validation.",
                validation_result=result,
            )
        return spec


__all__ = ["IntentSpecProvider", "IntentSpecProviderError"]
