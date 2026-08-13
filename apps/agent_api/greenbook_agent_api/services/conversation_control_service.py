"""Conversation command adapter for existing durable Execution controls."""

from __future__ import annotations

from typing import Any

from greenbook_agent_core.command import TargetCandidate
from greenbook_agent_core.conversation import (
    ExecutionControlCommand,
    ExecutionControlType,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueStatus
from greenbook_agent_core.execution.models import StepStatus

from ..models.runtime_result import RuntimeResult


class ConversationControlService:
    """Execute typed conversation controls through existing Runtime services."""

    def __init__(
        self,
        *,
        runtime_manager: Any,
        retry_manager: Any,
        execution_queue: Any | None = None,
    ) -> None:
        self._runtime = runtime_manager
        self._retry = retry_manager
        self._queue = execution_queue

    async def execute(
        self,
        command: ExecutionControlCommand,
        target: TargetCandidate,
        *,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        execution_id = target.execution_id
        if not execution_id:
            raise ValueError("Execution control requires a resolved execution_id")

        if command.command == ExecutionControlType.PAUSE_EXECUTION:
            execution = self._runtime.pause_execution(
                execution_id,
                reason="Conversation command requested pause",
            )
            return self._result(
                execution,
                run_id=run_id,
                trace_id=trace_id,
                content="任务正在安全暂停，将在当前步骤边界保存进度。",
            )

        if command.command == ExecutionControlType.RESUME_EXECUTION:
            execution = self._runtime.resume_execution(execution_id)
            self._requeue(execution_id)
            return self._result(
                execution,
                run_id=run_id,
                trace_id=trace_id,
                content="任务已恢复，将从保存的检查点继续执行。",
            )

        if command.command == ExecutionControlType.RETRY_EXECUTION:
            execution = self._runtime.get_execution(execution_id)
            failed = next(
                (
                    step
                    for step in reversed(execution.steps)
                    if step.status in {StepStatus.FAILED_RETRYABLE, StepStatus.FAILED}
                ),
                None,
            )
            if failed is None:
                raise ValueError("Execution has no failed step to retry")
            self._retry.retry_step(execution_id, failed.step_id)
            self._requeue(execution_id)
            execution = self._runtime.get_execution(execution_id)
            return self._result(
                execution,
                run_id=run_id,
                trace_id=trace_id,
                content="失败步骤已重新进入执行队列，已完成步骤不会重复执行。",
            )

        raise ValueError(f"Unsupported execution control: {command.command}")

    def _requeue(self, execution_id: str) -> None:
        if self._queue is None:
            return
        message = self._queue.get_by_execution_id(execution_id)
        if message is None:
            raise ValueError("Execution has no durable dispatch message")
        queued = self._queue.enqueue(
            execution_id,
            trace_id=message.trace_id,
            payload=message.payload,
            requeue=True,
        )
        if queued.status != ExecutionQueueStatus.READY:
            raise ValueError(f"Execution could not enter queue: {queued.status.value}")

    @staticmethod
    def _result(
        execution: Any,
        *,
        run_id: str,
        trace_id: str,
        content: str,
    ) -> RuntimeResult:
        status = str(getattr(execution.status, "value", execution.status))
        return RuntimeResult(
            success=True,
            status=status,
            run_id=run_id,
            task_id=str(execution.task_id),
            execution_id=str(execution.execution_id),
            trace_id=trace_id,
            content=content,
            summary=content,
            execution_path="runtime",
            partial_results={"control_command": True},
        )


__all__ = ["ConversationControlService"]
