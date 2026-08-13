from __future__ import annotations

from typing import Any

from app.creator.domain.models import CreatorTaskStatus
from app.creator.evaluation.deterministic_judge import (
    DeterministicGenerationJudge,
)
from app.creator.evaluation.metrics import (
    CreatorEvaluatorRegistry,
    MetricEvaluationContext,
)
from app.creator.evaluation.models import (
    CreatorEvaluationObservation,
    EvaluationCase,
    EvaluationCriteria,
    EvaluationMetricName,
    EvaluationMetricStatus,
    GenerationObservation,
    ObservedEvidence,
    RuntimeEvaluationSummary,
    StyleCriteria,
)
from app.creator.runtime.models import (
    AgentExecutionContext,
    ArtifactKind,
)

_RUNTIME_DATASET_ID = "creator-runtime-context"
_RUNTIME_DATASET_VERSION = "1.0.0"


class CreatorRuntimeContextEvaluator:
    """Reference-free evaluator for the in-graph EvaluationAgent."""

    def __init__(
        self,
        *,
        registry: CreatorEvaluatorRegistry | None = None,
        judge: DeterministicGenerationJudge | None = None,
    ) -> None:
        self._registry = registry or CreatorEvaluatorRegistry.default()
        self._judge = judge or DeterministicGenerationJudge()

    async def evaluate(
        self,
        context: AgentExecutionContext,
    ) -> RuntimeEvaluationSummary:
        draft_artifact = _latest_draft(context)
        critique_artifact = _latest(context, ArtifactKind.CRITIQUE)
        evidence_artifact = _latest(context, ArtifactKind.EVIDENCE_PACK)
        outline_artifact = _latest(context, ArtifactKind.CONTENT_OUTLINE)
        profile_artifact = _latest(context, ArtifactKind.CREATOR_PROFILE)

        draft = draft_artifact.content
        critique = critique_artifact.content
        evidence = _evidence(evidence_artifact.content) if evidence_artifact else ()
        required_concepts = _required_concepts(
            context.goal.constraints,
            outline_artifact.content if outline_artifact else None,
        )
        style = _style_criteria(
            context.goal.constraints,
            outline_artifact.content if outline_artifact else None,
            profile_artifact.content if profile_artifact else None,
        )
        required_metrics = [
            EvaluationMetricName.AGENT_TASK_SUCCESS_RATE,
        ]
        if evidence:
            required_metrics.append(EvaluationMetricName.GENERATION_FAITHFULNESS)
        if required_concepts:
            required_metrics.append(EvaluationMetricName.GENERATION_RELEVANCE)
        if _has_deterministic_style_rules(style):
            required_metrics.append(EvaluationMetricName.GENERATION_STYLE_CONSISTENCY)
        case = EvaluationCase(
            id=f"runtime:{context.identity.run_id}",
            task_kind=context.identity.task_kind,
            goal=context.goal.text,
            criteria=EvaluationCriteria(
                required_concepts=required_concepts,
                expected_final_artifact_kind=ArtifactKind.DRAFT,
                style=style,
                required_metrics=tuple(required_metrics),
            ),
            split="online-reference-free",
            tags=("runtime", "reference-free"),
        )
        accepted = (
            str(critique.get("verdict") or "").upper() == "ACCEPT"
            and critique.get("reviewed_artifact_id") == draft_artifact.id
        )
        observation = CreatorEvaluationObservation(
            case_id=case.id,
            tenant_id=context.identity.tenant_id,
            creator_id=context.identity.creator_id,
            task_id=context.identity.task_id,
            run_id=context.identity.run_id,
            trace_id=context.identity.trace_id,
            task_status=(
                CreatorTaskStatus.COMPLETED if accepted else CreatorTaskStatus.FAILED
            ),
            goal=context.goal.text,
            final_artifact_kind=ArtifactKind.DRAFT,
            evidence=evidence,
            generation=GenerationObservation(
                title=str(draft.get("title") or ""),
                body_markdown=str(draft.get("body_markdown") or ""),
                cited_evidence_ids=tuple(
                    str(value) for value in draft.get("evidence_ids", ())
                ),
                declared_unsupported_claims=tuple(
                    str(value) for value in draft.get("unsupported_claims", ())
                ),
            ),
            runtime_error_codes=(() if accepted else ("CRITIC_NOT_ACCEPTED",)),
            limitations=(
                "Runtime context does not contain a frozen gold retrieval label, "
                "complete tool audit, or full plan trajectory.",
            ),
        )
        assessment = await self._judge.assess(case, observation)
        metrics = self._registry.evaluate(
            MetricEvaluationContext(
                case=case,
                observation=observation,
                judge=assessment,
            )
        )
        scored = [
            metric.score
            for metric in metrics
            if metric.status == EvaluationMetricStatus.SCORED
            and metric.score is not None
        ]
        task_metric = next(
            metric
            for metric in metrics
            if metric.metric == EvaluationMetricName.AGENT_TASK_SUCCESS_RATE
        )
        evaluated = {metric.metric for metric in metrics}
        unevaluated = tuple(
            metric for metric in EvaluationMetricName if metric not in evaluated
        )
        partial = bool(unevaluated) or any(
            metric.status != EvaluationMetricStatus.SCORED for metric in metrics
        )
        return RuntimeEvaluationSummary(
            task_success=task_metric.passed is True,
            quality_score=sum(scored) / len(scored) if scored else 0.0,
            metric_status="PARTIAL" if partial else "COMPUTED",
            dataset_id=_RUNTIME_DATASET_ID,
            dataset_version=_RUNTIME_DATASET_VERSION,
            evaluator_version=(
                f"{self._registry.version};" f"{self._judge.name}/{self._judge.version}"
            ),
            metrics=metrics,
            unevaluated_metrics=unevaluated,
            planning_observations=(
                "Full planning quality is evaluated from persisted plan and execution "
                "events in offline or sampled evaluation.",
            ),
            generation_observations=tuple(
                metric.reason
                for metric in metrics
                if metric.metric.value.startswith("generation_")
            ),
        )


