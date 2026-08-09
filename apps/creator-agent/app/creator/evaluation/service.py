from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.creator.evaluation.dataset import dataset_sha256
from app.creator.evaluation.errors import (
    CreatorEvaluationConflictError,
    CreatorEvaluationDatasetError,
)
from app.creator.evaluation.hashing import canonical_sha256
from app.creator.evaluation.metrics import (
    CreatorEvaluatorRegistry,
    MetricEvaluationContext,
)
from app.creator.evaluation.models import (
    CreatorEvaluationObservation,
    EvaluationCase,
    EvaluationCaseReport,
    EvaluationDataset,
    EvaluationExecutionResult,
    EvaluationMetricName,
    EvaluationMetricResult,
    EvaluationMetricStatus,
    EvaluationMode,
    EvaluationObservationSet,
    EvaluationOutcome,
    EvaluationRunReport,
    utc_now,
)
from app.creator.evaluation.ports import (
    CreatorEvaluationStore,
    CreatorGenerationJudge,
)


logger = logging.getLogger(__name__)


class CreatorEvaluationPipeline:
    def __init__(
        self,
        *,
        registry: CreatorEvaluatorRegistry | None = None,
        store: CreatorEvaluationStore | None = None,
        judge: CreatorGenerationJudge | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._registry = registry or CreatorEvaluatorRegistry.default()
        self._store = store
        self._judge = judge
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"eval-{uuid.uuid4().hex}")

    async def evaluate(
        self,
        dataset: EvaluationDataset,
        observations: EvaluationObservationSet,
        *,
        tenant_id: str,
        actor_id: str,
        candidate_name: str,
        candidate_version: str,
        mode: EvaluationMode = EvaluationMode.OFFLINE_REGRESSION,
        evaluation_run_id: str | None = None,
        baseline: EvaluationRunReport | None = None,
        persist: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationExecutionResult:
        self._validate_inputs(dataset, observations, tenant_id=tenant_id)
        dataset_hash = dataset_sha256(dataset)
        if baseline is not None and (
            baseline.dataset_id != dataset.id
            or baseline.dataset_version != dataset.version
            or baseline.dataset_sha256 != dataset_hash
        ):
            raise CreatorEvaluationDatasetError(
                "Baseline and candidate must use the same frozen dataset",
                details={"baseline_evaluation_run_id": baseline.id},
            )
        request_hash = canonical_sha256(
            {
                "dataset_sha256": dataset_hash,
                "observations": observations.model_dump(mode="json"),
                "evaluator_version": self.evaluator_version,
                "judge": self.judge_identity,
                "candidate_name": candidate_name,
                "candidate_version": candidate_version,
                "baseline_evaluation_run_id": baseline.id if baseline else None,
                "mode": mode.value,
            }
        )
        run_id = evaluation_run_id or self._id_factory()
        if persist and self._store is None:
            raise CreatorEvaluationDatasetError(
                "Evaluation persistence was requested without a configured store"
            )
        if persist and self._store is not None:
            existing = await self._store.get(run_id)
            if existing is not None:
                if existing.request_sha256 != request_hash:
                    raise CreatorEvaluationConflictError(
                        f"Evaluation run {run_id} already exists for another request",
                        details={"evaluation_run_id": run_id},
                    )
                return EvaluationExecutionResult(report=existing, replayed=True)

        started_at = self._clock()
        observations_by_case = {
            observation.case_id: observation
            for observation in observations.observations
        }
        case_reports = []
        for case in dataset.cases:
            case_reports.append(
                await self._evaluate_case(case, observations_by_case[case.id])
            )
        aggregate_metrics = _aggregate_metrics(tuple(case_reports))
        metric_deltas = _metric_deltas(aggregate_metrics, baseline)
        outcome = _aggregate_outcome(tuple(case_reports))
        scored = [
            metric.score
            for metric in aggregate_metrics
            if metric.status == EvaluationMetricStatus.SCORED
            and metric.score is not None
        ]
        limitations = tuple(
            dict.fromkeys(
                limitation
                for case_report in case_reports
                for limitation in case_report.limitations
            )
        )
        completed_at = self._clock()
        report = EvaluationRunReport(
            id=run_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            mode=mode,
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            dataset_sha256=dataset_hash,
            request_sha256=request_hash,
            candidate_name=candidate_name,
            candidate_version=candidate_version,
            evaluator_version=self.evaluator_version,
            baseline_evaluation_run_id=baseline.id if baseline else None,
            metric_deltas=metric_deltas,
            outcome=outcome,
            passed=outcome == EvaluationOutcome.PASSED,
            overall_score=(sum(scored) / len(scored) if scored else None),
            metrics=aggregate_metrics,
            cases=tuple(case_reports),
            limitations=limitations,
            metadata=metadata or {},
            started_at=started_at,
            completed_at=completed_at,
        )
        logger.info(
            "Creator evaluation completed evaluation_run_id=%s dataset=%s "
            "dataset_version=%s cases=%d outcome=%s score=%s",
            report.id,
            report.dataset_id,
            report.dataset_version,
            len(report.cases),
            report.outcome.value,
            report.overall_score,
        )
        if persist:
            assert self._store is not None
            return await self._store.save(report)
        return EvaluationExecutionResult(report=report)

    async def _evaluate_case(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> EvaluationCaseReport:
        limitations = list(observation.limitations)
        assessment = None
        if self._judge is not None and observation.generation is not None:
            try:
                assessment = await self._judge.assess(case, observation)
                limitations.extend(assessment.limitations)
            except Exception as exc:
                logger.warning(
                    "Creator evaluation judge degraded case_id=%s task_id=%s "
                    "error=%s",
                    case.id,
                    observation.task_id,
                    type(exc).__name__,
                )
                limitations.append(
                    f"Generation judge unavailable: {type(exc).__name__}"
                )

        metrics = self._registry.evaluate(
            MetricEvaluationContext(
                case=case,
                observation=observation,
                judge=assessment,
            )
        )
        by_name = {metric.metric: metric for metric in metrics}
        required_results = [
            by_name[metric] for metric in case.criteria.required_metrics
        ]
        failed = any(
            result.status == EvaluationMetricStatus.SCORED and result.passed is False
            for result in required_results
        )
        incomplete = [
            result.metric.value
            for result in required_results
            if result.status != EvaluationMetricStatus.SCORED
        ]
        if incomplete:
            limitations.append(
                "Required metrics were not scored: " + ", ".join(incomplete)
            )
        outcome = (
            EvaluationOutcome.FAILED
            if failed
            else (EvaluationOutcome.PARTIAL if incomplete else EvaluationOutcome.PASSED)
        )
        scored = [
            metric.score
            for metric in metrics
            if metric.status == EvaluationMetricStatus.SCORED
            and metric.score is not None
        ]
        return EvaluationCaseReport(
            case_id=case.id,
            tenant_id=observation.tenant_id,
            creator_id=observation.creator_id,
            task_id=observation.task_id,
            run_id=observation.run_id,
            trace_id=observation.trace_id,
            outcome=outcome,
            passed=outcome == EvaluationOutcome.PASSED,
            overall_score=(sum(scored) / len(scored) if scored else None),
            metrics=metrics,
            required_metrics=case.criteria.required_metrics,
            observation_sha256=canonical_sha256(observation),
            limitations=tuple(dict.fromkeys(limitations)),
        )

    @property
    def evaluator_version(self) -> str:
        return self._registry.version

    @property
    def judge_identity(self) -> dict[str, str] | None:
        if self._judge is None:
            return None
        return {"name": self._judge.name, "version": self._judge.version}

    async def aclose(self) -> None:
        close = getattr(self._judge, "aclose", None)
        if close is not None:
            await close()

    @staticmethod
    def _validate_inputs(
        dataset: EvaluationDataset,
        observations: EvaluationObservationSet,
        *,
        tenant_id: str,
    ) -> None:
        if observations.dataset_id != dataset.id:
            raise CreatorEvaluationDatasetError(
                "Observation dataset ID does not match the evaluation dataset",
                details={
                    "dataset_id": dataset.id,
                    "observation_dataset_id": observations.dataset_id,
                },
            )
        if observations.dataset_version != dataset.version:
            raise CreatorEvaluationDatasetError(
                "Observation dataset version does not match the evaluation dataset",
                details={
                    "dataset_version": dataset.version,
                    "observation_dataset_version": observations.dataset_version,
                },
            )
        expected = {case.id for case in dataset.cases}
        actual = {observation.case_id for observation in observations.observations}
        if expected != actual:
            raise CreatorEvaluationDatasetError(
                "Every evaluation case requires exactly one observation",
                details={
                    "missing_case_ids": sorted(expected - actual),
                    "unexpected_case_ids": sorted(actual - expected),
                },
            )
        cross_tenant = sorted(
            {
                observation.tenant_id
                for observation in observations.observations
                if observation.tenant_id != tenant_id
            }
        )
        if cross_tenant:
            raise CreatorEvaluationDatasetError(
                "Evaluation observations cannot cross tenant boundaries",
                details={"unexpected_tenant_ids": cross_tenant},
            )


def _aggregate_outcome(
    cases: tuple[EvaluationCaseReport, ...],
) -> EvaluationOutcome:
    if any(case.outcome == EvaluationOutcome.FAILED for case in cases):
        return EvaluationOutcome.FAILED
    if any(case.outcome == EvaluationOutcome.PARTIAL for case in cases):
        return EvaluationOutcome.PARTIAL
    return EvaluationOutcome.PASSED


def _aggregate_metrics(
    cases: tuple[EvaluationCaseReport, ...],
) -> tuple[EvaluationMetricResult, ...]:
    aggregated = []
    metric_names = tuple(
        metric_name
        for metric_name in EvaluationMetricName
        if any(
            any(metric.metric == metric_name for metric in case.metrics)
            for case in cases
        )
    )
    for metric_name in metric_names:
        results = [
            metric
            for case in cases
            for metric in case.metrics
            if metric.metric == metric_name
        ]
        scored = [
            result
            for result in results
            if result.status == EvaluationMetricStatus.SCORED
            and result.score is not None
        ]
        threshold = sum(result.threshold for result in results) / len(results)
        details = {
            "case_count": len(results),
            "scored_case_count": len(scored),
            "skipped_case_count": sum(
                result.status == EvaluationMetricStatus.SKIPPED for result in results
            ),
            "error_case_count": sum(
                result.status == EvaluationMetricStatus.ERROR for result in results
            ),
            "passed_case_count": sum(result.passed is True for result in scored),
        }
        if scored:
            score = sum(
                result.score for result in scored if result.score is not None
            ) / len(scored)
            aggregated.append(
                EvaluationMetricResult(
                    metric=metric_name,
                    status=EvaluationMetricStatus.SCORED,
                    score=score,
                    threshold=threshold,
                    passed=all(result.passed is True for result in scored),
                    evaluator="creator-macro-aggregate",
                    evaluator_version="1.0.0",
                    reason="Macro average across scored evaluation cases.",
                    details=details,
                    sample_size=len(scored),
                )
            )
            continue
        status = (
            EvaluationMetricStatus.ERROR
            if any(result.status == EvaluationMetricStatus.ERROR for result in results)
            else EvaluationMetricStatus.SKIPPED
        )
        aggregated.append(
            EvaluationMetricResult(
                metric=metric_name,
                status=status,
                threshold=threshold,
                evaluator="creator-macro-aggregate",
                evaluator_version="1.0.0",
                reason="No evaluation case produced a score for this metric.",
                details=details,
                sample_size=0,
            )
        )
    return tuple(aggregated)


def _metric_deltas(
    metrics: tuple[EvaluationMetricResult, ...],
    baseline: EvaluationRunReport | None,
) -> dict[str, float]:
    if baseline is None:
        return {}
    baseline_scores = {
        metric.metric: metric.score
        for metric in baseline.metrics
        if metric.status == EvaluationMetricStatus.SCORED and metric.score is not None
    }
    return {
        metric.metric.value: metric.score - baseline_scores[metric.metric]
        for metric in metrics
        if metric.status == EvaluationMetricStatus.SCORED
        and metric.score is not None
        and metric.metric in baseline_scores
        and baseline_scores[metric.metric] is not None
    }
