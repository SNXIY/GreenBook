"""Approval materialization invariant for WRITE capabilities.

When a WRITE capability is gated by requires_approval, the loop must surface a
real durable WAITING_APPROVAL (with a PlanExecution + approval_id) instead of a
fake WAITING_USER with no execution to approve.  Approving resumes the SAME
execution; rejecting terminates it without a side effect.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.task.models import Task, TaskGoal, TaskStatus


class _RecordingStore:
    def _record(self, task: Any, event: str, detail: Any) -> None:
        task.last_action = event

    def _record_resource(self, *args: Any, **kwargs: Any) -> None:
        return None


class _QueueDecisions:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self._queue = list(decisions)
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self._queue.pop(0)


def _task() -> Task:
    return Task(
        task_id="t-approval",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="立即发布",
        status=TaskStatus.RUNNING,
        goals=[TaskGoal(task_id="t-approval", kind="POST", status="PENDING")],
        resource_index=[],
        execution_refs=[],
        artifacts=[],
    )


def _command() -> Command:
    return Command(type=CommandType.MODIFY, goal="立即发布", raw_input="立即发布")


def _request() -> Any:
    return type("Req", (), {
        "run_id": "run-ap", "trace_id": "trace-ap", "conversation_id": "c1",
        "user_id": "u1", "tenant_id": "t1", "session": None, "auth": None,
        "mcp": None, "llm": None, "model": "", "timezone": "Asia/Shanghai",
        "activity_callback": None, "completion_callback": None,
    })()


class _Boundary:
    def record_operation_submitted(self, tool_name: str = "") -> None:
        return None

    def record_result_unknown(self) -> None:
        return None

    def record_read(self) -> None:
        return None


# ── A1 + A4: requires_approval -> WAITING_APPROVAL with execution+approval_id ─


@pytest.mark.asyncio
async def test_a1_requires_approval_surfaces_waiting_approval_with_ids() -> None:
    calls: list[tuple[str, dict]] = []

    async def approval_write(tool_name=None, arguments=None, **kwargs):
        calls.append((tool_name, dict(arguments or {})))
        return {
            "ok": True,
            "status": "WAITING_APPROVAL",
            "execution_id": "exec-approval-1",
            "approval_id": "approval-1",
            "message": "该操作需要用户确认后才能继续。",
        }

    decisions = _QueueDecisions([
        ActionDecision(decision=ActionDecisionType.CALL_TOOL, semantic_action="PUBLISH_NOW"),
    ])
    loop = ActionLoop(
        decision_maker=decisions,
        write_submitter=approval_write,
        task_store=_RecordingStore(),
        max_iterations=4,
    )
    result = await loop.run(_task(), _command(), request=_request(), boundary=_Boundary())
    assert result.status == "WAITING_APPROVAL"
    assert result.execution_id == "exec-approval-1"
    assert result.approval_id == "approval-1"
    # The write was durably submitted exactly once to create the PlanExecution;
    # the Java tool itself is NOT executed until the approval is granted.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a4_requires_approval_never_returns_fake_waiting_user() -> None:
    async def approval_write(tool_name=None, arguments=None, **kwargs):
        return {
            "ok": True,
            "status": "WAITING_APPROVAL",
            "execution_id": "exec-approval-2",
            "approval_id": "approval-2",
        }

    decisions = _QueueDecisions([
        ActionDecision(decision=ActionDecisionType.CALL_TOOL, semantic_action="PUBLISH_NOW"),
    ])
    loop = ActionLoop(
        decision_maker=decisions,
        write_submitter=approval_write,
        task_store=_RecordingStore(),
        max_iterations=4,
    )
    result = await loop.run(_task(), _command(), request=_request(), boundary=_Boundary())
    assert result.status == "WAITING_APPROVAL"
    assert result.status != "WAITING_USER"
    assert decisions.calls == 1, "the model must not be re-asked after an approval pause"


# ── A2 / A3: approval decide resumes the same execution / reject cancels ────


class _FakeStore:
    def __init__(self, request: Any) -> None:
        self._request = request
        self.transitioned: list[Any] = []

    async def find_by_id(self, approval_id: str) -> Any:
        return self._request

    async def transition(self, approval_id: str, decision: Any) -> None:
        self.transitioned.append(decision)
        self._request.status = decision


def _approval_request(execution_id: str = "exec-3") -> Any:
    return SimpleNamespace(
        approval_id="approval-3",
        execution_id=execution_id,
        run_id="run-approval-3",
        user_id="u1",
        tenant_id="t1",
        status="PENDING",
        operation="publication.publish_now",
        resource_id="draft-3",
        message="approve",
        payload={},
    )


@pytest.mark.asyncio
async def test_a2_approve_resumes_same_execution() -> None:
    from greenbook_agent_core.human import ApprovalRequestStatus
    from greenbook_agent_core.human.approval_runtime_service import ApprovalRuntimeService

    request = _approval_request()
    store = _FakeStore(request)
    approved: list[tuple[str, str]] = []
    requeued: list[str] = []

    class _State:
        def approve_and_resume(self, execution_id: str, step_execution_id: str) -> None:
            approved.append((execution_id, step_execution_id))

    class _Runtime:
        def get_execution(self, execution_id: str) -> Any:
            return SimpleNamespace(
                execution_id=execution_id,
                task_id="task-3",
                status="WAITING_APPROVAL",
                steps=[SimpleNamespace(status="WAITING_APPROVAL", step_execution_id="step-3")],
            )

    service = ApprovalRuntimeService(
        store=store,
        runtime_manager=_Runtime(),
        state_manager=_State(),
        execution_queue=None,
    )
    service._queue_message = lambda _eid: SimpleNamespace(execution_id="exec-3")

    def fake_requeue(execution_id: str, queue_message: Any) -> None:
        requeued.append(execution_id)

    service._requeue = fake_requeue
    result = await service.decide(
        "approval-3",
        decision=ApprovalRequestStatus.APPROVED,
        user_id="u1",
        tenant_id="t1",
    )
    # The SAME execution is resumed: its waiting step is marked runnable and its
    # original dispatch message is re-published (no new side-effect execution).
    assert ("exec-3", "step-3") in approved
    assert requeued == ["exec-3"]
    assert result is not None
    assert result.execution_id == "exec-3"


@pytest.mark.asyncio
async def test_a3_reject_cancels_execution_no_side_effect() -> None:
    from greenbook_agent_core.human import ApprovalRequestStatus
    from greenbook_agent_core.human.approval_runtime_service import ApprovalRuntimeService

    request = _approval_request()
    store = _FakeStore(request)
    cancelled: list[str] = []

    class _State:
        def cancel_execution(self, execution_id: str, reason: str = "") -> Any:
            cancelled.append(execution_id)
            return SimpleNamespace(execution_id=execution_id, task_id="task-3", status="CANCELLED")

    service = ApprovalRuntimeService(
        store=store,
        runtime_manager=None,
        state_manager=_State(),
        execution_queue=None,
    )
    result = await service.decide(
        "approval-3",
        decision=ApprovalRequestStatus.REJECTED,
        user_id="u1",
        tenant_id="t1",
    )
    # Reject cancels the durable execution; Java is never called (no requeue).
    assert cancelled == ["exec-3"]
    assert result is not None
    assert result.status == "CANCELLED"
