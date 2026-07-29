import asyncio
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.prompts import (
    POLICY_QUERY_PLANNER_SYSTEM_PROMPT,
    POLICY_QUERY_PLANNER_TASK_PROMPT,
)
from core import get_model, settings
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    ModerationSignalEvidence,
    PolicyEvidence,
    PolicyQueryPlan,
    RiskClassification,
    RiskType,
)
from moderation.security import redact_data, redact_text

from .dependencies import PolicyPlannerCall
from .structured_output import bind_moderation_structured_output


class LLMPolicyQueryPlanner:
    """Plans policy retrieval using only the configured real model."""

    def __init__(
        self,
        rag_config: AgenticPolicyRAGConfig | None = None,
    ) -> None:
        self.rag_config = rag_config or settings.agentic_policy_rag_config()

    async def plan(
        self,
        *,
        content: str,
        classification: RiskClassification,
        signals: list[ModerationSignalEvidence],
        risk_hypotheses: list[RiskType],
        evidence_summary: dict[str, Any] | None,
        preliminary_policies: list[PolicyEvidence],
        config: RunnableConfig,
    ) -> PolicyPlannerCall:
        model_name_value = config.get("configurable", {}).get(
            "model",
            settings.DEFAULT_MODEL,
        )
        model = get_model(model_name_value)  # type: ignore[arg-type]
        runnable = bind_moderation_structured_output(
            model,
            PolicyQueryPlan,
            model_name=model_name_value,
            include_raw=True,
        )
        call_config = _planner_config(
            config,
            classification=classification,
            model_name=str(model_name_value or "unconfigured"),
            preliminary_policy_count=len(preliminary_policies),
        )
        messages = [
            SystemMessage(content=POLICY_QUERY_PLANNER_SYSTEM_PROMPT),
            HumanMessage(
                content=POLICY_QUERY_PLANNER_TASK_PROMPT.format(
                    content=content,
                    classification=classification.model_dump_json(),
                    signals=(
                        "\n".join(signal.model_dump_json() for signal in signals)
                        or "None"
                    ),
                    evidence_summary=(
                        redact_data(evidence_summary)
                        if evidence_summary
                        else "None"
                    ),
                    preliminary_policies=(
                        "\n".join(
                            policy.model_dump_json()
                            for policy in preliminary_policies
                        )
                        or "None"
                    ),
                    max_queries=self.rag_config.max_queries_per_round,
                )
            ),
        ]
        async with asyncio.timeout(self.rag_config.agent_timeout_seconds):
            response = await runnable.ainvoke(messages, call_config)
        if not isinstance(response, dict) or response.get("parsing_error") is not None:
            raise ValueError("Policy Query Planner returned invalid structured output")
        parsed = response.get("parsed")
        plan = _sanitize_plan(
            PolicyQueryPlan.model_validate(parsed),
            classification=classification,
            max_queries=self.rag_config.max_queries_per_round,
        )
        return PolicyPlannerCall(plan=plan)


def _sanitize_plan(
    plan: PolicyQueryPlan,
    *,
    classification: RiskClassification,
    max_queries: int,
) -> PolicyQueryPlan:
    data = redact_data(plan.model_dump(mode="python"))
    data["queries"] = [
        redact_text(str(query))
        for query in data["queries"][:max_queries]
    ]
    hypotheses = data.get("risk_hypotheses") or [classification.risk_type]
    data["risk_hypotheses"] = hypotheses
    data["risk_type_filters"] = data.get("risk_type_filters") or hypotheses
    return PolicyQueryPlan.model_validate(data)


def _planner_config(
    config: RunnableConfig,
    *,
    classification: RiskClassification,
    model_name: str,
    preliminary_policy_count: int,
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "policy_query_planner"
    call_config["tags"] = list(
        dict.fromkeys(
            [
                *config.get("tags", []),
                "moderation",
                "policy_rag",
                "planner",
                "skip_stream",
            ]
        )
    )
    call_config["metadata"] = {
        "moderation_task_id": config.get("configurable", {}).get(
            "moderation_task_id"
        ),
        "initial_risk_type": classification.risk_type.value,
        "model_name": model_name,
        "preliminary_policy_count": preliminary_policy_count,
    }
    return call_config
