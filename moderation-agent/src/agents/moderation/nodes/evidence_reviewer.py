import hashlib
import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from agents.moderation.reviewer import reviewer_revision_signature, validate_reviewer_route
from agents.moderation.reviewer_observability import record_reviewer_trace_metadata
from agents.moderation.state import ModerationState
from core import settings
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    EvidenceReviewerDecision,
    EvidenceReviewerMetrics,
    JudgeAgentResult,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyEvidence,
    PolicyGradeNextAction,
    PolicyGradeResult,
    ReviewerNextAction,
    ReviewerProblem,
    ReviewerProblemType,
    RiskAgentResult,
    RiskClassification,
    SafeAgentResult,
)
from moderation.security import redact_data

from .dependencies import EvidenceReviewInput, ModerationDependencies
from .evidence_reviewer_model import EvidenceReviewerInvocationError


class EvidenceReviewerNodes:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies

    async def review(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        iteration = int(state.get("reviewer_iteration", 0)) + 1
        review_input = build_evidence_review_input(state, iteration=iteration)
        reviewer = self.dependencies.evidence_reviewer
        reviewer_config = self.dependencies.evidence_reviewer_config
        errors: list[str] = []

        try:
            if not reviewer_config.enabled:
                raise RuntimeError("Evidence Reviewer is disabled")
            if reviewer is None:
                raise RuntimeError("Evidence Reviewer is enabled but not configured")
            call = await reviewer.review(review_input=review_input, config=config)
            decision = call.decision
            metrics = call.metrics
        except Exception as exc:
            code = _reviewer_error_code(exc)
            errors.append(code)
            metrics = _reviewer_error_metrics(exc, config=config)
            decision = _reviewer_failure_decision(code)

        record_reviewer_trace_metadata(
            trace_name="evidence_reviewer",
            moderation_task_id=state.get("task_id"),
            judge_type="ADVERSARIAL" if review_input.judge_result is not None else "SINGLE",
            reviewer_iteration=iteration,
            problem_types=[problem.problem_type.value for problem in decision.problems],
            next_action=decision.next_action.value,
            reviewer_confidence=decision.confidence,
            model_name=metrics.model_name,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            total_tokens=metrics.total_tokens,
            latency_ms=metrics.latency_ms,
            error_count=len(errors),
        )

        return {
            "reviewer_decision": decision.model_dump(mode="json"),
            "reviewer_iteration": iteration,
            "reviewer_model_metrics": metrics.model_dump(mode="json"),
            "reviewer_errors": errors,
        }

    async def validate_route(self, state: ModerationState) -> ModerationState:
        decision = EvidenceReviewerDecision.model_validate(state["reviewer_decision"])
        progress_snapshot = _reviewer_progress_snapshot(state, decision)
        no_progress = _reviewer_made_no_progress(
            previous=state.get("reviewer_progress_snapshot"),
            current=progress_snapshot,
            revision_source=state.get("revision_source"),
        )
        route = validate_reviewer_route(
            {**state, "reviewer_no_progress": no_progress},
            decision,
            reviewer_config=self.dependencies.evidence_reviewer_config,
            tool_config=self.dependencies.tool_calling_config,
            policy_config=self.dependencies.policy_rag_config,
        )
        proposed = decision.next_action
        is_revision = route in {
            ReviewerNextAction.COLLECT_MORE_EVIDENCE,
            ReviewerNextAction.RETRIEVE_MORE_POLICY,
            ReviewerNextAction.REVISE_JUDGMENT,
        }
        history = {
            "iteration": int(state.get("reviewer_iteration", 0)),
            "input_decision_version": int(state.get("agent_decision_version", 0)),
            "decision": decision.model_dump(mode="json"),
            "validated_route": route.value,
            "proposed_route": proposed.value,
            "revision_source": state.get("revision_source"),
            "metrics": dict(state.get("reviewer_model_metrics", {})),
            "status": "FAILED" if state.get("reviewer_errors") else "SUCCEEDED",
            "error_code": (
                str(state.get("reviewer_errors", [])[-1]) if state.get("reviewer_errors") else None
            ),
            "created_at": datetime.now(UTC).isoformat(),
        }
        result: ModerationState = {
            "reviewer_route": route.value,
            "reviewer_history": [history],
            "reviewer_budget_exceeded": _reviewer_budget_exceeded(
                state,
                proposed=proposed,
                validated=route,
                dependencies=self.dependencies,
            ),
            "reviewer_progress_snapshot": progress_snapshot,
            "reviewer_no_progress": no_progress,
        }
        record_reviewer_trace_metadata(
            trace_name="reviewer_route_validation",
            moderation_task_id=state.get("task_id"),
            reviewer_iteration=state.get("reviewer_iteration", 0),
            problem_types=[problem.problem_type.value for problem in decision.problems],
            next_action=proposed.value,
            reviewer_confidence=decision.confidence,
            tool_revision_count=state.get("reviewer_tool_revision_count", 0),
            policy_revision_count=state.get("reviewer_policy_revision_count", 0),
            judgment_revision_count=state.get("reviewer_judgment_revision_count", 0),
            revision_count=state.get("reviewer_revision_count", 0),
            budget_exceeded=result["reviewer_budget_exceeded"],
            no_progress=no_progress,
            final_route=route.value,
        )
        if route == ReviewerNextAction.HUMAN_REVIEW:
            result["requires_human_review"] = True
            result["revision_source"] = ReviewerNextAction.HUMAN_REVIEW.value
            return result
        if not is_revision:
            result["revision_source"] = None
            return result

        result["reviewer_revision_count"] = int(state.get("reviewer_revision_count", 0)) + 1
        result["reviewer_revision_signatures"] = [reviewer_revision_signature(decision)]
        result["revision_source"] = route.value
        if route == ReviewerNextAction.COLLECT_MORE_EVIDENCE:
            result["reviewer_tool_revision_count"] = (
                int(state.get("reviewer_tool_revision_count", 0)) + 1
            )
            result["reviewer_feedback_for_tools"] = _tool_feedback(decision)
        elif route == ReviewerNextAction.RETRIEVE_MORE_POLICY:
            result["reviewer_policy_revision_count"] = (
                int(state.get("reviewer_policy_revision_count", 0)) + 1
            )
            result["reviewer_feedback_for_policy"] = list(
                dict.fromkeys(
                    [
                        *decision.suggested_policy_queries,
                        *(problem.description for problem in decision.problems),
                    ]
                )
            )[:20]
        else:
            result["reviewer_judgment_revision_count"] = (
                int(state.get("reviewer_judgment_revision_count", 0)) + 1
            )
            result["reviewer_feedback_for_judge"] = list(
                dict.fromkeys(
                    [
                        *decision.judgment_revision_instructions,
                        *(problem.description for problem in decision.problems),
                    ]
                )
            )[:20]
        return result

    def route_after_validation(
        self,
        state: ModerationState,
    ) -> Literal[
        "finalize",
        "collect_more_evidence",
        "retrieve_more_policy",
        "revise_judgment",
        "human_review",
    ]:
        route = ReviewerNextAction(
            state.get("reviewer_route") or ReviewerNextAction.HUMAN_REVIEW.value
        )
        if route == ReviewerNextAction.FINALIZE:
            return "finalize"
        if route == ReviewerNextAction.COLLECT_MORE_EVIDENCE:
            return "collect_more_evidence"
        if route == ReviewerNextAction.RETRIEVE_MORE_POLICY:
            return "retrieve_more_policy"
        if route == ReviewerNextAction.REVISE_JUDGMENT:
            return "revise_judgment"
        return "human_review"

    async def prepare_tool_revision(self, state: ModerationState) -> ModerationState:
        decision = EvidenceReviewerDecision.model_validate(state["reviewer_decision"])
        feedback = {
            "kind": "evidence_reviewer_revision",
            "missing_evidence": decision.missing_evidence,
            "suggested_tools": decision.suggested_tools,
            "problems": [problem.model_dump(mode="json") for problem in decision.problems],
        }
        _record_revision_cycle(state, ReviewerNextAction.COLLECT_MORE_EVIDENCE)
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "Evidence Reviewer requested targeted evidence collection. "
                        "Resolve these gaps with the minimum necessary allowed tools; do not "
                        "repeat a successful call with identical arguments.\n"
                        + json.dumps(redact_data(feedback), ensure_ascii=True)
                    )
                )
            ],
            "evidence_gaps": list(
                dict.fromkeys([*state.get("evidence_gaps", []), *_tool_feedback(decision)])
            )[:20],
            "evidence_collection_complete": False,
            "tool_agent_error": None,
            "policy_rag_requires_human_review": False,
            "requires_human_review": False,
        }

    def route_policy_after_evidence(
        self,
        state: ModerationState,
    ) -> Literal["grade", "plan", "judgment"]:
        if not ( # # 策略 RAG 没开启 → 跳过，直接去重判
            self.dependencies.policy_rag_config.enabled
            and self.dependencies.agentic_policy_retriever is not None
        ):
            return "judgment"
        # 已有 query plan → 直接评分
        return "grade" if state.get("policy_query_plan") else "plan"

    async def prepare_policy_after_evidence(self, state: ModerationState) -> ModerationState:
        del state
        return {}

    async def prepare_policy_revision(self, state: ModerationState) -> ModerationState:
        decision = EvidenceReviewerDecision.model_validate(state["reviewer_decision"])
        _record_revision_cycle(state, ReviewerNextAction.RETRIEVE_MORE_POLICY)
        grade_value = state.get("policy_grade_result")
        if grade_value:
            grade = PolicyGradeResult.model_validate(grade_value)
            topics = list(
                dict.fromkeys([*grade.missing_policy_topics, *decision.suggested_policy_queries])
            )[:20]
            grade = grade.model_copy(
                update={
                    "sufficient": False,
                    "missing_policy_topics": topics,
                    "suggested_next_action": PolicyGradeNextAction.REWRITE_QUERY,
                    "reason": (
                        "Evidence Reviewer requested a more specific Policy search: "
                        + "; ".join(decision.suggested_policy_queries)
                    )[:3000],
                }
            )
            grade_data: dict[str, Any] | None = grade.model_dump(mode="json")
        else:
            grade_data = None
        return {
            "policy_grade_result": grade_data,
            "policy_rag_complete": False,
            "policy_rag_sufficient": False,
            "policy_rewrite_no_change": False,
            "policy_rag_requires_human_review": False,
            "requires_human_review": False,
        }

    def route_policy_revision(
        self,
        state: ModerationState,
    ) -> Literal["rewrite", "plan", "human_review"]:
        if not (
            self.dependencies.policy_rag_config.enabled
            and self.dependencies.agentic_policy_retriever is not None
        ):
            return "human_review"
        if state.get("policy_query_plan") and state.get("policy_grade_result"):
            return "rewrite"
        return "plan"

    async def prepare_judgment_revision(self, state: ModerationState) -> ModerationState:
        decision = EvidenceReviewerDecision.model_validate(state["reviewer_decision"])
        _record_revision_cycle(
            state,
            ReviewerNextAction(
                state.get("revision_source") or ReviewerNextAction.REVISE_JUDGMENT.value
            ),
        )
        evidence_summary = dict(state.get("evidence_summary") or {})
        evidence_summary["reviewer_feedback"] = {
            "revision_source": state.get("revision_source"),
            "problems": [problem.model_dump(mode="json") for problem in decision.problems],
            "instructions": decision.judgment_revision_instructions,
            "missing_evidence": decision.missing_evidence,
            "suggested_policy_queries": decision.suggested_policy_queries,
        }
        return {
            "evidence_summary": redact_data(evidence_summary),
            "reviewer_judge_scope": _reviewer_judge_scope(state, decision),
            "requires_human_review": False,
        }

    def route_judgment_revision(
        self,
        state: ModerationState,
    ) -> Literal["single", "judge", "risk", "safe"] | list[Literal["risk_joint", "safe_joint"]]:
        scope = state.get("reviewer_judge_scope", "SINGLE")
        if scope == "SINGLE":
            return "single"
        if scope == "RISK":
            return "risk"
        if scope == "SAFE":
            return "safe"
        if scope == "BOTH":
            return ["risk_joint", "safe_joint"]
        return "judge"

    async def prepare_action_route(self, state: ModerationState) -> ModerationState:
        del state
        return {}

    def route_after_policy_finalize(
        self,
        state: ModerationState,
    ) -> Literal["review", "revision"]:
        if state.get("revision_source") in {
            ReviewerNextAction.COLLECT_MORE_EVIDENCE.value,
            ReviewerNextAction.RETRIEVE_MORE_POLICY.value,
        }:
            return "revision"
        return "review"


