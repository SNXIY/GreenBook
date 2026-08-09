from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from app.creator.evaluation.deterministic_judge import (
    DeterministicGenerationJudge,
)
from app.creator.evaluation.judge import (
    OpenAICompatibleGenerationJudge,
    OpenAICompatibleJudgeConfig,
)
from app.creator.evaluation.ports import (
    CreatorEvaluationStore,
    CreatorGenerationJudge,
)
from app.creator.evaluation.service import CreatorEvaluationPipeline


class CreatorEvaluationSettings(Protocol):
    creator_evaluation_judge_provider: str
    creator_evaluation_judge_base_url: str
    creator_evaluation_judge_api_key: str
    creator_evaluation_judge_model: str
    creator_evaluation_judge_timeout_seconds: float
    creator_evaluation_judge_max_context_chars: int
    creator_evaluation_judge_max_attempts: int


def build_creator_evaluation_pipeline(
    settings: CreatorEvaluationSettings,
    *,
    store: CreatorEvaluationStore | None = None,
) -> CreatorEvaluationPipeline:
    provider = settings.creator_evaluation_judge_provider.strip().lower()
    judge: CreatorGenerationJudge | None = DeterministicGenerationJudge()
    if provider in {"disabled", "none"}:
        judge = None
    elif provider not in {"", "deterministic"}:
        if provider != "openai":
            raise ValueError(
                f"Unsupported Creator evaluation judge provider: {provider}"
            )
        if not (
            settings.creator_evaluation_judge_base_url.strip()
            and settings.creator_evaluation_judge_api_key.strip()
            and settings.creator_evaluation_judge_model.strip()
        ):
            raise ValueError(
                "OpenAI-compatible evaluation judge requires base URL, API key, "
                "and model"
            )
        judge = OpenAICompatibleGenerationJudge(
            OpenAICompatibleJudgeConfig(
                base_url=settings.creator_evaluation_judge_base_url,
                api_key=SecretStr(settings.creator_evaluation_judge_api_key),
                model=settings.creator_evaluation_judge_model,
                timeout_seconds=settings.creator_evaluation_judge_timeout_seconds,
                max_context_chars=settings.creator_evaluation_judge_max_context_chars,
                max_attempts=settings.creator_evaluation_judge_max_attempts,
            )
        )
    return CreatorEvaluationPipeline(store=store, judge=judge)
