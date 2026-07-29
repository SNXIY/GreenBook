from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from agents.moderation.prompts import CLASSIFICATION_PROMPT, JUDGE_PROMPT
from core import get_model, settings
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    ModerationAction,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyEvidence,
    RiskClassification,
    RiskType,
)

from .structured_output import bind_moderation_structured_output


class DecisionDraft(BaseModel):
    risk_type: RiskType
    risk_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: ModerationAction
    reason: str = Field(min_length=1, max_length=2000)
    needs_context_review: bool = False


class LLMModerationModel:
    """Moderation classifier and judge backed only by the configured real model."""

    async def classify(
        self,
        *,
        content: str,
        content_type: ModerationContentType,
        context: ModerationContextEvidence | None,
        signals: list[ModerationSignalEvidence],
        config: RunnableConfig,
    ) -> RiskClassification:
        model_name = config["configurable"].get("model", settings.DEFAULT_MODEL)
        model = get_model(model_name)
        runnable = bind_moderation_structured_output(
            model,
            RiskClassification,
            model_name=model_name,
        ).with_config(tags=["moderation", "classification", "skip_stream"])
        result = await runnable.ainvoke(
            [
                SystemMessage(
                    content="Return only the requested structured classification."
                ),
                HumanMessage(
                    content=CLASSIFICATION_PROMPT.format(
                        content=content,
                        content_type=content_type.value,
                        context=context.model_dump_json() if context else "None",
                        signals=(
                            "\n".join(item.model_dump_json() for item in signals)
                            or "None"
                        ),
                    )
                ),
            ],
            config,
        )
        return RiskClassification.model_validate(result)

    async def decide(
        self,
        *,
        content: str,
        content_type: ModerationContentType,
        classification: RiskClassification,
        policies: list[PolicyEvidence],
        cases: list[CaseEvidence],
        context: ModerationContextEvidence | None,
        signals: list[ModerationSignalEvidence],
        evidence_summary: dict[str, Any] | None,
        config: RunnableConfig,
    ) -> AgentDecision:
        model_name = config["configurable"].get("model", settings.DEFAULT_MODEL)
        model = get_model(model_name)
        runnable = bind_moderation_structured_output(
            model,
            DecisionDraft,
            model_name=model_name,
        ).with_config(tags=["moderation", "judge", "skip_stream"])
        draft = DecisionDraft.model_validate(
            await runnable.ainvoke(
                [
                    SystemMessage(
                        content="Return only the requested structured decision."
                    ),
                    HumanMessage(
                        content=JUDGE_PROMPT.format(
                            content=content,
                            content_type=content_type.value,
                            classification=classification.model_dump_json(),
                            policies=(
                                "\n".join(p.model_dump_json() for p in policies)
                                or "None"
                            ),
                            cases=(
                                "\n".join(c.model_dump_json() for c in cases)
                                or "None"
                            ),
                            context=(
                                context.model_dump_json() if context else "None"
                            ),
                            signals=(
                                "\n".join(s.model_dump_json() for s in signals)
                                or "None"
                            ),
                            evidence_summary=(
                                str(evidence_summary) if evidence_summary else "None"
                            ),
                        )
                    ),
                ],
                config,
            )
        )
        needs_context_review = draft.needs_context_review or bool(
            content_type == ModerationContentType.COMMENT
            and context is not None
            and not context.complete
            and classification.risk_type != RiskType.NORMAL
        )
        return AgentDecision(
            **draft.model_dump(exclude={"needs_context_review"}),
            matched_policies=policies,
            similar_cases=cases,
            signals=signals,
            context_evidence=context,
            source_evidence=_source_evidence(context),
            needs_context_review=needs_context_review,
            evidence_complete=False,
        )


def _source_evidence(context: ModerationContextEvidence | None) -> list[str]:
    if context is None:
        return []
    evidence: list[str] = []
    if context.current is not None:
        evidence.append(f"Current content: {context.current.content}")
    if context.parent_comment is not None:
        evidence.append(f"Parent comment: {context.parent_comment.content}")
    evidence.extend(
        f"Conversation: {item.content}"
        for item in context.conversation_context[-5:]
    )
    return evidence[:20]