def build_evidence_review_input(
    state: ModerationState,
    *,
    iteration: int,
) -> EvidenceReviewInput:
    context_value = state.get("context_evidence")
    errors = [
        *state.get("adversarial_errors", []),
        *state.get("policy_rag_errors", []),
    ]
    if state.get("tool_agent_error"):
        errors.append(str(state["tool_agent_error"]))
    return EvidenceReviewInput(
        content=state["normalized_content"],
        content_hash=state.get("content_hash"),
        content_type=ModerationContentType(state.get("content_type", "TEXT")),
        classification=RiskClassification.model_validate(state["classification"]),
        decision=AgentDecision.model_validate(state["agent_decision"]),
        policies=tuple(
            PolicyEvidence.model_validate(value) for value in state.get("matched_policies", [])
        ),
        cases=tuple(CaseEvidence.model_validate(value) for value in state.get("similar_cases", [])),
        context=(
            ModerationContextEvidence.model_validate(context_value) if context_value else None
        ),
        signals=tuple(
            ModerationSignalEvidence.model_validate(value) for value in state.get("signals", [])
        ),
        evidence_summary=state.get("evidence_summary"),
        policy_evidence_summary=state.get("policy_evidence_summary"),
        risk_result=_optional_model(state.get("risk_agent_result"), RiskAgentResult),
        safe_result=_optional_model(state.get("safe_agent_result"), SafeAgentResult),
        judge_result=_optional_model(state.get("judge_agent_result"), JudgeAgentResult),
        agent_conflict=bool(state.get("agent_conflict")),
        agent_errors=tuple(dict.fromkeys(errors))[:50],
        evidence_check_passed=bool(state.get("evidence_check_passed")),
        evidence_check_issues=tuple(state.get("evidence_check_issues", [])),
        reviewer_iteration=iteration,
    )


