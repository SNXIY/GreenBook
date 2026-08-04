"""Phase 5 Step 4 — Creator create/revise Runtime + dependency recovery."""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.clients import CapabilityGrant
from app.creator_tools import (
    CreatorToolServices,
    handle_creator_tool,
    register_creator_tool_handlers,
)
from app.database import Conversation, Database, Run, RunStep, SideEffect
from app.side_effect_ledger import SideEffectLedger, stable_hash
from app.tool_dependency import DependencyPending, resume_creator_dependency
from app.tool_runtime import (
    LEGACY_BUILTIN_MIGRATION_BACKLOG,
    MIGRATED_WRITE_TOOLS,
    ToolCredentials,
    ToolErrorCode,
    ToolInvocationContext,
    ToolInvocationStatus,
    ToolRuntime,
)
from app.tools import IdempotencyMode, TransportType, tool_registry
from app.worker import AgentWorker

ROOT = Path(__file__).resolve().parents[1]
CREATOR_ROOT = ROOT.parent / "creator-agent"
SHA = "a" * 64
SHA_B = "b" * 64


def _ctx(**overrides: Any) -> ToolInvocationContext:
    payload = {
        "run_id": "run-creator-1",
        "user_id": "user-1",
        "tenant_id": "zhiguang",
        "conversation_id": "conv-1",
        "request_id": "req-1",
        "operation_key": "assistant-effect-run-creator-1-1",
        "idempotency_key": "assistant-effect-run-creator-1-1",
        "attempt": 1,
    }
    payload.update(overrides)
    return ToolInvocationContext(**payload)


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt-test", trace_id="trace-1")


