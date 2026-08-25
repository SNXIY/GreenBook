"""Focused Conversation Lifecycle regression coverage.

These tests exercise the existing Conversation/Context/Target boundaries.  They
do not create a second session store or invoke the expensive evaluation matrix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.api.routes import deterministic_conversation_title
from greenbook_agent_api.services.action_loop_executor import _command_task_id
from greenbook_agent_api.services.explicit_resource_admission import (
    admit_explicit_resources,
    explicit_resource_references,
)
from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolver
from greenbook_agent_core.context import ContextBuilder, SessionContext


class _ObservationStore:
    async def list_recent_for_tasks(self, *, task_ids: list[str], limit: int):
        assert task_ids == ["task-a"]
        return [
            {"conversation_id": "conversation-a", "task_id": "task-a", "status": "COMPLETED"},
            {"conversation_id": "conversation-b", "task_id": "task-a", "status": "COMPLETED"},
        ][:limit]


class _Mcp:
    def __init__(self, result: dict):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, kwargs))
        return self.result


class _McpByTool:
    def __init__(self, results: dict[str, dict]):
        self.results = results
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, tool_name: str, **kwargs):
        self.calls.append((tool_name, kwargs))
        return self.results[tool_name]


class _TaskManager:
    def __init__(self) -> None:
        self.added: list[dict] = []

    async def create_task(self, **kwargs):
        return SimpleNamespace(task_id="task-b", **kwargs)

    async def add_resource(self, task_id: str, **kwargs):
        self.added.append({"task_id": task_id, **kwargs})
        return SimpleNamespace(task_id=task_id)


def _auth() -> SimpleNamespace:
    return SimpleNamespace(user_id="user-a", tenant_id="tenant-a")


def _session(conversation_id: str = "conversation-b") -> SessionContext:
    return SessionContext(
        conversation_id=conversation_id,
        user_id="user-a",
        tenant_id="tenant-a",
        active_task_id="old-task-from-another-turn",
    )


def _explicit_draft_command(*, draft_id: str = "draft-a") -> Command:
    return Command(
        type=CommandType.MODIFY,
        raw_input=f"把 Draft ID {draft_id} 的标题改成新的标题",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={
                    "kind": "DRAFT",
                    "resource_kind": "DRAFT",
                    "draft_id": draft_id,
                },
                desired_changes={
                    "semantic_action": "UPDATE_DRAFT",
                    "title": "新的标题",
                },
            )
        ],
    )


def _explicit_resource_command(kind: str, resource_id: str) -> Command:
    field = {
        "DRAFT": "draft_id",
        "SCHEDULE": "schedule_id",
        "POST": "post_id",
    }[kind]
    return Command(
        type=CommandType.MODIFY,
        raw_input=f"Modify {kind} ID {resource_id}",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={
                    "kind": kind,
                    "resource_kind": kind,
                    field: resource_id,
                },
                desired_changes={"semantic_action": "UPDATE_DRAFT" if kind == "DRAFT" else "UPDATE_SCHEDULE"},
            )
        ],
    )


@pytest.mark.asyncio
async def test_a_new_turn_does_not_admit_implicit_cross_conversation_resource() -> None:
    command = Command(
        type=CommandType.MODIFY,
        raw_input="把刚才那篇改一下",
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "DRAFT", "reference": "刚才那篇"},
                desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "改后"},
            )
        ],
    )
    mcp = _Mcp({"ok": True, "data": {"draft_id": "should-not-be-read"}})

    assert explicit_resource_references(command) == []
    admission = await admit_explicit_resources(
        command,
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-a",
        run_id="run-a",
    )

    assert not admission.failed
    assert admission.candidates == []
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_b_context_observations_are_filtered_by_conversation() -> None:
    builder = ContextBuilder(observation_store=_ObservationStore())

    observations = await builder._load_recent_observations(  # noqa: SLF001
        {"task-a"},
        conversation_id="conversation-a",
        limit=5,
    )

    assert len(observations) == 1
    assert observations[0]["conversation_id"] == "conversation-a"


@pytest.mark.asyncio
async def test_c_explicit_resource_is_read_from_user_scoped_business_truth() -> None:
    mcp = _Mcp({
        "ok": True,
        "data": {
            "draft_id": "draft-a",
            "title": "Draft A",
            "owner_id": "user-a",
        },
    })
    admission = await admit_explicit_resources(
        _explicit_draft_command(),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-c",
        run_id="run-c",
    )

    assert not admission.failed
    assert admission.external_candidates[0]["resource_id"] == "draft-a"
    assert admission.external_candidates[0]["source"] == "JAVA_BUSINESS_TRUTH"
    assert admission.command.parameters["__external_explicit_resource_admission"][0]["resource_id"] == "draft-a"
    assert "__force_new_task_for_explicit_resource" not in admission.command.parameters
    assert admission.command.parameters["__explicit_resource_admission"][0]["ownership_verified"] is True
    reference = admission.command.task_changes[0].target_reference
    assert reference["draft_id"] == "draft-a"
    assert [name for name, _kwargs in mcp.calls] == ["content.get_draft"]


@pytest.mark.asyncio
async def test_d_wrong_user_resource_fails_closed_before_any_write() -> None:
    mcp = _Mcp({
        "ok": True,
        "data": {
            "draft_id": "draft-other",
            "title": "Other user",
            "owner_id": "user-other",
        },
    })
    admission = await admit_explicit_resources(
        _explicit_draft_command(draft_id="draft-other"),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-d",
        run_id="run-d",
    )

    assert admission.failed
    assert admission.candidates == []
    assert [name for name, _kwargs in mcp.calls] == ["content.get_draft"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "resource_id"),
    [("SCHEDULE", "schedule-a"), ("POST", "post-a")],
)
async def test_d1_schedule_and_post_positive_variants_require_owned_business_truth(
    kind: str,
    resource_id: str,
) -> None:
    if kind == "SCHEDULE":
        mcp = _McpByTool({
            "publication.get_status": {
                "ok": True,
                "data": {"schedule_id": resource_id, "draft_id": "draft-a", "status": "SCHEDULED"},
            },
            "content.get_draft": {
                "ok": True,
                "data": {"draft_id": "draft-a", "owner_id": "user-a"},
            },
        })
    else:
        mcp = _McpByTool({
            "community.get_post": {
                "ok": True,
                "data": {"post_id": resource_id, "title": "Post A"},
            },
            "community.list_own_posts": {
                "ok": True,
                "data": [{"post_id": resource_id}],
            },
        })

    admission = await admit_explicit_resources(
        _explicit_resource_command(kind, resource_id),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-d1",
        run_id="run-d1",
    )

    assert not admission.failed
    assert admission.external_candidates[0]["resource_id"] == resource_id
    assert admission.external_candidates[0]["ownership_verified"] is True


@pytest.mark.asyncio
async def test_d2_schedule_without_owned_linked_draft_fails_closed() -> None:
    mcp = _McpByTool({
        "publication.get_status": {
            "ok": True,
            "data": {"schedule_id": "schedule-other", "draft_id": "draft-other"},
        },
        "content.get_draft": {
            "ok": True,
            "data": {"draft_id": "draft-other", "owner_id": "user-other"},
        },
    })

    admission = await admit_explicit_resources(
        _explicit_resource_command("SCHEDULE", "schedule-other"),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-d2",
        run_id="run-d2",
    )

    assert admission.failed
    assert admission.candidates == []
    assert [name for name, _kwargs in mcp.calls] == [
        "publication.get_status",
        "content.get_draft",
    ]


def test_e_target_resolver_accepts_only_the_admitted_typed_candidate() -> None:
    command = _explicit_draft_command()
    candidate = {
        "id": "draft-a",
        "resource_id": "draft-a",
        "resource_kind": "DRAFT",
        "resource_index": [{"resource_id": "draft-a", "resource_kind": "DRAFT"}],
        "metadata": {"resource_refs": [{"resource_id": "draft-a", "resource_kind": "DRAFT"}]},
    }

    resolution = TargetResolver().resolve_task_delta(
        command.task_changes[0],
        [candidate],
        user_input=command.raw_input,
    )

    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.resource_id == "draft-a"


@pytest.mark.asyncio
async def test_f_external_resource_gets_a_fresh_current_task_before_action_loop() -> None:
    mcp = _Mcp({"ok": True, "data": {"draft_id": "draft-a", "owner_id": "user-a"}})
    admission = await admit_explicit_resources(
        _explicit_draft_command(),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-f",
        run_id="run-f",
    )
    manager = _TaskManager()
    coordinator = SimpleNamespace(_task_manager=manager)
    command, error = await TurnCoordinator._materialize_external_resource_task(  # noqa: SLF001
        coordinator,
        admission.command,
        SimpleNamespace(conversation_id="conversation-b", user_id="user-a", tenant_id="tenant-a", run_id="run-f"),
        SimpleNamespace(is_resolved=True),
    )

    assert error == ""
    assert command.resolved_target["task_id"] == "task-b"
    assert manager.added[0]["resource_id"] == "draft-a"
    assert _command_task_id(command, _session()) == "task-b"


def test_g_new_conversation_title_is_deterministic_and_bounded() -> None:
    title = deterministic_conversation_title("  第一行\n\n第二行   " + "x" * 100)

    assert title.startswith("第一行 第二行")
    assert len(title) == 64
    assert "\n" not in title


@pytest.mark.asyncio
async def test_h_external_admission_does_not_copy_conversation_transient_state() -> None:
    mcp = _Mcp({"ok": True, "data": {"draft_id": "draft-a", "owner_id": "user-a"}})
    admission = await admit_explicit_resources(
        _explicit_draft_command(),
        existing_candidates=[],
        mcp=mcp,
        auth=_auth(),
        session=_session(),
        trace_id="trace-h",
        run_id="run-h",
    )

    candidate = admission.candidates[0]
    assert "conversation_id" not in candidate
    assert "recent_entities" not in candidate
    assert "recent_tool_calls" not in candidate
    assert "pending_approval" not in candidate


def test_i_business_resource_candidate_has_no_implicit_recent_context_fields() -> None:
    command = _explicit_draft_command()
    assert "recent_entities" not in command.model_dump(mode="python")
    assert "conversation_id" not in command.model_dump(mode="python")