def _reviewer_judge_scope(
    state: ModerationState,
    decision: EvidenceReviewerDecision,
) -> str:
    if not state.get("use_adversarial_review", False):
        return "SINGLE"
    if state.get("revision_source") in {
        ReviewerNextAction.COLLECT_MORE_EVIDENCE.value,
        ReviewerNextAction.RETRIEVE_MORE_POLICY.value,
    }:
        return "BOTH"

    text = " ".join(
        [
            *(
                " ".join(
                    [
                        problem.problem_type.value,
                        problem.description,
                        *problem.affected_fields,
                    ]
                )
                for problem in decision.problems
            ),
            *decision.judgment_revision_instructions,
        ]
    ).lower()
    risk = "risk agent" in text or "risk_agent" in text or "risk_agent_result" in text
    safe = "safe agent" in text or "safe_agent" in text or "safe_agent_result" in text
    if state.get("risk_agent_result") is None:
        risk = True
    if state.get("safe_agent_result") is None:
        safe = True
    if risk and safe:
        return "BOTH"
    if risk:
        return "RISK"
    if safe:
        return "SAFE"
    return "JUDGE"


def _tool_feedback(decision: EvidenceReviewerDecision) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *decision.missing_evidence,
                *(problem.description for problem in decision.problems),
            ]
        )
    )[:20]