class FakeCreator:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.get_calls = 0
        self.handoff_calls = 0
        self.tasks: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.key_body: dict[str, str] = {}
        self.fail_submit = False
        self.next_status = "QUEUED"
        self.settings = type("S", (), {"creator_timeout_seconds": 240})()

    async def submit_draft(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls += 1
        if self.fail_submit:
            raise TimeoutError("submit timed out after accept")
        key = str(kwargs["idempotency_key"])
        body = stable_hash(
            {
                "instruction": kwargs.get("instruction"),
                "references": kwargs.get("references"),
            }
        )
        if key in self.by_key:
            if self.key_body[key] != body:
                raise RuntimeError("idempotency conflict")
            task_id = self.by_key[key]
            return {"task_id": task_id, "status": self.tasks[task_id]["status"]}
        task_id = f"task-{self.submit_calls}"
        self.by_key[key] = task_id
        self.key_body[key] = body
        self.tasks[task_id] = {
            "task_id": task_id,
            "status": self.next_status,
            "final_artifact_id": "art-1",
            "version": 1,
        }
        return {"task_id": task_id, "status": self.next_status}

    async def get_task(self, task_id: str, **_k: Any) -> dict[str, Any]:
        self.get_calls += 1
        return dict(self.tasks[task_id])

    async def create_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self.handoff_calls += 1
        return {
            "task_id": kwargs["task_id"],
            "draft_id": "draft-new-1",
            "title": "Java 学习",
            "handoff_id": "ho-1",
            "status": "READY",
            "content_sha256": SHA_B,
            "description": "desc",
            "body_markdown": "body",
        }


class FakeCommunity:
    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {
            "draft-1": {
                "id": "draft-1",
                "status": "READY",
                "contentSha256": SHA,
                "title": "old",
            }
        }

    async def get_own_draft(self, draft_id: str, **_k: Any) -> dict[str, Any]:
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise LookupError("not found")
        return draft


@pytest_asyncio.fixture
async def harness(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    await db.initialize()
    worker_id = "worker-creator"
    async with db.sessions() as session, session.begin():
        session.add(Conversation(id="conv-1", user_id="user-1", title="c"))
        session.add(
            Run(
                id="run-creator-1",
                conversation_id="conv-1",
                user_id="user-1",
                tenant_id="zhiguang",
                prompt="create",
                status="RUNNING",
                lease_owner=worker_id,
                goal_id=None,
            )
        )
        session.add(
            RunStep(
                id="step-1",
                run_id="run-creator-1",
                ordinal=1,
                kind="TOOL",
                tool_name="creator.create_draft",
                label="create",
                status="RUNNING",
            )
        )

    creator = FakeCreator()
    community = FakeCommunity()
    ledger = SideEffectLedger(db, worker_id=worker_id)

    async def issue_capability(**_k: Any) -> CapabilityGrant:
        return CapabilityGrant(
            token="cap-token",
            capability_id="cap-1",
            expires_at="2099-01-01T00:00:00Z",
        )

    async def load_target(_ctx: ToolInvocationContext) -> dict[str, Any] | None:
        return {"draft_id": "draft-1", "content_sha256": SHA, "goal_id": "goal-1"}

    services = CreatorToolServices(
        creator=creator,  # type: ignore[arg-type]
        community=community,  # type: ignore[arg-type]
        ledger=ledger,
        issue_capability=issue_capability,
        consume_budget=AsyncMock(),
        load_content_target=load_target,
    )
    runtime = ToolRuntime(definitions=tool_registry)
    register_creator_tool_handlers(runtime, services=services)
    yield {
        "db": db,
        "runtime": runtime,
        "creator": creator,
        "community": community,
        "ledger": ledger,
        "services": services,
    }
    await db.close()


async def _invoke_create(harness: dict[str, Any], **extra: Any) -> Any:
    return await harness["runtime"].invoke(
        tool_name="creator.create_draft",
        arguments={"instruction": "写一篇 Java 学习帖子", "references": []},
        context=_ctx(),
        credentials=_creds(),
        ordinal=1,
        skip_policy=True,
        raise_on_failure=False,
        **extra,
    )


# ---------------------------------------------------------------------------
# Creator server Idempotency-Key contract (sibling package)
# ---------------------------------------------------------------------------


def test_creator_server_idempotency_key_contract() -> None:
    """Prove Creator harness strong Idempotency-Key semantics via sibling suite."""

    if not CREATOR_ROOT.exists():
        pytest.skip("creator-agent not present")
    import subprocess

    venv_python = CREATOR_ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = CREATOR_ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    completed = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "tests/test_creator_harness.py::CreatorHarnessIntegrationTests::test_create_task_is_atomic_and_idempotent",
            "-q",
            "--tb=line",
        ],
        cwd=str(CREATOR_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "Creator Idempotency-Key contract failed:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )


@pytest.mark.asyncio
async def test_fake_creator_mirrors_strong_idempotency() -> None:
    creator = FakeCreator()
    first = await creator.submit_draft(
        instruction="写 Java",
        references=[],
        access_token="t",
        idempotency_key="k1",
    )
    replay = await creator.submit_draft(
        instruction="写 Java",
        references=[],
        access_token="t",
        idempotency_key="k1",
    )
    assert first["task_id"] == replay["task_id"]
    assert creator.submit_calls == 2
    assert len(creator.tasks) == 1
    with pytest.raises(RuntimeError, match="idempotency conflict"):
        await creator.submit_draft(
            instruction="另一篇",
            references=[],
            access_token="t",
            idempotency_key="k1",
        )


# ---------------------------------------------------------------------------
# A/B create sync-via-poll and async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_waiting_dependency(harness: dict[str, Any]) -> None:
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert pending.value.task_id.startswith("task-")
    assert harness["creator"].submit_calls == 1
    async with harness["db"].sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-creator-1")
        )
    assert effect is not None
    assert effect.status == "WAITING_DEPENDENCY"
    assert effect.remote_operation_id == pending.value.task_id


@pytest.mark.asyncio
async def test_create_async_then_complete(harness: dict[str, Any]) -> None:
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    task_id = pending.value.task_id
    harness["creator"].tasks[task_id]["status"] = "COMPLETED"
    output = await handle_creator_tool(
        services=harness["services"],
        tool_name="creator.create_draft",
        arguments={"instruction": "写 Java", "references": []},
        context=_ctx(),
        definition=tool_registry.get("creator.create_draft"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    assert output["draft_id"] == "draft-new-1"
    assert harness["creator"].submit_calls == 1
    assert harness["creator"].handoff_calls == 1
    # Replay
    again = await handle_creator_tool(
        services=harness["services"],
        tool_name="creator.create_draft",
        arguments={"instruction": "写 Java", "references": []},
        context=_ctx(),
        definition=tool_registry.get("creator.create_draft"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    assert again.get("_runtime_replayed") is True
    assert harness["creator"].submit_calls == 1
    assert harness["creator"].handoff_calls == 1


@pytest.mark.asyncio
async def test_waiting_dependency_replay_does_not_resubmit(
    harness: dict[str, Any],
) -> None:
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert harness["creator"].submit_calls == 1
    assert harness["creator"].get_calls >= 1
    assert pending.value.task_id == "task-1"


@pytest.mark.asyncio
async def test_submit_timeout_unknown_no_second_create(
    harness: dict[str, Any],
) -> None:
    harness["creator"].fail_submit = True
    with pytest.raises(Exception) as raised:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert "未知" in str(raised.value) or "UNKNOWN" in type(raised.value).__name__
    assert harness["creator"].submit_calls == 1
    # Recovery with strong idempotency (FakeCreator) — one recovery submit.
    harness["creator"].fail_submit = False
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert harness["creator"].submit_calls == 2
    async with harness["db"].sessions() as session:
        effects = list(
            (
                await session.scalars(
                    select(SideEffect).where(SideEffect.run_id == "run-creator-1")
                )
            ).all()
        )
    assert len(effects) == 1


@pytest.mark.asyncio
async def test_revise_conflict_before_creator_call(harness: dict[str, Any]) -> None:
    async with harness["db"].sessions() as session, session.begin():
        session.add(
            RunStep(
                id="step-2",
                run_id="run-creator-1",
                ordinal=2,
                kind="TOOL",
                tool_name="creator.revise_draft",
                label="revise",
                status="RUNNING",
            )
        )
    with pytest.raises(LookupError):
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.revise_draft",
            arguments={
                "instruction": "加入实战",
                "draft_id": "draft-1",
                "expected_content_sha256": SHA_B,
                "references": [],
            },
            context=_ctx(),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=2,
        )
    assert harness["creator"].submit_calls == 0


@pytest.mark.asyncio
async def test_revise_success_sets_supersedes(harness: dict[str, Any]) -> None:
    async with harness["db"].sessions() as session, session.begin():
        session.add(
            RunStep(
                id="step-2",
                run_id="run-creator-1",
                ordinal=2,
                kind="TOOL",
                tool_name="creator.revise_draft",
                label="revise",
                status="RUNNING",
            )
        )
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.revise_draft",
            arguments={
                "instruction": "加入实战",
                "draft_id": "draft-1",
                "expected_content_sha256": SHA,
                "references": [],
            },
            context=_ctx(),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=2,
        )
    task_id = pending.value.task_id
    harness["creator"].tasks[task_id]["status"] = "COMPLETED"
    output = await handle_creator_tool(
        services=harness["services"],
        tool_name="creator.revise_draft",
        arguments={
            "instruction": "加入实战",
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
            "references": [],
        },
        context=_ctx(),
        definition=tool_registry.get("creator.revise_draft"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=2,
    )
    assert output["supersedes_draft_id"] == "draft-1"
    assert output["draft_id"] == "draft-new-1"


@pytest.mark.asyncio
async def test_waiting_human_not_failed(harness: dict[str, Any]) -> None:
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    harness["creator"].tasks["task-1"]["status"] = "WAITING_HUMAN"
    harness["creator"].tasks["task-1"]["pending_decision_id"] = "dec-1"
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=harness["services"],
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert pending.value.status == "WAITING_HUMAN"
    assert "人工" in str(pending.value.state.get("display_message") or "")
    assert harness["creator"].submit_calls == 1


@pytest.mark.asyncio
async def test_resume_creator_dependency_stub() -> None:
    result = await resume_creator_dependency(task_id="task-x")
    assert result["status"] == "NOT_IMPLEMENTED"


def test_migration_hygiene() -> None:
    assert "creator.create_draft" in MIGRATED_WRITE_TOOLS
    assert "creator.revise_draft" in MIGRATED_WRITE_TOOLS
    assert "creator.create_draft" not in LEGACY_BUILTIN_MIGRATION_BACKLOG
    assert "creator.revise_draft" not in LEGACY_BUILTIN_MIGRATION_BACKLOG
    source = inspect.getsource(AgentWorker._dispatch_builtin_tool)
    assert "submit_draft" not in source
    assert 'tool in {"creator.create_draft", "creator.revise_draft"}' in source
    for name in ("creator.create_draft", "creator.revise_draft"):
        definition = tool_registry.get(name)
        assert definition.transport == TransportType.BUILTIN
        assert definition.idempotency_mode == IdempotencyMode.SIDE_EFFECT_REQUIRED
        assert definition.retry_policy.max_attempts == 1
    # Architecture: creator_tools must not import worker.
    creator_src = (ROOT / "app" / "creator_tools.py").read_text(encoding="utf-8")
    assert "app.worker" not in creator_src
    assert "from app.worker" not in creator_src
