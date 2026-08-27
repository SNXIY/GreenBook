"""Targeted semantic control-plane matrix.

The matrix feeds structured Commands directly.  It deliberately does not
match user-language phrases or invoke Java/Runtime IO.
"""

from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.command.models import (
    Command,
    CommandItem,
    CommandTarget,
    CommandType,
    ResolvedSemanticState,
    TargetKind,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.command.target import TargetCandidate, Resolved
from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
from greenbook_agent_core.turn import FastPathGate, TurnRoute
from greenbook_agent_core.turn.models import AssembledTurnContext
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.task.objective_compat import objectives_from_items
from greenbook_agent_core.command.interpreter import _normalize_draft_only


def _target() -> Resolved:
    candidate = TargetCandidate(
        id="draft-java",
        kind=TargetKind.DRAFT,
        label="Java draft",
        resource_id="draft-java",
        task_id="task-java",
    )
    return Resolved(target=candidate, candidates=[candidate])


def _delta(action: str, **fields: object) -> TaskDelta:
    return TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"id": "draft-java"},
        desired_changes={"semantic_action": action, **fields},
    )


def _command(
    ctype: CommandType,
    *,
    action: str = "",
    caps: list[str] | None = None,
    items: list[CommandItem] | None = None,
    changes: list[TaskDelta] | None = None,
    target: CommandTarget | None = None,
    needs_clarification: bool = False,
    constraints: dict[str, object] | None = None,
) -> Command:
    return Command(
        type=ctype,
        goal="structured request",
        semantic_operation=action,
        required_capabilities=list(caps or ([action] if action else [])),
        items=list(items or []),
        task_changes=list(changes or []),
        target=target,
        needs_clarification=needs_clarification,
        constraints=dict(constraints or {}),
    )


