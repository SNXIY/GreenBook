from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.creator.domain.models import CreatorTaskStatus
from app.creator.evaluation.models import (
    ClaimVerdict,
    CreatorEvaluationObservation,
    EvaluationCase,
    EvaluationMetricName,
    EvaluationMetricResult,
    EvaluationMetricStatus,
    GenerationJudgeAssessment,
    ToolExpectation,
)
from app.creator.runtime.models import PlanStepStatus
from app.creator.tools.models import CreatorToolCallStatus


@dataclass(frozen=True)
class MetricEvaluationContext:
    case: EvaluationCase
    observation: CreatorEvaluationObservation
    judge: GenerationJudgeAssessment | None = None


class CreatorEvaluatorRegistry:
    def __init__(
        self,
        evaluator: CreatorDeterministicMetricEvaluator,
    ) -> None:
        self._evaluator = evaluator
        self.version = f"{evaluator.name}/{evaluator.version}"

    @classmethod
    def default(cls) -> CreatorEvaluatorRegistry:
        return cls(CreatorDeterministicMetricEvaluator())

    def evaluate(
        self,
        context: MetricEvaluationContext,
    ) -> tuple[EvaluationMetricResult, ...]:
        return self._evaluator.evaluate(
            context.case,
            context.observation,
            generation_assessment=context.judge,
        )


