from time import perf_counter
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from agents.moderation.adversarial import (
    detect_agent_conflict,
    map_judge_to_agent_decision,
    validate_risk_agent_result,
    validate_safe_agent_result,
)
from agents.moderation.state import ModerationState
from core import settings
from moderation.schemas import (
    AdversarialAgentMetrics,
    AdversarialTraceName,
    CaseEvidence,
    JudgeAgentResult,
    ModerationAction,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyEvidence,
    RiskAgentResult,
    RiskClassification,
    SafeAgentResult,
)

from .adversarial_model import AdversarialAgentInvocationError
from .dependencies import AdversarialReviewInput, ModerationDependencies


class AdversarialReviewNodes:
    def __init__(
        self,
        dependencies: ModerationDependencies,
        *,
        partial_confidence_cap: float = 0.69,
    ) -> None:
        if not 0.0 <= partial_confidence_cap <= 1.0:
            raise ValueError("partial_confidence_cap must be between 0 and 1")
        self.dependencies = dependencies
        self.partial_confidence_cap = partial_confidence_cap

    async def risk_investigator(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        started = perf_counter()
        agent = self.dependencies.risk_investigator
        if agent is None:
            return _failed_agent_state(
                result_key="risk_agent_result",
                metrics_key="risk_agent_metrics",
                trace_name="risk_investigator",
                code="risk_investigator:NotConfigured",
                config=config,
                started=started,
            )
        try:
            call = await agent.investigate(
                review_input=build_adversarial_review_input(state),
                config=config,
            )
            validation = validate_risk_agent_result(state, call.result)
            return {
                "risk_agent_result": validation.result.model_dump(mode="json"),
                "risk_agent_metrics": call.metrics.model_dump(mode="json"),
                "adversarial_errors": list(validation.errors),
            }
        except Exception as exc:
            return _failed_agent_state(
                result_key="risk_agent_result",
                metrics_key="risk_agent_metrics",
                trace_name="risk_investigator",
                code=_error_code("risk_investigator", exc),
                config=config,
                started=started,
                error=exc,
            )

    async def safe_advocate(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        started = perf_counter()
        agent = self.dependencies.safe_advocate
        if agent is None:
            return _failed_agent_state(
                result_key="safe_agent_result",
                metrics_key="safe_agent_metrics",
                trace_name="safe_advocate",
                code="safe_advocate:NotConfigured",
                config=config,
                started=started,
            )
        try:
            call = await agent.advocate(
                review_input=build_adversarial_review_input(state),
                config=config,
            )
            validation = validate_safe_agent_result(state, call.result)
            return {
                "safe_agent_result": validation.result.model_dump(mode="json"),
                "safe_agent_metrics": call.metrics.model_dump(mode="json"),
                "adversarial_errors": list(validation.errors),
            }
        except Exception as exc:
            return _failed_agent_state(
                result_key="safe_agent_result",
                metrics_key="safe_agent_metrics",
                trace_name="safe_advocate",
                code=_error_code("safe_advocate", exc),
                config=config,
                started=started,
                error=exc,
            )

    async def adversarial_judge(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        started = perf_counter()
        risk_result, risk_error = _optional_result(
            state.get("risk_agent_result"),
            RiskAgentResult,
            "risk_agent_result",
        )
        safe_result, safe_error = _optional_result(
            state.get("safe_agent_result"),
            SafeAgentResult,
            "safe_agent_result",
        )
        local_errors = [error for error in (risk_error, safe_error) if error]
        prior_errors = tuple(state.get("adversarial_errors", []))
        agent_conflict = bool(
            risk_result is not None
            and safe_result is not None
            and detect_agent_conflict(risk_result, safe_result)
        )
        review_input = build_adversarial_review_input(state)

        if risk_result is None and safe_result is None:
            return _fallback_judge_state(
                state=state,
                review_input=review_input,
                agent_conflict=agent_conflict,
                code="adversarial_judge:NoAgentResults",
                config=config,
                started=started,
                extra_errors=local_errors,
            )
        judge = self.dependencies.adversarial_judge
        if judge is None:
            return _fallback_judge_state(
                state=state,
                review_input=review_input,
                agent_conflict=agent_conflict,
                code="adversarial_judge:NotConfigured",
                config=config,
                started=started,
                extra_errors=local_errors,
            )
        try:
            call = await judge.decide_adversarial(
                review_input=review_input,
                risk_result=risk_result,
                safe_result=safe_result,
                agent_conflict=agent_conflict,
                agent_errors=prior_errors + tuple(local_errors),
                config=config,
            )
            result = call.result
            partial_or_invalid = bool(prior_errors or local_errors) or (
                risk_result is None or safe_result is None
            )
            if partial_or_invalid:
                result = result.model_copy(
                    update={
                        "confidence": min(result.confidence, self.partial_confidence_cap),
                        "need_human_review": True,
                    }
                )
            mapping = map_judge_to_agent_decision(state, result)
            return {
                "judge_agent_result": result.model_dump(mode="json"),
                "judge_agent_metrics": call.metrics.model_dump(mode="json"),
                "agent_decision": mapping.decision.model_dump(mode="json"),
                "agent_decision_version": int(state.get("agent_decision_version", 0)) + 1,
                "agent_conflict": agent_conflict,
                "adversarial_review_count": state.get("adversarial_review_count", 0) + 1,
                "adversarial_errors": local_errors + list(mapping.errors),
                "requires_human_review": (
                    mapping.decision.recommended_action == ModerationAction.HUMAN_REVIEW
                ),
            }
        except Exception as exc:
            return _fallback_judge_state(
                state=state,
                review_input=review_input,
                agent_conflict=agent_conflict,
                code=_error_code("adversarial_judge", exc),
                config=config,
                started=started,
                error=exc,
                extra_errors=local_errors,
            )


def build_adversarial_review_input(state: ModerationState) -> AdversarialReviewInput:
    context_value = state.get("context_evidence")
    return AdversarialReviewInput(
        content=state["normalized_content"],
        content_hash=state.get("content_hash"),
        content_type=ModerationContentType(state.get("content_type", "TEXT")),
        classification=RiskClassification.model_validate(state["classification"]),
        policies=tuple(
            PolicyEvidence.model_validate(policy) for policy in state.get("matched_policies", [])
        ),
        cases=tuple(CaseEvidence.model_validate(case) for case in state.get("similar_cases", [])),
        context=(
            ModerationContextEvidence.model_validate(context_value) if context_value else None
        ),
        signals=tuple(
            ModerationSignalEvidence.model_validate(signal) for signal in state.get("signals", [])
        ),
        evidence_summary=state.get("evidence_summary"),
    )


def _fallback_judge_state(
    *,
    state: ModerationState,
    review_input: AdversarialReviewInput,
    agent_conflict: bool,
    code: str,
    config: RunnableConfig,
    started: float,
    error: Exception | None = None,
    extra_errors: list[str] | None = None,
) -> ModerationState:
    result = JudgeAgentResult(
        action=ModerationAction.HUMAN_REVIEW,
        risk_type=review_input.classification.risk_type,
        risk_score=review_input.classification.risk_score,
        confidence=0.0,
        reason="Adversarial review could not produce a reliable automated decision.",
        need_human_review=True,
    )
    mapping = map_judge_to_agent_decision(state, result)
    metrics = _failure_metrics(
        "adversarial_judge",
        config,
        started,
        error,
    )
    return {
        "judge_agent_result": result.model_dump(mode="json"),
        "judge_agent_metrics": metrics.model_dump(mode="json"),
        "agent_decision": mapping.decision.model_dump(mode="json"),
        "agent_decision_version": int(state.get("agent_decision_version", 0)) + 1,
        "agent_conflict": agent_conflict,
        "adversarial_review_count": state.get("adversarial_review_count", 0) + 1,
        "adversarial_errors": [*(extra_errors or []), code, *mapping.errors],
        "requires_human_review": True,
    }


def _failed_agent_state(
    *,
    result_key: str,
    metrics_key: str,
    trace_name: AdversarialTraceName,
    code: str,
    config: RunnableConfig,
    started: float,
    error: Exception | None = None,
) -> ModerationState:
    metrics = _failure_metrics(trace_name, config, started, error)
    return cast(
        ModerationState,
        {
            result_key: None,
            metrics_key: metrics.model_dump(mode="json"),
            "adversarial_errors": [code],
        },
    )


def _optional_result[ResultT: BaseModel](
    value: dict[str, Any] | None,
    model_type: type[ResultT],
    field_name: str,
) -> tuple[ResultT | None, str | None]:
    if value is None:
        return None, None
    try:
        return model_type.model_validate(value), None
    except Exception:
        return None, f"adversarial_judge:Invalid{field_name.title().replace('_', '')}"


def _failure_metrics(
    trace_name: AdversarialTraceName,
    config: RunnableConfig,
    started: float,
    error: Exception | None,
) -> AdversarialAgentMetrics:
    if isinstance(error, AdversarialAgentInvocationError):
        return error.metrics
    model_name = str(
        config.get("configurable", {}).get("model", settings.DEFAULT_MODEL) or "unconfigured"
    )
    return AdversarialAgentMetrics(
        trace_name=trace_name,
        model_name=model_name,
        latency_ms=(perf_counter() - started) * 1000,
    )


def _error_code(trace_name: AdversarialTraceName, error: Exception) -> str:
    if isinstance(error, AdversarialAgentInvocationError):
        return error.code
    return f"{trace_name}:{type(error).__name__}"