@pytest.mark.parametrize(
    ("name", "command", "resolution", "expected_route"),
    [
        ("query", _command(CommandType.QUERY, caps=["LIST_DRAFTS"]), None, TurnRoute.QUERY),
        ("search", _command(CommandType.QUERY, action="SEARCH_POSTS", caps=["SEARCH_COMMUNITY"]), None, TurnRoute.COMPLEX),
        ("create_draft", _command(CommandType.CREATE, action="CREATE_DRAFT", caps=["GENERATE_CONTENT"], items=[CommandItem(topic="Java", capabilities=["GENERATE_CONTENT"])]), None, TurnRoute.COMPLEX),
        ("create_schedule", _command(CommandType.CREATE, caps=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], items=[CommandItem(topic="Agent", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="2026-08-20 14:00")]), None, TurnRoute.COMPLEX),
        ("publish_now", _command(CommandType.MODIFY, action="PUBLISH_NOW", caps=["PUBLISH_NOW"], changes=[_delta("PUBLISH_NOW")]), _target(), TurnRoute.FAST),
        ("update_content", _command(CommandType.MODIFY, action="UPDATE_DRAFT", caps=["MANAGE_DRAFT"], changes=[_delta("UPDATE_DRAFT", content="shorter")]), _target(), TurnRoute.FAST),
        ("update_title", _command(CommandType.MODIFY, action="UPDATE_DRAFT", caps=["MANAGE_DRAFT"], changes=[_delta("UPDATE_DRAFT", title="better")]), _target(), TurnRoute.FAST),
        ("update_schedule", _command(CommandType.MODIFY, action="UPDATE_SCHEDULE", caps=["MANAGE_SCHEDULE"], changes=[_delta("UPDATE_SCHEDULE", run_at="2026-08-20T08:00:00Z")]), _target(), TurnRoute.FAST),
        ("cancel_schedule", _command(CommandType.CANCEL, action="CANCEL_SCHEDULE", caps=["CANCEL_SCHEDULE"], changes=[_delta("CANCEL_SCHEDULE")]), _target(), TurnRoute.FAST),
        ("delete", _command(CommandType.MODIFY, action="DELETE_POST", caps=["DELETE_POST"], changes=[_delta("DELETE_POST")]), _target(), TurnRoute.FAST),
        ("search_create", _command(CommandType.CREATE, caps=["SEARCH_COMMUNITY", "GENERATE_CONTENT"], items=[CommandItem(topic="Java", capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT"])]), None, TurnRoute.COMPLEX),
        ("multi_objective", _command(CommandType.CREATE, caps=["GENERATE_CONTENT"], items=[CommandItem(topic="Java", capabilities=["GENERATE_CONTENT"], temporal_text="2026-08-20 09:00"), CommandItem(topic="Agent", capabilities=["GENERATE_CONTENT"], temporal_text="2026-08-20 14:00"), CommandItem(topic="Draft", capabilities=["GENERATE_CONTENT"])]), None, TurnRoute.COMPLEX),
        ("cross_turn", _command(CommandType.MODIFY, action="UPDATE_DRAFT", caps=["MANAGE_DRAFT"], changes=[_delta("UPDATE_DRAFT", content="HashMap")]), _target(), TurnRoute.FAST),
        ("ambiguous_target", _command(CommandType.MODIFY, action="UPDATE_DRAFT", caps=["MANAGE_DRAFT"], changes=[_delta("UPDATE_DRAFT")]), SimpleNamespace(is_resolved=False, is_ambiguous=True), TurnRoute.CLARIFY),
        ("unresolved_temporal", _command(CommandType.CREATE, caps=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], items=[CommandItem(topic="Agent", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="some future time")]), None, TurnRoute.CLARIFY),
    ],
)
def test_structured_semantic_matrix(name, command, resolution, expected_route):
    coordinator = TurnCoordinator()
    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=resolution,
        timezone="Asia/Shanghai",
    )
    command = command.model_copy(update={"resolved_semantics": state})
    decision = FastPathGate().decide(
        command,
        target_resolution=resolution,
        run_at=state.run_at,
        semantic_state=state,
    )
    assert decision.route == expected_route, name


def test_grounded_answer_capability_admits_canonical_rag_action() -> None:
    command = _command(
        CommandType.QUERY,
        action="SEARCH_AND_SUMMARIZE",
        caps=["SEARCH_COMMUNITY", "ANSWER_FROM_KNOWLEDGE"],
    )
    semantic_state = ResolvedSemanticState(
        operation="QUERY",
        semantic_operation="SEARCH_AND_SUMMARIZE",
        capabilities=["SEARCH_COMMUNITY", "ANSWER_FROM_KNOWLEDGE"],
    )

    decision = FastPathGate().decide(command, semantic_state=semantic_state)

    assert decision.route == TurnRoute.COMPLEX
    assert decision.semantic_actions == ["ANSWER_FROM_KNOWLEDGE"]


def test_draft_only_normalization_preserves_read_item_action_ownership() -> None:
    command = _command(
        CommandType.CREATE,
        caps=["SEARCH_COMMUNITY", "GENERATE_CONTENT"],
        constraints={"publication_intent": "DRAFT_ONLY"},
        items=[
            CommandItem(topic="RAG 评测", capabilities=["SEARCH_COMMUNITY"]),
            CommandItem(
                title="Java 线程池实践",
                topic="Java 线程池",
                capabilities=["GENERATE_CONTENT"],
                constraints={"publication_intent": "DRAFT_ONLY"},
            ),
        ],
    )

    _normalize_draft_only(command, "分别搜索并创建一篇草稿")

    assert command.items[0].capabilities == ["SEARCH_COMMUNITY"]
    assert command.items[1].capabilities == ["GENERATE_CONTENT"]


def test_canonical_temporal_is_resolved_once_for_objective_projection() -> None:
    coordinator = TurnCoordinator()
    command = _command(
        CommandType.CREATE,
        caps=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        items=[CommandItem(topic="Agent", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="2026-08-20 14:00")],
    )
    state = coordinator._resolve_semantic_state(command, target_resolution=None, timezone="Asia/Shanghai")
    assert state.temporal_resolved is True
    assert state.items[0].run_at == "2026-08-20T06:00:00Z"
    assert state.items[0].constraints["run_at"] == state.items[0].run_at


def test_create_schedule_uses_canonical_temporal_resolution() -> None:
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    command = _command(
        CommandType.CREATE,
        caps=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        items=[
            CommandItem(
                topic="Agent",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="明天下午 2 点",
            ),
        ],
    )

    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert state.temporal_kind == "FUTURE"
    assert state.temporal_resolved is True
    assert state.clarification_required is False
    assert state.run_at == "2026-08-21T06:00:00Z"
    assert state.items[0].run_at == state.run_at


def test_update_schedule_unresolved_temporal_is_clarified_before_fast_path() -> None:
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    command = _command(
        CommandType.MODIFY,
        action="UPDATE_SCHEDULE",
        caps=["MANAGE_SCHEDULE"],
        changes=[_delta("UPDATE_SCHEDULE", run_at="sometime later")],
    )

    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=_target(),
        timezone="Asia/Shanghai",
    )
    decision = FastPathGate().decide(
        command.model_copy(update={"resolved_semantics": state}),
        target_resolution=_target(),
        run_at=state.run_at,
        semantic_state=state,
    )

    assert state.temporal_kind == "UNRESOLVED"
    assert state.temporal_resolved is False
    assert state.clarification_required is True
    assert decision.route == TurnRoute.CLARIFY
    assert decision.reason == "schedule_time_unresolved"


