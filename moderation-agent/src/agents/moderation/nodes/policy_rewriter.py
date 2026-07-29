import asyncio
import hashlib
import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.prompts import (
    POLICY_QUERY_REWRITER_SYSTEM_PROMPT,
    POLICY_QUERY_REWRITER_TASK_PROMPT,
)
from core import get_model, settings
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    PolicyGradeResult,
    PolicyQueryPlan,
    RetrievedPolicy,
    RewrittenPolicyQuery,
)
from moderation.security import redact_data, redact_text
from rag.policy.text import normalize_policy_query

from .dependencies import PolicyRewriterCall
from .structured_output import bind_moderation_structured_output


class LLMPolicyQueryRewriter:
    """Rewrites policy queries using only the configured real model."""

    def __init__(
        self,
        rag_config: AgenticPolicyRAGConfig | None = None,
    ) -> None:
        self.rag_config = rag_config or settings.agentic_policy_rag_config()

    async def rewrite(
        self,
        *,
        plan: PolicyQueryPlan,
        grade_result: PolicyGradeResult,
        retrieved_policies: list[RetrievedPolicy],
        retrieval_round: int,
        config: RunnableConfig,
    ) -> PolicyRewriterCall:
        model_name_value = config.get("configurable", {}).get(
            "model",
            settings.DEFAULT_MODEL,
        )
        model = get_model(model_name_value)  # type: ignore[arg-type]
        runnable = bind_moderation_structured_output(
            model,
            RewrittenPolicyQuery,
            model_name=model_name_value,
            include_raw=True,
        )
        messages = [
            SystemMessage(content=POLICY_QUERY_REWRITER_SYSTEM_PROMPT),
            HumanMessage(
                content=POLICY_QUERY_REWRITER_TASK_PROMPT.format(
                    query_plan=plan.model_dump_json(),
                    retrieval_round=retrieval_round,
                    retrieved_policies=(
                        "\n".join(
                            policy.model_dump_json()
                            for policy in retrieved_policies
                        )
                        or "None"
                    ),
                    grade_result=grade_result.model_dump_json(),
                )
            ),
        ]
        call_config = _rewriter_config(
            config,
            plan=plan,
            retrieval_round=retrieval_round,
            model_name=str(model_name_value or "unconfigured"),
        )
        async with asyncio.timeout(self.rag_config.agent_timeout_seconds):
            raw = await _invoke_with_one_repair(
                runnable,
                messages,
                call_config,
            )
        return PolicyRewriterCall(
            rewritten=_sanitize_rewrite(
                raw,
                plan=plan,
                max_queries=self.rag_config.max_queries_per_round,
            )
        )


async def _invoke_with_one_repair(
    runnable,
    messages,
    config,
) -> RewrittenPolicyQuery:
    current_messages = list(messages)
    for attempt in range(2):
        response = await runnable.ainvoke(current_messages, config)
        if isinstance(response, RewrittenPolicyQuery):
            return response
        if isinstance(response, dict) and response.get("parsing_error") is None:
            parsed = response.get("parsed")
            if parsed is not None:
                return RewrittenPolicyQuery.model_validate(parsed)
        if attempt == 0:
            current_messages.append(
                SystemMessage(
                    content=(
                        "The previous response did not match RewrittenPolicyQuery. "
                        "Return one corrected structured rewrite without repeating "
                        "the old query."
                    )
                )
            )
    raise ValueError("Policy Query Rewriter structured output could not be repaired")


def policy_query_signature(
    plan: PolicyQueryPlan,
    *,
    policy_version: str,
) -> str:
    payload = {
        "queries": sorted(
            normalize_policy_query(query)
            for query in plan.queries
        ),
        "risk_type_filters": sorted(
            item.value
            for item in plan.risk_type_filters
        ),
        "severity_filters": sorted(
            item.value
            for item in plan.severity_filters
        ),
        "retrieval_mode": plan.retrieval_mode.value,
        "policy_version": policy_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sanitize_rewrite(
    rewrite: RewrittenPolicyQuery,
    *,
    plan: PolicyQueryPlan,
    max_queries: int,
) -> RewrittenPolicyQuery:
    data = redact_data(rewrite.model_dump(mode="python"))
    queries = _unique_queries(
        [redact_text(str(query)) for query in data["queries"]]
    )
    if not queries:
        raise ValueError("Policy Query Rewriter returned no usable query")
    data["queries"] = queries[:max_queries]
    data["risk_type_filters"] = (
        data.get("risk_type_filters") or plan.risk_type_filters
    )
    data["severity_filters"] = (
        data.get("severity_filters") or plan.severity_filters
    )
    return RewrittenPolicyQuery.model_validate(data)


def _unique_queries(queries: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = normalize_policy_query(query)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(query.strip())
    return values


def _rewriter_config(
    config: RunnableConfig,
    *,
    plan: PolicyQueryPlan,
    retrieval_round: int,
    model_name: str,
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "policy_query_rewriter"
    call_config["tags"] = list(
        dict.fromkeys(
            [
                *config.get("tags", []),
                "moderation",
                "policy_rag",
                "rewriter",
                "skip_stream",
            ]
        )
    )
    call_config["metadata"] = {
        "moderation_task_id": config.get("configurable", {}).get(
            "moderation_task_id"
        ),
        "risk_type_filters": [
            risk.value
            for risk in plan.risk_type_filters
        ],
        "retrieval_mode": plan.retrieval_mode.value,
        "retrieval_round": retrieval_round,
        "model_name": model_name,
    }
    return call_config
