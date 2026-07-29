from __future__ import annotations

import asyncio
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.creator.application.harness import CreatorAgentHarness, CreatorHarnessPolicy
from app.creator.domain.errors import (
    CreatorIdempotencyConflictError,
    CreatorRunLeaseConflictError,
    CreatorRuntimeRetryableError,
    CreatorStaleWorkerResultError,
)
from app.creator.domain.models import (
    CancelCreatorTaskCommand,
    CreateCreatorTaskCommand,
    CreatorRunStatus,
    CreatorTaskKind,
    CreatorTaskStatus,
    RetryCreatorTaskCommand,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from app.creator.infrastructure.sqlalchemy import (
    CreatorIdempotencyRow,
    CreatorOutboxRow,
    CreatorRunEventRow,
    CreatorRunRow,
    CreatorTaskRow,
)
from app.creator.infrastructure.database import CreatorDatabase


class MutableClock:
    def __init__(self):
        self.current = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class ScriptedRuntime:
    name = "scripted-test-runtime"

    def __init__(self, *steps):
        self.steps = deque(steps)
        self.requests = []

    async def start(self, request, **kwargs):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("No scripted runtime step remains")
        step = self.steps.popleft()
        if isinstance(step, Exception):
            raise step
        return step


class BlockingRuntime:
    name = "blocking-test-runtime"

    def __init__(self, outcome: RuntimeOutcome):
        self.outcome = outcome
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def start(self, request, **kwargs):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return self.outcome


class RecoverableBlockingRuntime:
    name = "recoverable-blocking-test-runtime"

    def __init__(self, outcome: RuntimeOutcome):
        self.outcome = outcome
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.requests = []

    async def start(self, request, **kwargs):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.first_started.set()
            await self.release_first.wait()
        return self.outcome


class CreatorHarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = CreatorDatabase.from_url("sqlite+aiosqlite:///:memory:")
        await self.database.create_schema_for_development()
        self.sessions = self.database.sessions
        self.uow_factory = self.database.uow_factory
        self.clock = MutableClock()

    async def asyncTearDown(self):
        await self.database.dispose()

    def command(
        self,
        *,
        goal: str = "写一篇 Agent Harness 深度文章",
        idempotency_key: str = "create-1",
    ) -> CreateCreatorTaskCommand:
        return CreateCreatorTaskCommand(
            tenant_id="tenant-a",
            creator_id="creator-7",
            kind=CreatorTaskKind.CREATE_CONTENT,
            goal=goal,
            constraints={"language": "zh-CN"},
            source_scope={"include_creator_history": True},
            idempotency_key=idempotency_key,
        )

    def harness(self, runtime, *, max_runtime_attempts: int = 3):
        return CreatorAgentHarness(
            uow_factory=self.uow_factory,
            runtime=runtime,
            policy=CreatorHarnessPolicy(
                max_runtime_attempts=max_runtime_attempts,
                retry_delay_seconds=0,
                run_lease_seconds=60,
            ),
            clock=self.clock,
        )

    async def test_sqlite_test_database_enforces_foreign_keys(self):
        async with self.database.engine.connect() as connection:
            enabled = await connection.scalar(text("PRAGMA foreign_keys"))

        self.assertEqual(enabled, 1)

    async def test_create_task_is_atomic_and_idempotent(self):
        runtime = ScriptedRuntime()
        harness = self.harness(runtime)

        first = await harness.create_task(self.command())
        replay = await harness.create_task(self.command())

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.task_id, replay.task_id)
        self.assertEqual(first.run_id, replay.run_id)
        self.assertEqual(first.status, CreatorTaskStatus.QUEUED)

        async with self.sessions() as session:
            self.assertEqual(await _count(session, CreatorTaskRow), 1)
            self.assertEqual(await _count(session, CreatorRunRow), 1)
            self.assertEqual(await _count(session, CreatorRunEventRow), 1)
            self.assertEqual(await _count(session, CreatorOutboxRow), 1)
            self.assertEqual(await _count(session, CreatorIdempotencyRow), 1)

        with self.assertRaises(CreatorIdempotencyConflictError):
            await harness.create_task(self.command(goal="相同幂等键不能提交另一篇文章"))

    async def test_successful_runtime_projects_real_terminal_result(self):
        runtime = ScriptedRuntime(
            RuntimeOutcome(
                status=RuntimeOutcomeStatus.COMPLETED,
                checkpoint_id="checkpoint-1",
                final_artifact_id="artifact-final-1",
                events=(
                    RuntimeEvent(
                        type="artifact.created",
                        payload={"artifact_id": "artifact-final-1"},
                    ),
                ),
                state_summary={"plan_revision": 2},
            )
        )
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())

        result = await harness.start_run(created.run_id, worker_id="worker-a")

        self.assertTrue(result.invoked)
        self.assertEqual(result.task_status, CreatorTaskStatus.COMPLETED)
        self.assertEqual(result.run_status, CreatorRunStatus.COMPLETED)
        self.assertEqual(result.final_artifact_id, "artifact-final-1")
        self.assertEqual(runtime.requests[0].run_id, created.run_id)
        self.assertNotEqual(runtime.requests[0].thread_id, created.run_id)

        async with self.uow_factory() as uow:
            task = await uow.tasks.get(created.task_id)
            run = await uow.runs.get(created.run_id)
        self.assertEqual(task.final_artifact_id, "artifact-final-1")
        self.assertEqual(run.checkpoint_id, "checkpoint-1")
        self.assertIsNone(run.lease_owner)

        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorRunEventRow)
                    .where(CreatorRunEventRow.run_id == created.run_id)
                    .order_by(CreatorRunEventRow.sequence)
                )
            ).all()
        self.assertEqual(
            [row.type for row in rows],
            ["task.created", "run.started", "artifact.created", "task.completed"],
        )
        self.assertEqual([row.sequence for row in rows], [1, 2, 3, 4])

    async def test_retryable_runtime_failure_requeues_same_stable_run(self):
        runtime = ScriptedRuntime(
            CreatorRuntimeRetryableError(
                "temporary model outage",
                error_code="MODEL_UNAVAILABLE",
            ),
            RuntimeOutcome(
                status=RuntimeOutcomeStatus.COMPLETED,
                checkpoint_id="checkpoint-after-retry",
                final_artifact_id="artifact-after-retry",
            ),
        )
        harness = self.harness(runtime, max_runtime_attempts=2)
        created = await harness.create_task(self.command())

        first_attempt = await harness.start_run(created.run_id, worker_id="worker-a")
        second_attempt = await harness.start_run(created.run_id, worker_id="worker-b")

        self.assertEqual(first_attempt.task_status, CreatorTaskStatus.RETRYING)
        self.assertEqual(first_attempt.error_code, "MODEL_UNAVAILABLE")
        self.assertEqual(second_attempt.task_status, CreatorTaskStatus.COMPLETED)
        self.assertEqual(len(runtime.requests), 2)
        self.assertEqual(runtime.requests[0].thread_id, runtime.requests[1].thread_id)
        self.assertEqual(runtime.requests[1].execution_attempt, 2)

        async with self.uow_factory() as uow:
            run = await uow.runs.get(created.run_id)
        self.assertEqual(run.execution_attempts, 2)
        self.assertEqual(run.status, CreatorRunStatus.COMPLETED)

        async with self.sessions() as session:
            self.assertEqual(await _count(session, CreatorOutboxRow), 2)

    async def test_active_run_lease_prevents_concurrent_runtime_invocation(self):
        runtime = BlockingRuntime(
            RuntimeOutcome(
                status=RuntimeOutcomeStatus.COMPLETED,
                final_artifact_id="artifact-lease-test",
            )
        )
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())

        first_worker = asyncio.create_task(
            harness.start_run(created.run_id, worker_id="worker-a")
        )
        await runtime.started.wait()

        with self.assertRaises(CreatorRunLeaseConflictError):
            await harness.start_run(created.run_id, worker_id="worker-b")

        runtime.release.set()
        result = await first_worker
        self.assertEqual(result.task_status, CreatorTaskStatus.COMPLETED)
        self.assertEqual(len(runtime.requests), 1)

    async def test_expired_lease_recovery_rejects_stale_worker_result(self):
        runtime = RecoverableBlockingRuntime(
            RuntimeOutcome(
                status=RuntimeOutcomeStatus.COMPLETED,
                checkpoint_id="checkpoint-recovered",
                final_artifact_id="artifact-recovered",
            )
        )
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())
        stale_worker = asyncio.create_task(
            harness.start_run(created.run_id, worker_id="worker-stale")
        )
        await runtime.first_started.wait()

        self.clock.advance(seconds=61)
        recovered = await harness.recover_run(
            created.run_id,
            worker_id="worker-recovery",
        )
        runtime.release_first.set()

        with self.assertRaises(CreatorStaleWorkerResultError):
            await stale_worker
        self.assertEqual(recovered.task_status, CreatorTaskStatus.COMPLETED)
        self.assertEqual(len(runtime.requests), 2)
        self.assertEqual(runtime.requests[0].thread_id, runtime.requests[1].thread_id)

    async def test_invalid_runtime_contract_fails_without_retry_loop(self):
        runtime = ScriptedRuntime({"status": "COMPLETED"})
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())

        result = await harness.start_run(created.run_id, worker_id="worker-a")

        self.assertEqual(result.task_status, CreatorTaskStatus.FAILED)
        self.assertEqual(result.run_status, CreatorRunStatus.FAILED)
        self.assertEqual(result.error_code, "RUNTIME_CONTRACT_ERROR")
        async with self.sessions() as session:
            self.assertEqual(await _count(session, CreatorOutboxRow), 1)

    async def test_queued_task_can_be_cancelled_idempotently(self):
        runtime = ScriptedRuntime()
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())
        command = CancelCreatorTaskCommand(
            tenant_id="tenant-a",
            creator_id="creator-7",
            task_id=created.task_id,
            expected_version=created.version,
        )

        cancelled = await harness.request_cancel(command)
        replay = await harness.request_cancel(command)
        start_result = await harness.start_run(created.run_id, worker_id="worker-a")

        self.assertEqual(cancelled.status, CreatorTaskStatus.CANCELLED)
        self.assertEqual(replay.status, CreatorTaskStatus.CANCELLED)
        self.assertFalse(start_result.invoked)
        self.assertEqual(start_result.run_status, CreatorRunStatus.CANCELLED)
        self.assertEqual(runtime.requests, [])

    async def test_explicit_retry_creates_new_run_and_preserves_failed_run(self):
        runtime = ScriptedRuntime(
            RuntimeOutcome(
                status=RuntimeOutcomeStatus.FAILED,
                error=RuntimeErrorInfo(
                    code="WRITER_FATAL",
                    message="writer configuration is invalid",
                    retryable=False,
                ),
            )
        )
        harness = self.harness(runtime)
        created = await harness.create_task(self.command())
        failed = await harness.start_run(created.run_id, worker_id="worker-a")
        retry_command = RetryCreatorTaskCommand(
            tenant_id="tenant-a",
            creator_id="creator-7",
            task_id=created.task_id,
            expected_version=failed.task_version,
            idempotency_key="retry-1",
        )

        retried = await harness.retry_task(retry_command)
        replay = await harness.retry_task(retry_command)

        self.assertEqual(failed.task_status, CreatorTaskStatus.FAILED)
        self.assertEqual(retried.status, CreatorTaskStatus.QUEUED)
        self.assertNotEqual(retried.run_id, created.run_id)
        self.assertEqual(retried.run_id, replay.run_id)
        self.assertTrue(replay.replayed)

        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorRunRow)
                    .where(CreatorRunRow.task_id == created.task_id)
                    .order_by(CreatorRunRow.attempt)
                )
            ).all()
        self.assertEqual([row.attempt for row in rows], [1, 2])
        self.assertEqual(
            [row.status for row in rows],
            [CreatorRunStatus.FAILED.value, CreatorRunStatus.QUEUED.value],
        )


async def _count(session, row_type) -> int:
    return int(await session.scalar(select(func.count()).select_from(row_type)) or 0)


if __name__ == "__main__":
    unittest.main()