def test_multi_objective_temporal_resolution_is_per_item() -> None:
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    command = _command(
        CommandType.CREATE,
        caps=["GENERATE_CONTENT"],
        items=[
            CommandItem(
                topic="Java",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="2026-08-21 09:00",
            ),
            CommandItem(
                topic="Agent",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="2026-08-21 14:00",
            ),
        ],
    )

    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert [item.run_at for item in state.items] == [
        "2026-08-21T01:00:00Z",
        "2026-08-21T06:00:00Z",
    ]
    assert state.temporal_resolved is True
    assert state.clarification_required is False
    objectives = objectives_from_items(
        command.items,
        "temporal-isolation",
        timezone="Asia/Shanghai",
        resolved_state=state,
    )
    assert [objective.constraints["run_at"] for objective in objectives] == [
        "2026-08-21T01:00:00Z",
        "2026-08-21T06:00:00Z",
    ]


def test_item_publication_ownership_does_not_broadcast_schedule_to_sibling() -> None:
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    command = _command(
        CommandType.CREATE,
        caps=["SEARCH_COMMUNITY", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
        items=[
            CommandItem(
                item_key="A",
                topic="RAG 评测",
                capabilities=["SEARCH_COMMUNITY"],
            ),
            CommandItem(
                item_key="B",
                title="Agent 可观测性实践",
                capabilities=["GENERATE_CONTENT"],
                constraints={"publication_intent": "DRAFT_ONLY"},
            ),
            CommandItem(
                item_key="C",
                title="Agent 可观测性实践",
                capabilities=["SCHEDULE_PUBLISH"],
                temporal_text="明天 10:00",
                dependencies=["B"],
                constraints={"publication_intent": "SCHEDULED_PUBLISH"},
            ),
        ],
    )

    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert state.clarification_required is False
    assert [item.publication_intent for item in state.items] == [
        "", "DRAFT_ONLY", "SCHEDULED_PUBLISH",
    ]
    assert state.items[2].run_at == "2026-08-21T02:00:00Z"
    assert state.items[2].dependencies == ["B"]


def test_unresolved_future_never_routes_to_publish_now() -> None:
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    command = _command(
        CommandType.MODIFY,
        action="UPDATE_SCHEDULE",
        caps=["MANAGE_SCHEDULE"],
        changes=[_delta("UPDATE_SCHEDULE", run_at="以后")],
    )

    state = coordinator._resolve_semantic_state(
        command,
        target_resolution=_target(),
        timezone="Asia/Shanghai",
    )
    decision = FastPathGate().decide(
        command.model_copy(update={"resolved_semantics": state}),
        target_resolution=_target(),
        run_at=state.run_at,
        semantic_state=state,
    )

    assert state.temporal_kind == "UNRESOLVED"
    assert state.clarification_required is True
    assert "PUBLISH_NOW" not in decision.semantic_actions
    assert decision.route == TurnRoute.CLARIFY


class _TemporalSmokeAssembler:
    async def assemble(self, **kwargs):
        conversation_id = str(kwargs.get("conversation_id") or "smoke")
        return AssembledTurnContext(
            conversation_id=conversation_id,
            user_id="smoke-user",
            tenant_id="smoke-tenant",
            timezone="Asia/Shanghai",
            snapshot=ContextSnapshot(conversation_id=conversation_id),
        )


class _TemporalSmokeCommandRuntime:
    def __init__(self, command: Command) -> None:
        self.command = command

    async def interpret(self, *args, **kwargs):
        return self.command


class _TemporalSmokeRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_for_command(self, **kwargs):
        command = kwargs["command"]
        state = command.resolved_semantics
        self.calls.append({
            "semantic_action": command.semantic_operation,
            "run_at": state.run_at,
            "clarification_required": state.clarification_required,
        })
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=str(kwargs.get("run_id") or "smoke-run"),
            execution_path="action_loop",
        )


