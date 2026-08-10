"""Formal IntentSpec boundary for Runtime message migrations.

The legacy ``TaskUnderstanding`` API intentionally continues to return a
``TaskIntent``.  Runtime callers use this provider instead so that every
successful result is a schema-validated and semantically validated
``IntentSpec``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ..execution.temporal_resolver import TemporalResolver
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
            candidate = self._project_l1_create_content(
                intent,
                user_message=user_message,
            )
        else:
            raise IntentSpecProviderError(
                "INTENT_SPEC_UNAVAILABLE",
                "Formal IntentSpec is unavailable for the L2 understanding result.",
            )

        return self._validate(candidate, user_message, source=intent.source)

    async def resolve_graph(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Ask the semantic model for independent goals and dependencies.

        This is intentionally a separate contract from single-turn
        ``IntentSpec``.  The model must decide whether clauses are one
        composite goal or separate business goals, and must return dependency
        edges by goal index.  No punctuation/conjunction heuristic is used.
        """
        llm = getattr(self._understanding, "_llm", None)
        if llm is None:
            return []
        model = getattr(self._understanding, "_model", "")
        task_context = json.dumps(existing_tasks or [], ensure_ascii=False)
        system = """You are a conversation goal analyzer for an agent runtime.
Decompose the user's request into independent business goals only when they
have separate lifecycle, target, or side-effect ownership. Keep sequential
actions for one deliverable in the same goal and express their internal work
as IntentSpec actions. A read-only search/analysis goal must be marked QUERY
semantically and must not be turned into a write execution.

Return JSON only:
{"goals":[{"text":"source span or concise request",
"intent":{"mode":"SIMPLE|COMPOSITE|CONDITIONAL","goal":"...",
"actions":[{"action":"CREATE|UPDATE|DELETE|QUERY|SEARCH|ANALYZE|PUBLISH|UPDATE_OR_CREATE",
"resource":"CONTENT|DRAFT|SCHEDULE|POST|TASK|null"}],
"conditions":[],"constraints":[],"target_hint":null,"confidence":0.9},
"depends_on":[],"artifact_inputs":[],"artifact_outputs":[]}]}.
Use zero-based goal indexes in depends_on. Do not invent dependencies.
"""
        response = await llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({
                    "existing_tasks": json.loads(task_context),
                    "user_message": user_message,
                }, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1200,
        )
        raw = response.choices[0].message.content or "{}"
        try:
            payload = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            return []
        goals = payload.get("goals") if isinstance(payload, dict) else None
        if not isinstance(goals, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in goals:
            if not isinstance(item, dict):
                continue
            try:
                spec = self._validate(
                    item.get("intent") or item,
                    str(item.get("text") or user_message),
                    source="L2",
                )
            except IntentSpecProviderError:
                continue
            normalized.append({
                "text": str(item.get("text") or spec.goal or user_message),
                "intent": spec.model_dump(mode="json"),
                "depends_on": list(item.get("depends_on") or []),
                "artifact_inputs": list(item.get("artifact_inputs") or []),
                "artifact_outputs": list(item.get("artifact_outputs") or []),
            })
        return normalized

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

    def _project_l1_create_content(
        self,
        intent: TaskIntent,
        *,
        user_message: str = "",
    ) -> IntentSpec:
        """Project the explicitly supported lossless L1 CREATE contracts.

        L1 emits a small legacy-shaped ``TaskIntent`` rather than actions.
        Keep this boundary fail-closed: only a pure content creation request
        and the exact CREATE+PUBLISH compound shape are projected.  Every
        other combination remains unsupported instead of silently dropping an
        action or resource request.
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
        resource_requests = {
            (
                str(item.get("operation", "")).upper(),
                str(item.get("resource_type", "")).upper(),
            )
            for item in intent.resource_requests
            if isinstance(item, dict)
        }

        simple_shape = (
            requirement_types == {"CREATE"}
            and resource_requests == {("CREATE", "CONTENT_DRAFT")}
        )
        compound_shape = (
            requirement_types == {"CREATE", "PUBLISH"}
            and resource_requests == {
                ("CREATE", "CONTENT_DRAFT"),
                ("CREATE", "SCHEDULE"),
            }
        )
        if not simple_shape and not compound_shape:
            raise IntentSpecProviderError(
                "INTENT_UNSUPPORTED",
                "L1 CREATE_CONTENT contains actions or resources not covered by the formal projection.",
            )

        constraints = self._project_constraints(intent)
        actions = [
            IntentAction(
                action=ActionType.CREATE,
                resource=ResourceType.CONTENT,
                confidence=intent.confidence,
            ),
        ]
        mode = "SIMPLE"

        if compound_shape:
            actions.append(
                IntentAction(
                    action=ActionType.PUBLISH,
                    resource=ResourceType.CONTENT,
                    confidence=intent.confidence,
                )
            )
            mode = "COMPOSITE"

            # TaskUnderstanding's L1 projection records the schedule signal
            # in requirements/resource_requests but leaves constraints empty.
            # Preserve a resolvable time expression as TIME instead of
            # dropping it before IntentValidator/ArgumentBinder see it.
            has_time = any(
                constraint.type == ConstraintType.TIME
                for constraint in constraints
            )
            time_text = user_message.strip() or intent.goal.strip()
            if not has_time and time_text:
                if TemporalResolver().resolve(time_text) is not None:
                    constraints.append(
                        IntentConstraint(
                            type=ConstraintType.TIME,
                            value=time_text,
                        )
                    )

        return IntentSpec(
            mode=mode,
            goal=intent.goal,
            actions=actions,
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
