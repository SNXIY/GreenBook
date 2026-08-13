"""Synchronous SQLAlchemy repository for PostgreSQL-backed executions."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from .models import ArtifactHandle, PlanExecution, StepExecution
from .persistence import (
    execution_controls,
    execution_metadata,
    execution_steps,
    executions,
)


class PostgresExecutionRepository:
    """Persist PlanExecution and StepExecution without changing their models.

    The adapter accepts a synchronous SQLAlchemy Engine or Connection. The
    application can choose its PostgreSQL driver; tests can use SQLite with
    the same repository contract.
    """

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def save(self, execution: PlanExecution) -> PlanExecution:
        values = _execution_values(execution)
        with self._connect() as conn:
            exists = conn.execute(
                sa.select(executions.c.execution_id).where(
                    executions.c.execution_id == execution.execution_id
                )
            ).first()
            if exists:
                conn.execute(
                    sa.update(executions)
                    .where(executions.c.execution_id == execution.execution_id)
                    .values(**values)
                )
                conn.execute(
                    sa.delete(execution_steps).where(
                        execution_steps.c.execution_id == execution.execution_id
                    )
                )
            else:
                conn.execute(sa.insert(executions).values(**values))
            control_values = _control_values(execution)
            control_exists = conn.execute(
                sa.select(execution_controls.c.execution_id).where(
                    execution_controls.c.execution_id == execution.execution_id
                )
            ).first()
            if control_exists:
                conn.execute(
                    sa.update(execution_controls)
                    .where(execution_controls.c.execution_id == execution.execution_id)
                    .values(**control_values)
                )
            else:
                conn.execute(sa.insert(execution_controls).values(**control_values))
            if execution.steps:
                conn.execute(sa.insert(execution_steps).values([
                    _step_values(step, execution.execution_id)
                    for step in execution.steps
                ]))
        return execution.model_copy(deep=True)

    save_execution = save

    def find_by_id(self, execution_id: str) -> PlanExecution | None:
        with self._connect() as conn:
            execution_row = conn.execute(
                sa.select(executions).where(executions.c.execution_id == execution_id)
            ).mappings().first()
            if execution_row is None:
                return None
            control_row = conn.execute(
                sa.select(execution_controls).where(
                    execution_controls.c.execution_id == execution_id
                )
            ).mappings().first()
            step_rows = conn.execute(
                sa.select(execution_steps)
                .where(execution_steps.c.execution_id == execution_id)
                .order_by(execution_steps.c.ordinal)
            ).mappings().all()
        return _to_execution(execution_row, step_rows, control_row)

    def find_by_task_id(self, task_id: str) -> PlanExecution | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(executions.c.execution_id)
                .where(executions.c.task_id == task_id)
                .order_by(executions.c.created_at)
            ).first()
        return self.find_by_id(row[0]) if row else None

    def update_execution(self, execution: PlanExecution) -> PlanExecution:
        return self.save(execution)

    def delete(self, execution_id: str) -> None:
        with self._connect() as conn:
            conn.execute(sa.delete(execution_steps).where(
                execution_steps.c.execution_id == execution_id
            ))
            conn.execute(sa.delete(executions).where(
                executions.c.execution_id == execution_id
            ))
            conn.execute(sa.delete(execution_controls).where(
                execution_controls.c.execution_id == execution_id
            ))

    def find_step(self, execution_id: str, step_execution_id: str) -> StepExecution | None:
        execution = self.find_by_id(execution_id)
        if execution is None:
            return None
        return next((step.model_copy(deep=True) for step in execution.steps
                     if step.step_execution_id == step_execution_id), None)

    def update_step(self, execution_id: str, step_execution_id: str,
                    **fields: object) -> StepExecution | None:
        step = self.find_step(execution_id, step_execution_id)
        if step is None:
            return None
        for key, value in fields.items():
            setattr(step, key, value)
        step.version += 1
        execution = self.find_by_id(execution_id)
        if execution is None:
            return None
        execution.steps = [step if item.step_execution_id == step_execution_id else item
                           for item in execution.steps]
        self.save(execution)
        return step.model_copy(deep=True)

    def list_all(self) -> list[PlanExecution]:
        with self._connect() as conn:
            ids = conn.execute(sa.select(executions.c.execution_id)
                               .order_by(executions.c.created_at)).all()
        return [execution for row in ids
                if (execution := self.find_by_id(row[0])) is not None]


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


def _execution_values(execution: PlanExecution) -> dict[str, Any]:
    return {
        "execution_id": execution.execution_id,
        "plan_id": execution.plan_id,
        "task_id": execution.task_id,
        "status": execution.status.value,
        "current_step_index": execution.current_step_index,
        "version": execution.version,
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
        "completed_at": execution.completed_at,
        "requires_approval": execution.requires_approval,
        "has_side_effects": execution.has_side_effects,
    }


def _control_values(execution: PlanExecution) -> dict[str, Any]:
    return {
        "execution_id": execution.execution_id,
        "state": execution.control_state.value,
        "reason": execution.control_reason,
        "requested_at": execution.control_requested_at,
        "updated_at": execution.control_updated_at,
    }


def _step_values(step: StepExecution, execution_id: str) -> dict[str, Any]:
    # The deployed execution_step schema predates the newer StepExecution
    # runtime fields. Keep the resolved execution boundary in the durable
    # checkpoint envelope so a separate Worker reconstructs the exact tool,
    # arguments, idempotency and policy instead of falling back to a blank
    # tool name after a process boundary.
    checkpoint_data = dict(step.checkpoint_data or {})
    checkpoint_data.update({
        "_tool_name": step.tool_name,
        "_arguments": dict(step.arguments or {}),
        "_idempotency_key": step.idempotency_key,
        "_execution_mode": step.execution_mode,
        "_policy_snapshot": dict(step.policy_snapshot or {}),
    })
    return {
        "step_execution_id": step.step_execution_id,
        "step_id": step.step_id,
        "execution_id": execution_id,
        "capability": step.capability,
        "ordinal": step.ordinal,
        "status": step.status.value,
        "retry_count": step.retry_count,
        "max_retries": step.max_retries,
        "error_code": step.error_code,
        "error_message": step.error_message,
        "checkpoint_data": checkpoint_data,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
        "version": step.version,
        "input_artifact_types": step.input_artifact_types,
        "output_artifact_type": step.output_artifact_type,
        "depends_on": step.depends_on,
        "input_artifacts": [item.model_dump(mode="json") for item in step.input_artifacts],
        "output_artifact": step.output_artifact.model_dump(mode="json")
        if step.output_artifact else None,
    }


def _to_execution(row: Any, step_rows: list[Any], control_row: Any | None) -> PlanExecution:
    steps = []
    for raw in step_rows:
        data = dict(raw)
        checkpoint_data = dict(data.get("checkpoint_data") or {})
        # Fallbacks preserve compatibility with executions written before the
        # resolved runtime fields were added to the checkpoint envelope.
        data["tool_name"] = str(checkpoint_data.pop("_tool_name", "") or "")
        data["arguments"] = dict(checkpoint_data.pop("_arguments", {}) or {})
        data["idempotency_key"] = str(
            checkpoint_data.pop("_idempotency_key", "") or ""
        )
        data["execution_mode"] = str(
            checkpoint_data.pop("_execution_mode", "QUEUE") or "QUEUE"
        )
        data["policy_snapshot"] = dict(
            checkpoint_data.pop("_policy_snapshot", {}) or {}
        )
        data["checkpoint_data"] = checkpoint_data
        data["status"] = data["status"]
        data["input_artifacts"] = [ArtifactHandle.model_validate(item)
                                    for item in (data.get("input_artifacts") or [])]
        if data.get("output_artifact"):
            data["output_artifact"] = ArtifactHandle.model_validate(data["output_artifact"])
        steps.append(StepExecution.model_validate(data))
    control = dict(control_row) if control_row is not None else {}
    return PlanExecution.model_validate({
        **dict(row),
        "status": row["status"],
        "control_state": control.get("state", "RUNNING"),
        "control_reason": control.get("reason", ""),
        "control_requested_at": control.get("requested_at", ""),
        "control_updated_at": control.get("updated_at", row["updated_at"]),
        "steps": steps,
    })


__all__ = ["PostgresExecutionRepository"]