@pytest.mark.asyncio
async def test_runtime_resolved_schedule_enters_runtime_and_unresolved_does_not() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    resolved_command = _command(
        CommandType.CREATE,
        caps=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        items=[
            CommandItem(
                topic="Agent",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="明天下午 2 点",
            ),
        ],
    )
    resolved_runtime = _TemporalSmokeRuntime()
    resolved_coordinator = TurnCoordinator(
        context_assembler=_TemporalSmokeAssembler(),
        command_runtime=_TemporalSmokeCommandRuntime(resolved_command),
        action_loop_executor=resolved_runtime,
        temporal_resolver=TemporalResolver(now=now),
        tool_registry=[],
    )
    resolved = await resolved_coordinator.execute(
        conversation_id="temporal-resolved",
        user_id="smoke-user",
        tenant_id="smoke-tenant",
        message="明天下午 2 点发布",
        timezone="Asia/Shanghai",
        run_id="resolved-run",
    )

    assert resolved.status == "COMPLETED"
    assert len(resolved_runtime.calls) == 1
    assert resolved_runtime.calls[0]["run_at"] == "2026-08-21T06:00:00Z"

    unresolved_command = resolved_command.model_copy(
        deep=True,
        update={
            "items": [
                CommandItem(
                    topic="Agent",
                    capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    temporal_text="sometime later",
                ),
            ],
        },
    )
    unresolved_runtime = _TemporalSmokeRuntime()
    unresolved_coordinator = TurnCoordinator(
        context_assembler=_TemporalSmokeAssembler(),
        command_runtime=_TemporalSmokeCommandRuntime(unresolved_command),
        action_loop_executor=unresolved_runtime,
        temporal_resolver=TemporalResolver(now=now),
        tool_registry=[],
    )
    unresolved = await unresolved_coordinator.execute(
        conversation_id="temporal-unresolved",
        user_id="smoke-user",
        tenant_id="smoke-tenant",
        message="安排在以后发布",
        timezone="Asia/Shanghai",
        run_id="unresolved-run",
    )

    assert unresolved.status == "WAITING_HUMAN"
    assert unresolved.error_code == "SCHEDULE_TIME_REQUIRED"
    assert unresolved_runtime.calls == []


def test_mcp_java_scope_propagates_agent_run_id_to_read_headers() -> None:
    from greenbook_java_client.client import JavaClient, agent_run_scope

    client = JavaClient(base_url="http://127.0.0.1:8080")
    with agent_run_scope("run-123"):
        headers = client._headers(trace_id="trace-1")
    assert headers["X-Agent-Run-Id"] == "run-123"