class CreatorDeterministicMetricEvaluator:
    """Computes trace and label based metrics without model calls."""

    name = "mindflow.creator.deterministic"
    version = "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
        *,
        generation_assessment: GenerationJudgeAssessment | None,
    ) -> tuple[EvaluationMetricResult, ...]:
        handlers: dict[
            EvaluationMetricName,
            Callable[[], EvaluationMetricResult],
        ] = {
            EvaluationMetricName.RETRIEVAL_RECALL_AT_K: lambda: self._retrieval(
                case, observation
            )[0],
            EvaluationMetricName.RETRIEVAL_PRECISION_AT_K: lambda: self._retrieval(
                case, observation
            )[1],
            EvaluationMetricName.RETRIEVAL_MRR: lambda: self._retrieval(
                case, observation
            )[2],
            EvaluationMetricName.RETRIEVAL_NDCG_AT_K: lambda: self._retrieval(
                case, observation
            )[3],
            EvaluationMetricName.RETRIEVAL_ACL_SAFETY: lambda: self._retrieval(
                case, observation
            )[4],
            EvaluationMetricName.AGENT_TASK_SUCCESS_RATE: lambda: self._task_success(
                case, observation
            ),
            EvaluationMetricName.AGENT_TOOL_CALLING_ACCURACY: (
                lambda: self._tool_accuracy(case, observation)
            ),
            EvaluationMetricName.AGENT_PLANNING_QUALITY: (
                lambda: self._planning_quality(case, observation)
            ),
            EvaluationMetricName.GENERATION_FAITHFULNESS: lambda: self._judge_metric(
                case,
                observation,
                generation_assessment,
                EvaluationMetricName.GENERATION_FAITHFULNESS,
            ),
            EvaluationMetricName.GENERATION_RELEVANCE: lambda: self._judge_metric(
                case,
                observation,
                generation_assessment,
                EvaluationMetricName.GENERATION_RELEVANCE,
            ),
            EvaluationMetricName.GENERATION_STYLE_CONSISTENCY: (
                lambda: self._judge_metric(
                    case,
                    observation,
                    generation_assessment,
                    EvaluationMetricName.GENERATION_STYLE_CONSISTENCY,
                )
            ),
        }
        results: list[EvaluationMetricResult] = []
        retrieval_cache: tuple[EvaluationMetricResult, ...] | None = None
        for metric in case.criteria.required_metrics:
            try:
                if metric.value.startswith("retrieval_"):
                    if retrieval_cache is None:
                        retrieval_cache = self._retrieval(case, observation)
                    result_by_name = {item.metric: item for item in retrieval_cache}
                    results.append(result_by_name[metric])
                else:
                    results.append(handlers[metric]())
            except Exception as exc:
                results.append(
                    self._unscored(
                        case,
                        metric,
                        EvaluationMetricStatus.ERROR,
                        f"Metric computation failed: {type(exc).__name__}",
                    )
                )
        return tuple(results)

    def _retrieval(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> tuple[EvaluationMetricResult, ...]:
        metrics = (
            EvaluationMetricName.RETRIEVAL_RECALL_AT_K,
            EvaluationMetricName.RETRIEVAL_PRECISION_AT_K,
            EvaluationMetricName.RETRIEVAL_MRR,
            EvaluationMetricName.RETRIEVAL_NDCG_AT_K,
            EvaluationMetricName.RETRIEVAL_ACL_SAFETY,
        )
        relevant = set(case.criteria.relevant_document_ids)
        k = case.criteria.thresholds.retrieval_k
        ranked = sorted(observation.evidence, key=lambda item: item.rank)[:k]
        leaked = [item.document_id for item in ranked if not item.authority_verified]
        acl_safety = 1.0 if not ranked else 1.0 - (len(leaked) / len(ranked))
        acl_result = self._scored(
            case,
            EvaluationMetricName.RETRIEVAL_ACL_SAFETY,
            acl_safety,
            (
                "Every retrieved item passed authority verification."
                if not leaked
                else f"{len(leaked)} retrieved items failed authority verification."
            ),
            {
                "k": k,
                "retrieved_count": len(ranked),
                "unverified_document_ids": leaked,
            },
        )
        if not relevant:
            label_metrics = tuple(
                self._unscored(
                    case,
                    metric,
                    EvaluationMetricStatus.SKIPPED,
                    "The case has no labeled relevant document IDs.",
                )
                for metric in metrics[:-1]
            )
            return (*label_metrics, acl_result)
        returned_ids = [item.document_id for item in ranked]
        seen_relevant: set[str] = set()
        relevance_flags = []
        relevant_hits = []
        for document_id in returned_ids:
            is_new_hit = document_id in relevant and document_id not in seen_relevant
            relevance_flags.append(is_new_hit)
            if is_new_hit:
                seen_relevant.add(document_id)
                relevant_hits.append(document_id)
        unique_hits = set(relevant_hits)
        recall = len(unique_hits) / len(relevant)
        precision = len(relevant_hits) / k
        first_relevant_rank = next(
            (
                rank
                for rank, is_relevant in enumerate(relevance_flags, start=1)
                if is_relevant
            ),
            None,
        )
        mrr = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, is_relevant in enumerate(relevance_flags, start=1)
            if is_relevant
        )
        ideal_count = min(len(relevant), k)
        ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
        shared_details = {
            "k": k,
            "returned_document_ids": returned_ids,
            "relevant_document_ids": sorted(relevant),
            "relevant_hits": relevant_hits,
        }
        return (
            self._scored(
                case,
                EvaluationMetricName.RETRIEVAL_RECALL_AT_K,
                recall,
                (
                    f"Retrieved {len(unique_hits)} of {len(relevant)} labeled "
                    f"documents within top {k}."
                ),
                shared_details,
            ),
            self._scored(
                case,
                EvaluationMetricName.RETRIEVAL_PRECISION_AT_K,
                precision,
                f"{len(relevant_hits)} of {k} top-k slots were relevant.",
                shared_details,
            ),
            self._scored(
                case,
                EvaluationMetricName.RETRIEVAL_MRR,
                mrr,
                (
                    "No relevant document was returned."
                    if first_relevant_rank is None
                    else f"The first relevant document was ranked {first_relevant_rank}."
                ),
                {**shared_details, "first_relevant_rank": first_relevant_rank},
            ),
            self._scored(
                case,
                EvaluationMetricName.RETRIEVAL_NDCG_AT_K,
                ndcg,
                "Binary relevance ordering was compared with the ideal top-k order.",
                shared_details,
            ),
            acl_result,
        )

    def _task_success(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> EvaluationMetricResult:
        expected_kind = case.criteria.expected_final_artifact_kind
        final_kind_matches = (
            expected_kind is None or observation.final_artifact_kind == expected_kind
        )
        required_capabilities = set(case.criteria.expected_capabilities)
        successful_capabilities = {
            execution.capability
            for execution in observation.executions
            if execution.status == PlanStepStatus.SUCCEEDED
        }
        missing_capabilities = sorted(
            item.value for item in required_capabilities - successful_capabilities
        )
        passed = (
            observation.task_status == CreatorTaskStatus.COMPLETED
            and final_kind_matches
            and not missing_capabilities
        )
        return self._scored(
            case,
            EvaluationMetricName.AGENT_TASK_SUCCESS_RATE,
            1.0 if passed else 0.0,
            (
                "The run completed with the required artifact and capabilities."
                if passed
                else "The run did not satisfy the labeled completion contract."
            ),
            {
                "task_status": observation.task_status.value,
                "expected_final_artifact_kind": (
                    expected_kind.value if expected_kind else None
                ),
                "actual_final_artifact_kind": (
                    observation.final_artifact_kind.value
                    if observation.final_artifact_kind
                    else None
                ),
                "missing_capabilities": missing_capabilities,
                "runtime_error_codes": list(observation.runtime_error_codes),
            },
        )

    def _tool_accuracy(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> EvaluationMetricResult:
        expected = case.criteria.expected_tools
        actual = observation.tool_calls
        if not expected and not actual:
            return self._scored(
                case,
                EvaluationMetricName.AGENT_TOOL_CALLING_ACCURACY,
                1.0,
                "The labeled trajectory requires no tools and made no tool calls.",
                {"expected_call_count": 0, "actual_call_count": 0},
            )
        successful = [
            call for call in actual if call.status == CreatorToolCallStatus.SUCCESS
        ]
        available = set(range(len(successful)))
        matched_call_ids: set[str] = set()
        matched_required = 0
        expected_required = sum(item.min_calls for item in expected)
        expectation_details: list[dict[str, Any]] = []
        ordered_expectations = sorted(
            expected,
            key=lambda item: item.arguments_sha256 is None,
        )
        for item in ordered_expectations:
            matches = [
                index
                for index in sorted(available)
                if _tool_matches(
                    successful[index].name,
                    successful[index].arguments_sha256,
                    item,
                )
            ]
            maximum = item.max_calls or item.min_calls
            accepted = matches[:maximum]
            required_matches = min(len(accepted), item.min_calls)
            matched_required += required_matches
            for index in accepted:
                available.discard(index)
                matched_call_ids.add(successful[index].call_id)
            expectation_details.append(
                {
                    "tool": item.name,
                    "matched_calls": len(accepted),
                    "matched_required_calls": required_matches,
                    "min_calls": item.min_calls,
                    "max_calls": maximum,
                    "arguments_hash_required": item.arguments_sha256 is not None,
                }
            )
        recall = matched_required / expected_required if expected_required else 1.0
        if case.criteria.allow_additional_tools:
            precision = len(successful) / len(actual) if actual else 1.0
        else:
            precision = len(matched_call_ids) / len(actual) if actual else 0.0
        score = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        return self._scored(
            case,
            EvaluationMetricName.AGENT_TOOL_CALLING_ACCURACY,
            score,
            "Tool calling accuracy is multiset F1 over successful, argument-matched calls.",
            {
                "precision": precision,
                "recall": recall,
                "expected_required_calls": expected_required,
                "matched_required_calls": matched_required,
                "expected": expectation_details,
                "actual_call_count": len(actual),
                "matched_call_ids": sorted(matched_call_ids),
                "allow_additional_tools": case.criteria.allow_additional_tools,
            },
        )

    def _planning_quality(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> EvaluationMetricResult:
        steps = [step for plan in observation.plans for step in plan.steps]
        step_by_id = {step.step_id: step for step in steps}
        plan_valid, plan_issues = _validate_plan_dependencies(observation)
        expected_capabilities = set(case.criteria.expected_capabilities)
        planned_capabilities = {step.capability for step in steps}
        capability_coverage = (
            len(expected_capabilities & planned_capabilities)
            / len(expected_capabilities)
            if expected_capabilities
            else 1.0
        )
        aligned = {
            execution.step_id
            for execution in observation.executions
            if execution.status == PlanStepStatus.SUCCEEDED
            and execution.step_id in step_by_id
            and step_by_id[execution.step_id].capability == execution.capability
        }
        execution_alignment = (
            len(aligned) / len(step_by_id)
            if step_by_id
            else (1.0 if not observation.executions else 0.0)
        )
        step_efficiency = min(
            1.0,
            case.criteria.max_plan_steps / max(1, len(steps)),
        )
        replan_efficiency = (
            1.0
            if observation.replan_count <= case.criteria.max_replans
            else case.criteria.max_replans / max(1, observation.replan_count)
        )
        score = (
            0.35 * capability_coverage
            + 0.30 * (1.0 if plan_valid else 0.0)
            + 0.25 * execution_alignment
            + 0.05 * step_efficiency
            + 0.05 * replan_efficiency
        )
        return self._scored(
            case,
            EvaluationMetricName.AGENT_PLANNING_QUALITY,
            score,
            (
                "Planning quality combines labeled capability coverage, dependency "
                "validity, execution alignment, and bounded plan growth."
            ),
            {
                "capability_coverage": capability_coverage,
                "plan_valid": plan_valid,
                "plan_issues": plan_issues,
                "execution_alignment": execution_alignment,
                "step_efficiency": step_efficiency,
                "replan_efficiency": replan_efficiency,
                "plan_step_count": len(steps),
                "replan_count": observation.replan_count,
            },
        )

    def _judge_metric(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
        assessment: GenerationJudgeAssessment | None,
        metric: EvaluationMetricName,
    ) -> EvaluationMetricResult:
        if assessment is None:
            return self._unscored(
                case,
                metric,
                EvaluationMetricStatus.SKIPPED,
                "No generation output or generation evaluator was available.",
            )
        score_by_metric = {
            EvaluationMetricName.GENERATION_FAITHFULNESS: assessment.faithfulness,
            EvaluationMetricName.GENERATION_RELEVANCE: assessment.relevance,
            EvaluationMetricName.GENERATION_STYLE_CONSISTENCY: (
                assessment.style_consistency
            ),
        }
        judged = score_by_metric[metric]
        if judged is None:
            return self._unscored(
                case,
                metric,
                EvaluationMetricStatus.SKIPPED,
                "The generation evaluator lacked the required labeled criteria.",
                evaluator=assessment.judge_name,
                evaluator_version=assessment.judge_version,
            )
        details: dict[str, Any] = {"limitations": list(assessment.limitations)}
        if metric == EvaluationMetricName.GENERATION_FAITHFULNESS:
            authorized_ids = {
                evidence.evidence_id
                for evidence in observation.evidence
                if evidence.authority_verified
            }
            assessable = [
                claim
                for claim in assessment.claims
                if claim.verdict != ClaimVerdict.NOT_ASSESSABLE
            ]
            supported = [
                claim
                for claim in assessable
                if claim.verdict == ClaimVerdict.SUPPORTED
                and set(claim.supporting_evidence_ids) <= authorized_ids
            ]
            if not assessable:
                return self._unscored(
                    case,
                    metric,
                    EvaluationMetricStatus.SKIPPED,
                    "No claim-level verdict was assessable.",
                    evaluator=assessment.judge_name,
                    evaluator_version=assessment.judge_version,
                )
            judged = judged.model_copy(
                update={
                    "score": len(supported) / len(assessable),
                    "reason": (
                        f"{len(supported)} of {len(assessable)} assessable claims "
                        "were supported by authority-verified evidence."
                    ),
                }
            )
            details["claims"] = [
                claim.model_dump(mode="json") for claim in assessment.claims
            ]
        return self._scored(
            case,
            metric,
            judged.score,
            judged.reason,
            details,
            evaluator=assessment.judge_name,
            evaluator_version=assessment.judge_version,
        )

    def _scored(
        self,
        case: EvaluationCase,
        metric: EvaluationMetricName,
        score: float,
        reason: str,
        details: dict[str, Any],
        *,
        evaluator: str | None = None,
        evaluator_version: str | None = None,
    ) -> EvaluationMetricResult:
        bounded = min(1.0, max(0.0, float(score)))
        threshold = case.criteria.thresholds.for_metric(metric)
        return EvaluationMetricResult(
            metric=metric,
            status=EvaluationMetricStatus.SCORED,
            score=bounded,
            threshold=threshold,
            passed=bounded >= threshold,
            evaluator=evaluator or self.name,
            evaluator_version=evaluator_version or self.version,
            reason=reason,
            details=details,
        )

    def _unscored(
        self,
        case: EvaluationCase,
        metric: EvaluationMetricName,
        status: EvaluationMetricStatus,
        reason: str,
        *,
        evaluator: str | None = None,
        evaluator_version: str | None = None,
    ) -> EvaluationMetricResult:
        return EvaluationMetricResult(
            metric=metric,
            status=status,
            threshold=case.criteria.thresholds.for_metric(metric),
            evaluator=evaluator or self.name,
            evaluator_version=evaluator_version or self.version,
            reason=reason,
            sample_size=0,
        )


def _tool_matches(
    name: str,
    arguments_sha256: str | None,
    expected: ToolExpectation,
) -> bool:
    if name != expected.name:
        return False
    return (
        expected.arguments_sha256 is None
        or arguments_sha256 == expected.arguments_sha256
    )


def _validate_plan_dependencies(
    observation: CreatorEvaluationObservation,
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not observation.plans:
        issues.append("missing_plan")
    for plan in observation.plans:
        if not plan.steps:
            issues.append(f"revision_{plan.revision}:empty_plan")
        graph = {step.step_id: step.dependencies for step in plan.steps}
        known = set(graph)
        for step_id, dependencies in graph.items():
            if step_id in dependencies:
                issues.append(f"revision_{plan.revision}:self_dependency:{step_id}")
            if set(dependencies) - known:
                issues.append(f"revision_{plan.revision}:unknown_dependency:{step_id}")
        if _has_cycle(graph):
            issues.append(f"revision_{plan.revision}:dependency_cycle")
    return not issues, issues


def _has_cycle(graph: dict[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(dependency in graph and visit(dependency) for dependency in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)