def _latest(
    context: AgentExecutionContext,
    kind: ArtifactKind,
):
    matches = [artifact for artifact in context.artifacts if artifact.kind == kind]
    if not matches:
        if kind in {
            ArtifactKind.DRAFT,
            ArtifactKind.CRITIQUE,
        }:
            raise ValueError(f"Runtime evaluation requires {kind.value}")
        return None
    return max(
        matches,
        key=lambda artifact: (artifact.revision, artifact.created_at, artifact.id),
    )


def _latest_draft(context: AgentExecutionContext):
    matches = [
        artifact
        for artifact in context.artifacts
        if artifact.kind in {ArtifactKind.DRAFT, ArtifactKind.SOURCE_DRAFT}
    ]
    if not matches:
        raise ValueError("Runtime evaluation requires a draft artifact")
    return max(
        matches,
        key=lambda artifact: (artifact.revision, artifact.created_at, artifact.id),
    )


def _evidence(content: dict[str, Any]) -> tuple[ObservedEvidence, ...]:
    observed: list[ObservedEvidence] = []
    for rank, item in enumerate(content.get("evidence", ()), start=1):
        observed.append(
            ObservedEvidence(
                evidence_id=str(item["id"]),
                document_id=str(item.get("document_id") or item["id"]),
                rank=rank,
                text=str(item.get("summary") or ""),
                source=str(item.get("source") or "") or None,
                authority_verified=bool(item.get("authority_verified", False)),
            )
        )
    return tuple(observed)


def _required_concepts(
    constraints: dict[str, Any],
    outline: dict[str, Any] | None,
) -> tuple[str, ...]:
    configured = constraints.get("required_concepts", ())
    concepts = (
        [
            str(value).strip()
            for value in configured
            if isinstance(value, str) and value.strip()
        ]
        if isinstance(configured, (list, tuple))
        else []
    )
    if outline:
        thesis = str(outline.get("thesis") or "").strip()
        if thesis:
            concepts.append(thesis)
        for section in outline.get("sections", ()):
            for point in section.get("key_points", ()):
                if str(point).strip():
                    concepts.append(str(point).strip())
    return tuple(dict.fromkeys(concepts[:50]))


def _style_criteria(
    constraints: dict[str, Any],
    outline: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> StyleCriteria:
    headings = []
    if outline:
        headings = [
            str(section.get("heading") or "").strip()
            for section in outline.get("sections", ())
            if str(section.get("heading") or "").strip()
        ]
    instructions = (
        tuple(str(value) for value in profile.get("style_traits", ()))
        if profile
        else ()
    )
    return StyleCriteria(
        instructions=instructions,
        required_terms=_string_tuple(constraints.get("style_required_terms")),
        forbidden_terms=_string_tuple(constraints.get("style_forbidden_terms")),
        required_headings=tuple(headings[:20]),
        min_chars=_optional_positive_int(constraints.get("min_chars")),
        max_chars=_optional_positive_int(constraints.get("max_chars")),
    )


def _has_deterministic_style_rules(style: StyleCriteria) -> bool:
    return bool(
        style.required_terms
        or style.forbidden_terms
        or style.required_headings
        or style.exemplar_texts
        or style.min_chars is not None
        or style.max_chars is not None
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        str(item).strip() for item in value if isinstance(item, str) and item.strip()
    )


def _optional_positive_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and value > 0 else None
