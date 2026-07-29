import asyncio
from time import perf_counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.prompts import (
    EVIDENCE_REVIEWER_SYSTEM_PROMPT,
    EVIDENCE_REVIEWER_TASK_PROMPT,
)
from core import get_model, settings
from moderation.schemas import (
    EvidenceReviewerConfig,
    EvidenceReviewerDecision,
    EvidenceReviewerMetrics,
)
from moderation.security import redact_data

from .dependencies import EvidenceReviewerCall, EvidenceReviewInput
from .structured_output import bind_moderation_structured_output


class EvidenceReviewerInvocationError(RuntimeError):
    def __init__(self, code: str, metrics: EvidenceReviewerMetrics) -> None:
        super().__init__(code)
        self.code = code
        self.metrics = metrics


class ReviewerStructuredOutputError(ValueError):
    pass


class LLMEvidenceReviewerModel:
    def __init__(
        self,
        reviewer_config: EvidenceReviewerConfig | None = None,
    ) -> None:
        self.reviewer_config = reviewer_config or settings.evidence_reviewer_config()

    async def review(
        self,
        *,
        review_input: EvidenceReviewInput,
        config: RunnableConfig,
    ) -> EvidenceReviewerCall:
        model_name_value = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        started = perf_counter()
        model_name = str(model_name_value or "unconfigured")
        repair_attempted = False
        try:
            model = get_model(model_name_value)  # type: ignore[arg-type]
            runnable = bind_moderation_structured_output(
                model,
                EvidenceReviewerDecision,
                model_name=model_name_value,
                include_raw=True,
            )
            call_config = _reviewer_config(
                config,
                review_input=review_input,
                model_name=model_name,
            )
            messages = [
                SystemMessage(content=EVIDENCE_REVIEWER_SYSTEM_PROMPT),
                HumanMessage(content=_reviewer_prompt(review_input)),
            ]
            async with asyncio.timeout(self.reviewer_config.agent_timeout_seconds):
                result, raw, repair_attempted = await _invoke_with_one_repair(
                    runnable,
                    messages,
                    call_config,
                )
            decision = EvidenceReviewerDecision.model_validate(
                redact_data(result.model_dump(mode="python"))
            )
            return EvidenceReviewerCall(
                decision=decision,
                metrics=_metrics(
                    model_name,
                    started,
                    raw,
                    repair_attempted=repair_attempted,
                ),
            )
        except Exception as exc:
            if isinstance(exc, ReviewerStructuredOutputError):
                repair_attempted = True
            metrics = _metrics(
                model_name,
                started,
                repair_attempted=repair_attempted,
            )
            code = f"evidence_reviewer:{type(exc).__name__}"
            raise EvidenceReviewerInvocationError(code, metrics) from exc


async def _invoke_with_one_repair(
    runnable: Any,
    messages: list[Any],
    config: RunnableConfig,
) -> tuple[EvidenceReviewerDecision, Any, bool]:
    current_messages = list(messages)
    for attempt in range(2):
        response = await runnable.ainvoke(current_messages, config)
        parsed, raw, parsing_error = _structured_response(response)
        if parsing_error is None and parsed is not None:
            try:
                return EvidenceReviewerDecision.model_validate(parsed), raw, attempt > 0
            except Exception:
                pass
        if attempt == 0:
            current_messages.append(
                SystemMessage(
                    content=(
                        "The previous response did not match EvidenceReviewerDecision. "
                        "Return one corrected structured result. Do not output a final "
                        "moderation action or invent evidence, Policy IDs, or tools."
                    )
                )
            )
    raise ReviewerStructuredOutputError("Evidence Reviewer structured output could not be repaired")


def _reviewer_prompt(review_input: EvidenceReviewInput) -> str:
    judge_type = "ADVERSARIAL" if review_input.judge_result is not None else "SINGLE"
    return EVIDENCE_REVIEWER_TASK_PROMPT.format(
        content=review_input.content,
        content_type=review_input.content_type.value,
        classification=review_input.classification.model_dump_json(),
        judge_type=judge_type,
        agent_decision=review_input.decision.model_dump_json(),
        evidence_check_passed=str(review_input.evidence_check_passed),
        evidence_check_issues=redact_data(list(review_input.evidence_check_issues)) or "None",
        context=(
            review_input.context.model_dump_json() if review_input.context is not None else "None"
        ),
        signals="\n".join(item.model_dump_json() for item in review_input.signals) or "None",
        policies="\n".join(item.model_dump_json() for item in review_input.policies) or "None",
        policy_evidence_summary=(
            redact_data(review_input.policy_evidence_summary)
            if review_input.policy_evidence_summary
            else "None"
        ),
        cases="\n".join(item.model_dump_json() for item in review_input.cases) or "None",
        evidence_summary=(
            redact_data(review_input.evidence_summary) if review_input.evidence_summary else "None"
        ),
        risk_result=(
            review_input.risk_result.model_dump_json()
            if review_input.risk_result is not None
            else "None"
        ),
        safe_result=(
            review_input.safe_result.model_dump_json()
            if review_input.safe_result is not None
            else "None"
        ),
        judge_result=(
            review_input.judge_result.model_dump_json()
            if review_input.judge_result is not None
            else "None"
        ),
        agent_conflict=str(review_input.agent_conflict),
        agent_errors="\n".join(review_input.agent_errors) or "None",
        reviewer_iteration=review_input.reviewer_iteration,
    )


def _reviewer_config(
    config: RunnableConfig,
    *,
    review_input: EvidenceReviewInput,
    model_name: str,
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "evidence_reviewer"
    call_config["tags"] = list(
        dict.fromkeys(
            [*config.get("tags", []), "moderation", "reviewer", "evidence_reviewer", "skip_stream"]
        )
    )
    configurable = config.get("configurable", {})
    call_config["metadata"] = redact_data(
        {
            "moderation_task_id": configurable.get("moderation_task_id"),
            "judge_type": "ADVERSARIAL" if review_input.judge_result is not None else "SINGLE",
            "reviewer_iteration": review_input.reviewer_iteration,
            "risk_type": review_input.classification.risk_type.value,
            "decision_action": review_input.decision.recommended_action.value,
            "policy_count": len(review_input.policies),
            "agent_conflict": review_input.agent_conflict,
            "model_name": model_name,
        }
    )
    return call_config


def _structured_response(response: Any) -> tuple[Any, Any, Any]:
    if isinstance(response, dict) and "parsed" in response:
        return response.get("parsed"), response.get("raw"), response.get("parsing_error")
    return response, None, None


def _metrics(
    model_name: str,
    started: float,
    raw: Any = None,
    *,
    repair_attempted: bool = False,
) -> EvidenceReviewerMetrics:
    input_tokens, output_tokens, total_tokens = _token_usage(raw)
    return EvidenceReviewerMetrics(
        model_name=model_name,
        latency_ms=(perf_counter() - started) * 1000,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        repair_attempted=repair_attempted,
    )


def _token_usage(raw: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(raw, "usage_metadata", None) or {}
    response_metadata = getattr(raw, "response_metadata", None) or {}
    provider_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
    input_tokens = _integer(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _integer(provider_usage.get("prompt_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _integer(provider_usage.get("completion_tokens"))
    total_tokens = _integer(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _integer(provider_usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and value >= 0 else None
