import json
import logging
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.prompts import (
    MODERATION_TOOL_AGENT_SYSTEM_PROMPT,
    MODERATION_TOOL_AGENT_TASK_PROMPT,
)
from agents.moderation.state import ModerationState
from agents.moderation.tools import build_moderation_tools
from agents.moderation.tools.executor import allowed_tool_names_for_risk
from community.tools import CommunityEvidenceReader
from moderation.schemas import (
    EvidenceCollectionResult,
    ModerationContentType,
    ModerationSignalEvidence,
    ModerationSignalType,
    ReasoningTier,
    RiskClassification,
    RiskType,
    ToolCallingConfig,
)
from moderation.security import redact_data

from .dependencies import ModerationDependencies
from .tool_agent_model import sanitize_tool_agent_message

logger = logging.getLogger(__name__)
_RESULT_SCHEMA = json.dumps(
    EvidenceCollectionResult.model_json_schema(),
    ensure_ascii=False,
    separators=(",", ":"),
)


class ModerationToolAgentNodes:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies

    async def select_evidence_strategy(self, state: ModerationState) -> ModerationState:
        del state
        reader_available = isinstance(
            self.dependencies.context_loader,
            CommunityEvidenceReader,
        )
        tool_agent_available = bool(
            self.dependencies.tool_calling_config.enabled
            and self.dependencies.tool_agent is not None
            and reader_available
        )
        return {
            "use_dynamic_tool_agent": tool_agent_available,
            "cascade_tool_agent_available": tool_agent_available,
            "adaptive_cascade_enabled": self.dependencies.adaptive_cascade_enabled,
            "cascade_context_prefetched": False,
            "tool_agent_fallback_used": False,
            "tool_agent_error": None,
        }

    async def select_reasoning_tier(self, state: ModerationState) -> ModerationState:
        if not self.dependencies.adaptive_cascade_enabled:
            tier = (
                ReasoningTier.FAST
                if self._eligible_for_low_risk_fast_path(state)
                else ReasoningTier.LEGACY
            )
            return {
                "reasoning_tier": tier.value,
                "cascade_reasons": ["Adaptive cascade is disabled; legacy routing is preserved."],
            }

        if self._eligible_for_low_risk_fast_path(state):
            return {
                "reasoning_tier": ReasoningTier.FAST.value,
                "cascade_reasons": [
                    "Normal classification passed conservative score and confidence gates.",
                    "No contextual, report, or deterministic risk signal requires deeper review.",
                ],
            }

        reasons = self._deep_reasoning_reasons(state)
        tier = ReasoningTier.DEEP if reasons else ReasoningTier.STANDARD
        if not reasons:
            reasons = [
                "The item is sufficiently clear for fixed evidence retrieval and a single Judge."
            ]
        return {
            "reasoning_tier": tier.value,
            "cascade_reasons": reasons,
        }

    async def initialize(self, state: ModerationState) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        return {
            "use_dynamic_tool_agent": bool(
                self.dependencies.tool_calling_config.enabled
                and self.dependencies.tool_agent is not None
            ),
            "messages": [],
            "risk_hypotheses": [classification.risk_type.value],
            "evidence_gaps": _initial_evidence_gaps(state, classification),
            "tool_results": [],
            "called_tools": [],
            "failed_tools": [],
            "tool_call_cache": {},
            "tool_call_count": 0,
            "tool_call_round": 0,
            "tool_cache_hits": 0,
            "tool_budget_exceeded": False,
            "evidence_collection_complete": False,
            "evidence_summary": None,
            "tool_agent_error": None,
            "tool_agent_fallback_used": False,
            "tool_agent_metrics": {},
        }

    async def prepare_low_risk_fast_path(self, state: ModerationState) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        return {
            "use_dynamic_tool_agent": False,
            "low_risk_fast_path_used": True,
            "risk_hypotheses": [classification.risk_type.value],
            "evidence_gaps": [],
            "tool_results": [],
            "called_tools": [],
            "failed_tools": [],
            "tool_call_cache": {},
            "tool_call_count": 0,
            "tool_call_round": 0,
            "tool_cache_hits": 0,
            "tool_budget_exceeded": False,
            "evidence_collection_complete": False,
            "tool_agent_error": None,
            "tool_agent_fallback_used": False,
            "tool_agent_metrics": {},
        }

    async def moderation_tool_agent(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        round_number = state.get("tool_call_round", 0) + 1
        agent = self.dependencies.tool_agent
        if agent is None:
            return _error_state(
                round_number,
                "moderation_tool_agent:NotConfigured",
            )

        community_reader = self.dependencies.context_loader
        if not isinstance(community_reader, CommunityEvidenceReader):
            return _error_state(
                round_number,
                "moderation_tool_agent:CommunityEvidenceReaderUnavailable",
            )

        tools = build_moderation_tools(
            community_reader=community_reader,
            policy_retriever=self.dependencies.policy_retriever,
            case_retriever=self.dependencies.case_retriever,
            platform=state.get("platform", "default"),
            config=self.dependencies.tool_calling_config,
        )
        allowed_tools = allowed_tool_names_for_risk(
            RiskClassification.model_validate(state["classification"]).risk_type
        )
        tools = [tool for tool in tools if tool.name in allowed_tools]
        try:
            call = await agent.invoke(
                messages=build_tool_agent_messages(
                    state,
                    self.dependencies.tool_calling_config,
                ),
                tools=tools,
                state=state,
                config=config,
            )
            message = sanitize_tool_agent_message(call.message)
            return {
                "messages": [message],
                "tool_call_round": round_number,
                "tool_agent_error": None,
                "tool_agent_metrics": call.metrics.model_dump(mode="json"),
            }
        except Exception as exc:
            logger.exception("Moderation Tool Agent invocation failed")
            error_code = getattr(
                exc,
                "code",
                f"moderation_tool_agent:{type(exc).__name__}",
            )
            metrics = getattr(exc, "metrics", None)
            return _error_state(
                round_number,
                str(error_code),
                metrics.model_dump(mode="json") if metrics is not None else {},
            )

    async def prepare_fixed_fallback(self, state: ModerationState) -> ModerationState:
        return {
            "use_dynamic_tool_agent": False,
            "tool_agent_fallback_used": True,
            "evidence_collection_complete": False,
            "evidence_gaps": list(
                dict.fromkeys(
                    [
                        *state.get("evidence_gaps", []),
                        "Dynamic evidence collection failed; the fixed path was used.",
                    ]
                )
            ),
        }

    async def mark_budget_exceeded(self, state: ModerationState) -> ModerationState:
        del state
        return {
            "tool_budget_exceeded": True,
            "evidence_collection_complete": False,
        }

    def evidence_strategy_route(
        self,
        state: ModerationState,
    ) -> Literal["adaptive", "dynamic", "fixed"]:
        if state.get("adaptive_cascade_enabled", False):
            return "adaptive"
        return "dynamic" if state.get("use_dynamic_tool_agent", False) else "fixed"

    def reasoning_tier_route(
        self,
        state: ModerationState,
    ) -> Literal["dynamic", "fixed", "fast"]:
        tier = ReasoningTier(state.get("reasoning_tier", ReasoningTier.LEGACY.value))
        if tier == ReasoningTier.FAST:
            return "fast"
        if tier == ReasoningTier.STANDARD:
            return "fixed"
        if tier == ReasoningTier.DEEP:
            # Prefer fixed evidence for ambiguous scores; reserve dynamic tools for
            # missing context, reports, or non-trivial deterministic signals.
            if self._needs_dynamic_tools(state) and state.get("use_dynamic_tool_agent", False):
                return "dynamic"
            return "fixed"
        return "dynamic" if state.get("use_dynamic_tool_agent", False) else "fixed"

    def post_classification_evidence_route(
        self,
        state: ModerationState,
    ) -> Literal["dynamic", "fixed", "fast"]:
        route = self.reasoning_tier_route(state)
        return "dynamic" if route == "adaptive" else route

    def route_after_fast_path_prepare(
        self,
        state: ModerationState,
    ) -> Literal["direct", "evidence"]:
        if state.get("adaptive_cascade_enabled", False):
            return "direct"
        return "evidence"

    def _eligible_for_low_risk_fast_path(self, state: ModerationState) -> bool:
        if not self.dependencies.low_risk_fast_path_enabled:
            return False
        classification = RiskClassification.model_validate(state["classification"])
        thresholds = self.dependencies.thresholds
        content_type = ModerationContentType(state.get("content_type", "TEXT"))
        metadata = state.get("metadata", {})
        return bool(
            classification.risk_type == RiskType.NORMAL
            and classification.risk_score <= thresholds.pass_score_max
            and classification.confidence >= thresholds.auto_pass_confidence_min
            and not classification.fallback_used
            and not state.get("signals")
            and content_type != ModerationContentType.COMMENT
            and metadata.get("review_trigger") != "REPORT"
        )

    def _needs_dynamic_tools(self, state: ModerationState) -> bool:
        metadata = state.get("metadata", {})
        context = state.get("context_evidence")
        if metadata.get("review_trigger") == "REPORT":
            return True
        if isinstance(context, dict) and context.get("complete") is False:
            return True
        signal_types = {
            ModerationSignalEvidence.model_validate(value).signal_type
            for value in state.get("signals", [])
        }
        return bool(signal_types - {ModerationSignalType.TEXT_PATTERN})

    def _deep_reasoning_reasons(self, state: ModerationState) -> list[str]:
        classification = RiskClassification.model_validate(state["classification"])
        reasons: list[str] = []

        # Comments and reports no longer force DEEP by themselves; STANDARD fixed
        # evidence already loads parent/conversation/report context.
        if classification.fallback_used:
            reasons.append("The primary classifier failed and a fallback result was used.")
        signal_types = {
            ModerationSignalEvidence.model_validate(value).signal_type
            for value in state.get("signals", [])
        }
        if signal_types - {ModerationSignalType.TEXT_PATTERN}:
            reasons.append("Deterministic or community risk signals require corroboration.")
        context = state.get("context_evidence")
        if isinstance(context, dict) and context.get("complete") is False:
            reasons.append("Required community context is incomplete.")
        if classification.confidence < self.dependencies.thresholds.adversarial_confidence_min:
            reasons.append("Classifier confidence is below the deep-review threshold.")
        if (
            self.dependencies.thresholds.adversarial_score_min
            <= classification.risk_score
            <= self.dependencies.thresholds.adversarial_score_max
        ):
            reasons.append("Risk score falls inside the configured ambiguity band.")
        if (
            classification.risk_type == RiskType.NORMAL
            and classification.risk_score > self.dependencies.thresholds.pass_score_max
        ):
            reasons.append("A nominally normal classification is outside the automatic-pass band.")
        return list(dict.fromkeys(reasons))[:20]

    def route_after_tool_agent(
        self,
        state: ModerationState,
    ) -> Literal["tools", "finalize", "fallback", "budget"]:
        if state.get("tool_agent_error"):
            return "fallback"
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return "fallback"
        if not messages[-1].tool_calls:
            return "finalize"
        if (
            state.get("tool_call_round", 0) >= self.dependencies.tool_calling_config.max_rounds
            or state.get("tool_call_count", 0)
            >= self.dependencies.tool_calling_config.max_total_calls
        ):
            return "budget"
        return "tools"

    def route_after_finalize(
        self,
        state: ModerationState,
    ) -> Literal["review", "fallback", "reviewer_policy"]:
        if state.get("tool_agent_error") and not state.get("tool_agent_fallback_used"):
            return "fallback"
        if state.get("revision_source") == "COLLECT_MORE_EVIDENCE":
            return "reviewer_policy"
        return "review"


def build_tool_agent_messages(
    state: ModerationState,
    tool_config: ToolCallingConfig,
) -> list[AnyMessage]:
    classification = RiskClassification.model_validate(state["classification"])
    signals = [
        ModerationSignalEvidence.model_validate(signal).model_dump(mode="json")
        for signal in state.get("signals", [])
    ]
    prompt = MODERATION_TOOL_AGENT_TASK_PROMPT.format(
        content=state["normalized_content"],
        content_type=state.get("content_type", ModerationContentType.TEXT.value),
        content_id=state.get("content_id") or "Unavailable",
        author_id=state.get("creator_id") or "Unavailable",
        platform=state.get("platform", "default"),
        classification=classification.model_dump_json(),
        signals=json.dumps(redact_data(signals), ensure_ascii=False) if signals else "None",
        risk_hypotheses=json.dumps(state.get("risk_hypotheses", []), ensure_ascii=False),
        evidence_gaps=json.dumps(state.get("evidence_gaps", []), ensure_ascii=False),
        max_rounds=tool_config.max_rounds,
        max_total_calls=tool_config.max_total_calls,
        max_parallel_calls=tool_config.max_parallel_calls,
        current_round=state.get("tool_call_round", 0),
        current_calls=state.get("tool_call_count", 0),
        result_schema=_RESULT_SCHEMA,
    )
    return [
        SystemMessage(content=MODERATION_TOOL_AGENT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
        *state.get("messages", []),
    ]


def _initial_evidence_gaps(
    state: ModerationState,
    classification: RiskClassification,
) -> list[str]:
    gaps: list[str] = []
    content_type = ModerationContentType(state.get("content_type", "TEXT"))
    if classification.risk_type != RiskType.NORMAL:
        gaps.append("No applicable current platform policy has been collected yet.")
    if classification.risk_type == RiskType.ADVERTISING:
        gaps.append(
            "Commercial intent, contact channel, or repeated solicitation is not confirmed."
        )
    elif classification.risk_type == RiskType.ABUSE:
        gaps.append("The attack target and contextual meaning are not fully confirmed.")
    elif classification.risk_type == RiskType.PRIVACY:
        gaps.append("The sensitive-information type and authorization context are not confirmed.")
    if content_type == ModerationContentType.COMMENT:
        gaps.append("Parent-comment or conversation context has not been collected dynamically.")
    return gaps


def _error_state(
    round_number: int,
    error_code: str,
    metrics: dict | None = None,
) -> ModerationState:
    return {
        "tool_call_round": round_number,
        "tool_agent_error": error_code[:500],
        "tool_agent_metrics": metrics or {},
        "evidence_collection_complete": False,
    }