def _reviewer_progress_snapshot(
    state: ModerationState,
    decision: EvidenceReviewerDecision,
) -> dict[str, Any]:
    successful_tool_signatures = sorted(state.get("tool_call_cache", {}))
    policy_values = [
        *state.get("applicable_policies", []),
        *state.get("partial_policies", []),
    ]
    policy_ids = sorted(
        {str(value.get("policy_id")) for value in policy_values if value.get("policy_id")}
    )
    query_count = sum(
        len(entry.get("queries", [])) for entry in state.get("policy_query_history", [])
    )
    problem_types = sorted(problem.problem_type.value for problem in decision.problems)
    policy_summary = state.get("policy_evidence_summary") or {}
    policy_status = {
        "sufficient": bool(policy_summary.get("sufficient")),
        "applicable_policy_ids": sorted(
            str(value.get("policy_id"))
            for value in policy_summary.get("applicable_policies", [])
            if value.get("policy_id")
        ),
        "partial_policy_ids": sorted(
            str(value.get("policy_id"))
            for value in policy_summary.get("partial_policies", [])
            if value.get("policy_id")
        ),
    }
    decision_value = AgentDecision.model_validate(state["agent_decision"])
    decision_payload = {
        "risk_type": decision_value.risk_type.value,
        "risk_score": decision_value.risk_score,
        "confidence": decision_value.confidence,
        "recommended_action": decision_value.recommended_action.value,
        "reason": decision_value.reason,
        "matched_policy_ids": sorted(
            str(policy.policy_id) for policy in decision_value.matched_policies
        ),
        "evidence_complete": decision_value.evidence_complete,
    }
    return {
        "successful_tool_signatures": successful_tool_signatures,
        "context_fingerprint": _fingerprint(state.get("context_evidence")),
        "policy_ids": policy_ids,
        "policy_query_count": query_count,
        "policy_summary_fingerprint": _fingerprint(policy_status),
        "decision_fingerprint": _fingerprint(decision_payload),
        "problem_types": problem_types,
        "problem_count": len(decision.problems),
        "evidence_gap_count": len(state.get("evidence_gaps", [])),
    }


