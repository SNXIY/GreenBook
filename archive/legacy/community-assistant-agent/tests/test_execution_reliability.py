from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.artifacts import publish_step_artifact
from app.database import (
    Artifact,
    ConversationGoal,
    Run,
    RunStep,
    SideEffect,
    ToolExecutionReceipt,
)
from app.domain import TargetContext
from app.tools import tool_registry
from app.worker import AgentWorker, TransientToolError


def test_next_target_binding_version_ignores_artifact_lineage() -> None:
    """CONTENT bind after SCHEDULE must not reuse Artifact.version as binding version."""

    assert AgentWorker._next_target_binding_version([]) == 1
    assert (
        AgentWorker._next_target_binding_version(
            [
                SimpleNamespace(version=1),  # CONTENT draft artifact lineage v1
                SimpleNamespace(version=2),  # SCHEDULE already claimed binding v2
                SimpleNamespace(version=7),
            ]
        )
        == 8
    )


def test_content_plus_schedule_verify_is_serialized() -> None:
    from app.change_compiler import ChangeCompiler
    from app.domain import CommunityIntent, TargetBinding, TargetContext
    from app.turn_plan import Change, TurnPlan

    plan = ChangeCompiler().compile(
        turn_plan=TurnPlan(
            turn_relation="MODIFY",
            changes=[
                Change(
                    role="CONTENT",
                    op="APPEND",
                    payload={"instruction": "加入实战经验", "message": "改内容并五分钟后发布"},
                ),
                Change(
                    role="SCHEDULE",
                    op="UPDATE",
                    payload={"schedule_request": "五分钟之后"},
                ),
            ],
            raw_message="改内容并五分钟后发布",
        ),
        target_context=TargetContext(
            content_target=TargetBinding(
                target_type="DRAFT",
                role="CONTENT",
                target_id="draft-1",
                content_sha256="a" * 64,
            ),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                role="SCHEDULE",
                target_id="sched-1",
            ),
        ),
        intent=CommunityIntent(
            domain="content_edit",
            goal="改内容并改期",
            required_capabilities=[],
            confidence=0.9,
        ),
        client_timezone="Asia/Shanghai",
    )
    assert plan is not None
    schedule_read = next(
        step for step in plan.steps if step.task_id == "read-current-schedule"
    )
    assert "read-current-draft" in schedule_read.depends_on


class _AsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _ReceiptSession:
    def __init__(self, run: Run, step: RunStep) -> None:
        self.run = run
        self.step = step
        self.effect: SideEffect | None = None
        self.receipt: ToolExecutionReceipt | None = None
        self.receipt_add_count = 0

    def begin(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def get(self, model, identity, **kwargs):
        if model is Run and identity == self.run.id:
            return self.run
        return None

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is SideEffect:
            return self.effect
        if entity is RunStep:
            return self.step
        if entity is ToolExecutionReceipt:
            return self.receipt
        return None

    def add(self, value) -> None:
        if isinstance(value, SideEffect):
            value.id = value.id or "effect-1"
            value.attempts = value.attempts or 0
            self.effect = value
        elif isinstance(value, ToolExecutionReceipt):
            value.execution_id = value.execution_id or "execution-1"
            self.receipt = value
            self.receipt_add_count += 1

    async def flush(self) -> None:
        return None


class _ReceiptDatabase:
    def __init__(self, session: _ReceiptSession) -> None:
        self.session = session

    def sessions(self) -> _AsyncContext:
        return _AsyncContext(self.session)


class _ArtifactSession:
    def __init__(self) -> None:
        self.artifact: Artifact | None = None
        self.add_count = 0

    async def scalar(self, statement):
        return self.artifact

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: [])

    def add(self, value) -> None:
        if isinstance(value, Artifact):
            value.id = value.id or "publication-artifact-1"
            self.artifact = value
            self.add_count += 1

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_publish_retry_reuses_receipt_and_creates_one_artifact(monkeypatch) -> None:
    run = Run(id="run-1", lease_owner="worker-1", trace_id="trace-1")
    step = RunStep(
        id="step-1",
        run_id=run.id,
        ordinal=1,
        task_key="publish",
        agent_name="PublishAgent",
        tool_name="publication.publish_now",
        depends_on=[],
    )
    receipt_session = _ReceiptSession(run, step)
    worker = object.__new__(AgentWorker)
    worker.database = _ReceiptDatabase(receipt_session)
    worker.worker_id = "worker-1"
    worker.registry = tool_registry
    worker._consume_budget = AsyncMock()
    worker._dispatch_tool = AsyncMock(
        return_value={"post_id": "draft-1", "status": "published", "replayed": False}
    )
    monkeypatch.setattr("app.worker.append_event", AsyncMock())
    args = {"draft_id": "draft-1", "expected_content_sha256": "a" * 64}

    first = await worker._execute_side_effect(
        run=run,
        tool="publication.publish_now",
        args=args,
        ordinal=1,
        timeout_seconds=30,
    )
    # Model the crash/failure window: remote success and Receipt commit happened,
    # but Step/Artifact/TargetContext state has not yet been saved.
    assert step.status != "COMPLETED"
    second = await worker._execute_side_effect(
        run=run,
        tool="publication.publish_now",
        args=args,
        ordinal=1,
        timeout_seconds=30,
    )

    assert first == second
    worker._dispatch_tool.assert_awaited_once()
    assert receipt_session.receipt is not None
    assert receipt_session.receipt_add_count == 1
    assert receipt_session.receipt.status == "COMPLETED"
    assert receipt_session.receipt.idempotency_key == "assistant-effect-run-1-1"

    artifact_session = _ArtifactSession()
    artifact_a = await publish_step_artifact(
        artifact_session,
        step=step,
        output=second,
        artifact_type="PUBLICATION_RECEIPT",
        change_type="PUBLISH_NOW",
        provenance_key=receipt_session.receipt.idempotency_key,
    )
    artifact_b = await publish_step_artifact(
        artifact_session,
        step=step,
        output=second,
        artifact_type="PUBLICATION_RECEIPT",
        change_type="PUBLISH_NOW",
        provenance_key=receipt_session.receipt.idempotency_key,
    )

    assert artifact_a is artifact_b
    assert artifact_session.add_count == 1
    assert artifact_a.provenance_key == receipt_session.receipt.idempotency_key


@pytest.mark.asyncio
async def test_target_context_cas_rejects_stale_goal_version() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=0))
    )
    goal = ConversationGoal(id="goal-1", version=7, phase="READY")

    with pytest.raises(TransientToolError, match="CAS conflict"):
        await AgentWorker._cas_goal_target_context(
            session=session,
            goal=goal,
            target_context=TargetContext(),
            active_target_ref=None,
            phase="READY",
        )

    assert goal.version == 7
