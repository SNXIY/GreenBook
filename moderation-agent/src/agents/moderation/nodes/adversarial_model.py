import asyncio
from time import perf_counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from agents.moderation.nodes.dependencies import (
    AdversarialAgentCall,
    AdversarialReviewInput,
)
from agents.moderation.prompts import (
    ADVERSARIAL_JUDGE_PROMPT,
    RISK_INVESTIGATOR_PROMPT,
    SAFE_ADVOCATE_PROMPT,
)
from core import get_model, settings
from moderation.schemas import (
    AdversarialAgentMetrics,
    AdversarialTraceName,
    JudgeAgentResult,
    RiskAgentResult,
    SafeAgentResult,
)

from .structured_output import bind_moderation_structured_output


class AdversarialAgentInvocationError(RuntimeError):
    def __init__(
        self,
        code: str,
        metrics: AdversarialAgentMetrics,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.metrics = metrics


class LLMAdversarialReviewModel:
    def __init__(self, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = (
            settings.MODERATION_ADVERSARIAL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

    async def investigate(
        self,
        *,
        review_input: AdversarialReviewInput,
        config: RunnableConfig,
    ) -> AdversarialAgentCall[RiskAgentResult]:
        values = _prompt_values(review_input)
        return await self._invoke(
            schema=RiskAgentResult,
            trace_name="risk_investigator",
            system_message=(
                "Find supported moderation risk. Do not make the final enforcement decision."
            ),
            prompt=RISK_INVESTIGATOR_PROMPT.format(**values),
            review_input=review_input,
            config=config,
        )

    async def advocate(
        self,
        *,
        review_input: AdversarialReviewInput,
        config: RunnableConfig,
    ) -> AdversarialAgentCall[SafeAgentResult]:
        values = _prompt_values(review_input)
        return await self._invoke(
            schema=SafeAgentResult,
            trace_name="safe_advocate",
            system_message=("Identify supported harmless interpretations and false-positive risk."),
            prompt=SAFE_ADVOCATE_PROMPT.format(**values),
            review_input=review_input,
            config=config,
        )

    async def decide_adversarial(
        self,
        *,
        review_input: AdversarialReviewInput,
        risk_result: RiskAgentResult | None,
        safe_result: SafeAgentResult | None,
        agent_conflict: bool,
        agent_errors: tuple[str, ...],
        config: RunnableConfig,
    ) -> AdversarialAgentCall[JudgeAgentResult]:
        values = _prompt_values(review_input)
        values.update(
            {
                "risk_result": (
                    risk_result.model_dump_json() if risk_result is not None else "Unavailable"
                ),
                "safe_result": (
                    safe_result.model_dump_json() if safe_result is not None else "Unavailable"
                ),
                "agent_conflict": str(agent_conflict),
                "agent_errors": "\n".join(agent_errors) or "None",
            }
        )
        return await self._invoke(
            schema=JudgeAgentResult,
            trace_name="adversarial_judge",
            system_message="Judge the supplied evidence and return the final structured decision.",
            prompt=ADVERSARIAL_JUDGE_PROMPT.format(**values),
            review_input=review_input,
            config=config,
            agent_conflict=agent_conflict,
        )

    async def _invoke[ResultT: BaseModel](
        self,
        *,
        schema: type[ResultT],
        trace_name: AdversarialTraceName,
        system_message: str,
        prompt: str,
        review_input: AdversarialReviewInput,
        config: RunnableConfig,
        agent_conflict: bool = False,
    ) -> AdversarialAgentCall[ResultT]:
        started = perf_counter()
        model_name_value = config.get("configurable", {}).get(
            "model",
            settings.DEFAULT_MODEL,
        )
        model_name = str(model_name_value or "unconfigured")
        try:
            model = get_model(model_name_value)  # type: ignore[arg-type]
            runnable = bind_moderation_structured_output(
                model,
                schema,
                model_name=model_name_value,
                include_raw=True,
            )
            call_config = config.copy()
            call_config.pop("run_id", None)
            call_config["run_name"] = trace_name
            call_config["tags"] = list(
                dict.fromkeys(
                    [
                        *config.get("tags", []),
                        "moderation",
                        "adversarial",
                        trace_name,
                        "skip_stream",
                    ]
                )
            )
            call_config["metadata"] = _trace_metadata(
                review_input,
                config,
                model_name,
                agent_conflict,
            )
            async with asyncio.timeout(self.timeout_seconds):
                response: Any = await runnable.ainvoke(
                    [
                        SystemMessage(content=system_message),
                        HumanMessage(content=prompt),
                    ],
                    call_config,
                )
            parsed, raw, parsing_error = _structured_response(response)
            if parsing_error is not None:
                raise ValueError("structured output parsing failed")
            result = schema.model_validate(parsed)
            metrics = _metrics(trace_name, model_name, started, raw)
            return AdversarialAgentCall(result=result, metrics=metrics)
        except Exception as exc:
            metrics = _metrics(trace_name, model_name, started)
            code = f"{trace_name}:{type(exc).__name__}"
            raise AdversarialAgentInvocationError(code, metrics) from exc


def _prompt_values(review_input: AdversarialReviewInput) -> dict[str, str]:
    return {
        "content": review_input.content,
        "content_type": review_input.content_type.value,
        "classification": review_input.classification.model_dump_json(),
        "context": (
            review_input.context.model_dump_json() if review_input.context is not None else "None"
        ),
        "signals": "\n".join(item.model_dump_json() for item in review_input.signals) or "None",
        "policies": "\n".join(item.model_dump_json() for item in review_input.policies) or "None",
        "cases": "\n".join(item.model_dump_json() for item in review_input.cases) or "None",
        "evidence_summary": (
            str(review_input.evidence_summary) if review_input.evidence_summary else "None"
        ),
    }


def _trace_metadata(
    review_input: AdversarialReviewInput,
    config: RunnableConfig,
    model_name: str,
    agent_conflict: bool,
) -> dict[str, Any]:
    configurable = config.get("configurable", {})
    return {
        "moderation_task_id": configurable.get("moderation_task_id"),
        "content_hash": review_input.content_hash,
        "risk_type": review_input.classification.risk_type.value,
        "policy_versions": {
            policy.code: policy.version for policy in review_input.policies if policy.version
        },
        "model_name": model_name,
        "agent_conflict": agent_conflict,
    }


def _structured_response(response: Any) -> tuple[Any, Any, Any]:
    if isinstance(response, dict) and "parsed" in response:
        return response.get("parsed"), response.get("raw"), response.get("parsing_error")
    return response, None, None


def _metrics(
    trace_name: AdversarialTraceName,
    model_name: str,
    started: float,
    raw: Any = None,
) -> AdversarialAgentMetrics:
    input_tokens, output_tokens, total_tokens = _token_usage(raw)
    return AdversarialAgentMetrics(
        trace_name=trace_name,
        model_name=model_name,
        latency_ms=(perf_counter() - started) * 1000,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
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