def _reviewer_made_no_progress(
    *,
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    revision_source: str | None,
) -> bool:
    if not previous or revision_source not in {
        ReviewerNextAction.COLLECT_MORE_EVIDENCE.value,
        ReviewerNextAction.RETRIEVE_MORE_POLICY.value,
        ReviewerNextAction.REVISE_JUDGMENT.value,
    }:
        return False

    problem_reduced = int(current["problem_count"]) < int(previous["problem_count"])
    if revision_source == ReviewerNextAction.COLLECT_MORE_EVIDENCE.value:
        progressed = (
            bool(
                set(current["successful_tool_signatures"])
                - set(previous["successful_tool_signatures"])
            )
            or current["context_fingerprint"] != previous["context_fingerprint"]
        )
    elif revision_source == ReviewerNextAction.RETRIEVE_MORE_POLICY.value:
        progressed = bool(set(current["policy_ids"]) - set(previous["policy_ids"])) or (
            current["policy_summary_fingerprint"] != previous["policy_summary_fingerprint"]
            and int(current["policy_query_count"]) > int(previous["policy_query_count"])
        )
    else:
        progressed = current["decision_fingerprint"] != previous["decision_fingerprint"]
    return not (progressed or problem_reduced)


def _reviewer_budget_exceeded(
    state: ModerationState,
    *,
    proposed: ReviewerNextAction,
    validated: ReviewerNextAction,
    dependencies: ModerationDependencies,
) -> bool:
    if validated != ReviewerNextAction.HUMAN_REVIEW:
        return False
    config = dependencies.evidence_reviewer_config
    if int(state.get("reviewer_revision_count", 0)) >= config.max_iterations:
        return True
    if proposed == ReviewerNextAction.COLLECT_MORE_EVIDENCE:
        return bool(
            int(state.get("reviewer_tool_revision_count", 0)) >= config.max_tool_revisions
            or state.get("tool_budget_exceeded")
            or int(state.get("tool_call_count", 0))
            >= dependencies.tool_calling_config.max_total_calls
            or int(state.get("tool_call_round", 0)) >= dependencies.tool_calling_config.max_rounds
        )
    if proposed == ReviewerNextAction.RETRIEVE_MORE_POLICY:
        return bool(
            int(state.get("reviewer_policy_revision_count", 0)) >= config.max_policy_revisions
            or state.get("policy_rag_budget_exceeded")
            or int(state.get("policy_retrieval_round", 0))
            >= dependencies.policy_rag_config.max_retrieval_rounds
        )
    if proposed == ReviewerNextAction.REVISE_JUDGMENT:
        return int(state.get("reviewer_judgment_revision_count", 0)) >= (
            config.max_judgment_revisions
        )
    return False


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        redact_data(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_revision_cycle(
    state: ModerationState,
    route: ReviewerNextAction,
) -> None:
    record_reviewer_trace_metadata(
        trace_name="reviewer_revision_cycle",
        moderation_task_id=state.get("task_id"),
        reviewer_iteration=state.get("reviewer_iteration", 0),
        next_action=route.value,
        tool_revision_count=state.get("reviewer_tool_revision_count", 0),
        policy_revision_count=state.get("reviewer_policy_revision_count", 0),
        judgment_revision_count=state.get("reviewer_judgment_revision_count", 0),
        revision_count=state.get("reviewer_revision_count", 0),
    )


def _reviewer_failure_decision(code: str) -> EvidenceReviewerDecision:
    return EvidenceReviewerDecision(
        passed=False,
        problems=[
            ReviewerProblem(
                problem_type=ReviewerProblemType.PARTIAL_AGENT_FAILURE,
                description="Evidence Reviewer failed and automated disposition is unsafe.",
                affected_fields=["reviewer_decision"],
                severity="CRITICAL",
                supporting_evidence=[code[:1000]],
            )
        ],
        next_action=ReviewerNextAction.HUMAN_REVIEW,
        confidence=1.0,
        reason="The semantic review could not be completed reliably.",
    )


def _reviewer_error_code(error: Exception) -> str:
    if isinstance(error, EvidenceReviewerInvocationError):
        return error.code
    return f"evidence_reviewer:{type(error).__name__}"


def _reviewer_error_metrics(
    error: Exception,
    *,
    config: RunnableConfig,
) -> EvidenceReviewerMetrics:
    if isinstance(error, EvidenceReviewerInvocationError):
        return error.metrics
    started = perf_counter()
    model_name = str(
        config.get("configurable", {}).get("model", settings.DEFAULT_MODEL) or "unconfigured"
    )
    return EvidenceReviewerMetrics(
        model_name=model_name,
        latency_ms=(perf_counter() - started) * 1000,
    )


def _optional_model[ModelT: BaseModel](
    value: Any,
    model_type: type[ModelT],
) -> ModelT | None:
    if value is None:
        return None
    try:
        return model_type.model_validate(value)
    except (TypeError, ValueError):
        return None
