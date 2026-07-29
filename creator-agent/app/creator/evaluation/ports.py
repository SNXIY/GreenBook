from __future__ import annotations

from typing import Protocol

from app.creator.evaluation.models import (
    CreatorEvaluationObservation,
    EvaluationCase,
    EvaluationExecutionResult,
    EvaluationRunReport,
    EvaluationSnapshotRequest,
    GenerationJudgeAssessment,
    RuntimeEvaluationSummary,
)
from app.creator.runtime.models import AgentExecutionContext


class CreatorEvaluationStore(Protocol):
    backend_name: str

    async def save(self, report: EvaluationRunReport) -> EvaluationExecutionResult: ...

    async def get(self, evaluation_run_id: str) -> EvaluationRunReport | None: ...

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[EvaluationRunReport, ...]: ...


class CreatorEvaluationSnapshotReader(Protocol):
    backend_name: str

    async def capture(
        self,
        request: EvaluationSnapshotRequest,
    ) -> CreatorEvaluationObservation: ...


class CreatorGenerationJudge(Protocol):
    name: str
    version: str

    async def assess(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> GenerationJudgeAssessment: ...


class CreatorRuntimeEvaluator(Protocol):
    async def evaluate(
        self,
        context: AgentExecutionContext,
    ) -> RuntimeEvaluationSummary: ...
