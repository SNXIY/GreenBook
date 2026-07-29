from __future__ import annotations

import asyncio

from app.creator.evaluation.errors import CreatorEvaluationConflictError
from app.creator.evaluation.models import (
    EvaluationExecutionResult,
    EvaluationRunReport,
)


class InMemoryCreatorEvaluationStore:
    backend_name = "in-memory"

    def __init__(self) -> None:
        self._reports: dict[str, EvaluationRunReport] = {}
        self._lock = asyncio.Lock()

    async def save(
        self,
        report: EvaluationRunReport,
    ) -> EvaluationExecutionResult:
        async with self._lock:
            existing = self._reports.get(report.id)
            if existing is not None:
                if existing.request_sha256 != report.request_sha256:
                    raise CreatorEvaluationConflictError(
                        f"Evaluation run {report.id} already exists",
                        details={"evaluation_run_id": report.id},
                    )
                return EvaluationExecutionResult(report=existing, replayed=True)
            self._reports[report.id] = report
            return EvaluationExecutionResult(report=report)

    async def get(self, evaluation_run_id: str) -> EvaluationRunReport | None:
        async with self._lock:
            return self._reports.get(evaluation_run_id)

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[EvaluationRunReport, ...]:
        async with self._lock:
            return tuple(
                report
                for report in sorted(
                    self._reports.values(),
                    key=lambda item: (item.completed_at, item.id),
                    reverse=True,
                )
                if report.tenant_id == tenant_id
                and any(
                    case.creator_id == creator_id and case.task_id == task_id
                    for case in report.cases
                )
            )
