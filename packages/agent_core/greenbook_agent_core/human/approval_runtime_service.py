"""Durable approval coordination for Runtime and conversation commands."""

from __future__ import annotations

import inspect
from typing import Any

from greenbook_agent_core.command import TargetCandidate
from greenbook_agent_core.context import PendingApproval
from greenbook_agent_core.conversation import (
    ConversationNotFoundError,
    ExecutionControlCommand,
    ExecutionControlType,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueStatus
from greenbook_agent_core.execution.models import StepStatus
from greenbook_agent_core.human import (
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalRequestStore,
    ApprovalTransitionConflictError,
)

from ..execution.runtime_result import RuntimeResult


class ApprovalRuntimeService:
    """Persist approval requests and resume the existing execution checkpoint."""

    def __init__(
        self,
        *,
        store: ApprovalRequestStore,
        runtime_manager: Any,
        state_manager: Any,
        execution_queue: Any | None,
        conversation_service: Any | None = None,
        direct_resume: Any | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime_manager
        self._state = state_manager
        self._queue = execution_queue
        self._context = conversation_service
        self._direct_resume = direct_resume

    async def capture_result(
        self,
        result: RuntimeResult,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> ApprovalRequest | None:
        if result.status not in {"WAITING_APPROVAL", "WAITING_HUMAN"}:
            return None
        data = result.approval_data or result.approval or {}
        approval_id = str(result.approval_id or data.get("approval_id") or "")
        execution_id = str(result.execution_id or data.get("execution_id") or "")
        if not approval_id or not execution_id:
            return None
        existing = await self._store.find_by_id(approval_id)
        if existing is not None:
            return existing
        request = ApprovalRequest(
            approval_id=approval_id,
            execution_id=execution_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message=str(
                data.get("message")
                or data.get("description")
                or "内容已经准备好，是否继续执行需要审批的操作？"
            ),
            operation=str(data.get("operation") or "RUNTIME_APPROVAL"),
            resource_id=data.get("resource_id"),
            run_id=result.run_id or None,
            payload=dict(data.get("payload") or {}),
        )
        await self._store.save(request)
        await self._set_pending_context(request)
        return request

    async def get_request(self, approval_id: str) -> ApprovalRequest | None:
        return await self._store.find_by_id(approval_id)

    async def get_for_execution(self, execution_id: str) -> ApprovalRequest | None:
        return await self._store.find_by_execution(execution_id)

    async def reconcile_execution(
        self,
        message: Any,
        execution: Any,
    ) -> ApprovalRequest | None:
        """Restore an approval row for a durable waiting execution.

        A queue worker can acknowledge a message after persisting the
        WAITING_APPROVAL checkpoint but before the projection process writes
        the approval row. On API/Worker restart the checkpoint is the source
        of truth; reconstruct only the approval envelope, never the tool call.
        """
        status = str(
            getattr(getattr(execution, "status", ""), "value", execution.status)
        )
        if status not in {"WAITING_APPROVAL", "RUNNING"}:
            return None
        execution_id = str(getattr(execution, "execution_id", "") or "")
        if not execution_id:
            return None
        existing = await self._store.find_by_execution(execution_id)
        if existing is not None:
            queue_message = self._queue_message(execution_id)
            queue_status = getattr(queue_message, "status", None)
            queue_status = getattr(queue_status, "value", queue_status)
            execution_status = str(
                getattr(
                    getattr(execution, "status", ""),
                    "value",
                    getattr(execution, "status", ""),
                )
            )
            if (
                existing.status == ApprovalRequestStatus.APPROVED
                and queue_message is not None
                and queue_status in {
                    ExecutionQueueStatus.ACKED.value,
                    ExecutionQueueStatus.FAILED.value,
                }
                and execution_status == "RUNNING"
            ):
                running_step = next(
                    (
                        step
                        for step in (getattr(execution, "steps", ()) or ())
                        if step.status == StepStatus.RUNNING
                    ),
                    None,
                )
                if running_step is not None:
                    self._state.recover_step(
                        execution_id,
                        running_step.step_execution_id,
                    )
                self._requeue(execution_id, queue_message)
            return existing

        if status != "WAITING_APPROVAL":
            return None

        waiting = next(
            (
                step
                for step in (getattr(execution, "steps", ()) or ())
                if step.status == StepStatus.WAITING_APPROVAL
            ),
            None,
        )
        if waiting is None:
            return None
        payload = dict(getattr(message, "payload", {}) or {})
        identity = dict(payload.get("auth_context") or {})
        conversation_id = str(payload.get("conversation_id") or "")
        user_id = str(identity.get("user_id") or payload.get("user_id") or "")
        tenant_id = str(identity.get("tenant_id") or payload.get("tenant_id") or "")
        if not conversation_id or not user_id or not tenant_id:
            return None

        tool_name = str(getattr(waiting, "tool_name", "") or "")
        capability = str(getattr(waiting, "capability", "") or "")
        operation = tool_name or capability or "RUNTIME_APPROVAL"
        checkpoint_data = dict(getattr(waiting, "checkpoint_data", {}) or {})
        checkpoint_constraints = checkpoint_data.get("constraints") or {}
        constraints = {
            **dict(getattr(waiting, "arguments", {}) or {}),
            **(
                dict(checkpoint_constraints)
                if isinstance(checkpoint_constraints, dict)
                else {}
            ),
        }
        resource_id = None
        if isinstance(constraints, dict):
            resource_id = constraints.get("draft_id") or constraints.get("resource_id")
        request = ApprovalRequest(
            execution_id=execution_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message="This execution is waiting for explicit user approval.",
            operation=operation,
            resource_id=str(resource_id) if resource_id else None,
            run_id=str(payload.get("run_id") or "") or None,
            payload={
                "reconciled": True,
                "capability": capability,
                "tool_name": tool_name,
                "goal_id": str(getattr(waiting, "goal_id", "") or "") or None,
                "task_id": str(getattr(execution, "task_id", "") or "") or None,
                "step_id": str(getattr(waiting, "step_id", "") or "") or None,
                "target_type": "DRAFT" if resource_id else None,
                "run_at": constraints.get("run_at"),
                "timezone": constraints.get("timezone"),
            },
        )
        await self._store.save(request)
        await self._set_pending_context(request)
        return request

    async def decide(
        self,
        approval_id: str,
        *,
        decision: ApprovalRequestStatus,
        user_id: str,
        tenant_id: str,
    ) -> RuntimeResult:
        request = await self._store.find_by_id(approval_id)
        if request is None:
            raise ValueError("Approval request was not found")
        if request.user_id != user_id or request.tenant_id != tenant_id:
            raise PermissionError("Approval request is outside the authenticated scope")
        if request.status != ApprovalRequestStatus.PENDING:
            raise ValueError(f"Approval request is already {request.status.value}")

        # Atomic PENDING -> decided transition FIRST.  Only the first
        # concurrent decision may pass; a losing writer gets a conflict and
        # must never re-queue / re-run the side effect (double-execution
        # guard).  The state machine resumes the execution only after this
        # durable flip succeeded.
        try:
            await self._store.transition(approval_id, decision)
        except ApprovalTransitionConflictError as exc:
            raise ValueError(
                "Approval request has already been decided"
            ) from exc

        if decision == ApprovalRequestStatus.REJECTED:
            execution = self._state.cancel_execution(
                request.execution_id,
                reason="User rejected approval request",
            )
            status = "CANCELLED"
            content = "已取消需要审批的操作，后续步骤不会继续执行。"
        else:
            execution = self._runtime.get_execution(request.execution_id)
            waiting = next(
                (
                    step
                    for step in execution.steps
                    if step.status == StepStatus.WAITING_APPROVAL
                ),
                None,
            )
            if waiting is None:
                raise ValueError("Execution has no step waiting for approval")

            queue_message = self._queue_message(request.execution_id)
            if queue_message is not None:
                # Queue workers resume from the durable PlanExecution. Mark the
                # waiting step runnable, then publish the existing dispatch
                # message again. Completed steps remain untouched.
                self._state.approve_and_resume(
                    request.execution_id,
                    waiting.step_execution_id,
                )
                self._requeue(request.execution_id, queue_message)
                execution = self._runtime.get_execution(request.execution_id)
            elif self._direct_resume is not None:
                # Direct mode owns an in-process worker/context pair. Let that
                # existing path perform the state transition exactly once.
                resumed = self._direct_resume(approval_id, "ACCEPT")
                if inspect.isawaitable(resumed):
                    resumed = await resumed
                execution = self._runtime.get_execution(request.execution_id)
                if isinstance(resumed, RuntimeResult) and not resumed.success:
                    raise ValueError(
                        resumed.error_message or "Direct approval resume failed"
                    )
            else:
                raise ValueError("Approval execution has no resumable dispatch context")

            status = str(getattr(execution.status, "value", execution.status))
            content = "已确认，任务将从审批检查点继续执行。"

        await self._clear_pending_context(request)
        return RuntimeResult(
            success=True,
            status=status,
            run_id=request.run_id or "",
            task_id=str(execution.task_id),
            execution_id=request.execution_id,
            content=content,
            summary=content,
            execution_path="runtime",
            approval_id=approval_id,
            partial_results={"approval_decision": decision.value},
        )

    async def execute_command(
        self,
        command: ExecutionControlCommand,
        target: TargetCandidate,
        *,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        if not target.resource_id:
            raise ValueError("Approval command requires approval_id")
        decision = (
            ApprovalRequestStatus.APPROVED
            if command.command == ExecutionControlType.APPROVE
            else ApprovalRequestStatus.REJECTED
        )
        result = await self.decide(
            target.resource_id,
            decision=decision,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        result.run_id = result.run_id or run_id
        result.trace_id = trace_id
        return result

    def _queue_message(self, execution_id: str) -> Any | None:
        if self._queue is None:
            return None
        return self._queue.get_by_execution_id(execution_id)

    def _requeue(self, execution_id: str, message: Any) -> None:
        payload = dict(getattr(message, "payload", {}) or {})
        # This internal resume fact is added only after the durable approval
        # decision. It is not accepted from user input or stored in a token.
        payload["approval_granted"] = True
        queued = self._queue.enqueue(
            execution_id,
            trace_id=message.trace_id,
            payload=payload,
            requeue=True,
        )
        if queued.status != ExecutionQueueStatus.READY:
            raise ValueError(
                f"Approved execution could not enter queue: {queued.status.value}"
            )

    async def _set_pending_context(self, request: ApprovalRequest) -> None:
        if self._context is None:
            return
        try:
            snapshot = await self._context.load(
                request.conversation_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
            )
        except ConversationNotFoundError:
            # Historical executions can outlive deleted conversations. The
            # approval row remains durable even when no context can be indexed.
            return
        snapshot.session.pending_approval = PendingApproval(
            approval_id=request.approval_id,
            operation=request.operation,
            resource_id=request.resource_id,
            description=request.message,
        )
        await self._context.save_session(snapshot.session)

    async def _clear_pending_context(self, request: ApprovalRequest) -> None:
        if self._context is None:
            return
        try:
            snapshot = await self._context.load(
                request.conversation_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
            )
        except ConversationNotFoundError:
            return
        if (
            snapshot.session.pending_approval is not None
            and snapshot.session.pending_approval.approval_id == request.approval_id
        ):
            snapshot.session.pending_approval = None
            await self._context.save_session(snapshot.session)


__all__ = ["ApprovalRuntimeService"]
