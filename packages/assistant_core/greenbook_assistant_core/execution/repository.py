"""ExecutionRepository — in-memory persistence for PlanExecution.

Phase 3.3: in-memory store.  Phase 4+ will migrate to PostgreSQL via
the existing db/repositories pattern.
"""

from __future__ import annotations

from .models import PlanExecution, StepExecution

# ── in-memory store ──────────────────────────────────────────────────

_store: dict[str, PlanExecution] = {}


class ExecutionRepository:
    """CRUD for PlanExecution and its StepExecutions."""

    # ── PlanExecution ────────────────────────────────────────────

    def save(self, execution: PlanExecution) -> PlanExecution:
        _store[execution.execution_id] = execution
        return execution

    save_execution = save

    def update_execution(self, execution: PlanExecution) -> PlanExecution:
        return self.save(execution)

    def find_by_id(self, execution_id: str) -> PlanExecution | None:
        ex = _store.get(execution_id)
        return ex.model_copy(deep=True) if ex else None

    def find_by_task_id(self, task_id: str) -> PlanExecution | None:
        for ex in _store.values():
            if ex.task_id == task_id:
                return ex.model_copy(deep=True)
        return None

    def delete(self, execution_id: str) -> None:
        _store.pop(execution_id, None)

    # ── StepExecution ────────────────────────────────────────────

    def find_step(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution | None:
        ex = _store.get(execution_id)
        if ex is None:
            return None
        for s in ex.steps:
            if s.step_execution_id == step_execution_id:
                return s.model_copy(deep=True)
        return None

    def update_step(
        self,
        execution_id: str,
        step_execution_id: str,
        **fields: object,
    ) -> StepExecution | None:
        ex = _store.get(execution_id)
        if ex is None:
            return None
        for s in ex.steps:
            if s.step_execution_id == step_execution_id:
                for k, v in fields.items():
                    setattr(s, k, v)
                s.version += 1
                return s.model_copy(deep=True)
        return None

    def list_all(self) -> list[PlanExecution]:
        return [v.model_copy(deep=True) for v in _store.values()]

    @staticmethod
    def clear() -> None:
        _store.clear()
