"""Focused Phase 3A tests: TurnCoordinator / ContextAssembler / FastPathGate.

Every external boundary (read tool, write submission, context sources) is an
explicit test stub.  No stub fabricates a real Java/DB/LLM result: Fast Path
reads map real (stubbed) responses and Fast Path writes only assert correct
routing to the durable submission boundary — real VerificationEvidence and
OperationReceipt are produced downstream, out of scope here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_core.command.models import (
    Command,
    CommandItem,
    CommandContext,
    CommandTarget,
    CommandType,
    TargetKind,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import (
    Ambiguous,
    Resolved,
    TargetResolver,
)

from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.turn import (
    AssembledTurnContext,
    ContextAssembler,
    FastPathExecutor,
    FastPathGate,
    TurnBudget,
    TurnRequest,
    TurnRoute,
)
from greenbook_agent_core.turn.context_assembler import _PENDING_STATUSES

from tests.unit.test_turn_phase3a_stubs import (
    StubTool,
    _target_candidate,
)


def _command(
    *,
    ctype: CommandType,
    semantic_operation: str = "",
    task_changes: list[TaskDelta] | None = None,
    required_capabilities: list[str] | None = None,
    needs_clarification: bool = False,
) -> Command:
    grounded_input = " ".join(
        str((delta.target_reference or {}).get("label") or "")
        for delta in (task_changes or [])
    ).strip() or "test input"
    return Command(
        type=ctype,
        semantic_operation=semantic_operation,
        task_changes=task_changes or [],
        required_capabilities=required_capabilities or [],
        needs_clarification=needs_clarification,
        target=None,
        raw_input=grounded_input,
    )


def _update_draft_delta(**changes) -> TaskDelta:
    desired = {"semantic_action": "UPDATE_DRAFT"}
    desired.update(changes)
    return TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java 那篇"},
        desired_changes=desired,
    )


def _resolved_task(task_id: str = "task-java") -> Resolved:
    return Resolved(
        target=_target_candidate(task_id=task_id, kind="TASK"),
        candidates=[_target_candidate(task_id=task_id, kind="TASK")],
    )


def test_delta_reference_resolves_conversation_objective_before_active_task() -> None:
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Java 那篇"},
            desired_changes={"semantic_action": "UPDATE_SCHEDULE", "run_at": "2026-08-19T08:00:00Z"},
        )],
    )
    assembled = SimpleNamespace(snapshot=ContextSnapshot(
        active_task_id="task-agent",
        active_tasks=[
            {
                "task_id": "task-java",
                "created_at": "2026-08-18T00:00:00Z",
                "objectives": [{
                    "objective_id": "objective-java",
                    "description": "Java backend article",
                    "status": "COMPLETED",
                    "constraints": {"run_at": "2026-08-19T01:00:00Z"},
                }],
                "resource_index": [{"resource_id": "schedule-java", "resource_kind": "SCHEDULE", "objective_id": "objective-java"}],
            },
            {
                "task_id": "task-agent",
                "created_at": "2026-08-18T00:01:00Z",
                "objectives": [{
                    "objective_id": "objective-agent",
                    "description": "AI Agent article",
                    "status": "COMPLETED",
                    "constraints": {"run_at": "2026-08-19T06:00:00Z"},
                }],
                "resource_index": [{"resource_id": "schedule-agent", "resource_kind": "SCHEDULE", "objective_id": "objective-agent"}],
            },
        ],
    ))

    resolution = coordinator._resolve_delta_objective_target(command, assembled)

    assert resolution is not None and resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.task_id == "task-java"
    assert command.task_changes[0].desired_changes["objective_id"] == "objective-java"


def test_multi_owner_deltas_resolve_independently_without_turn_ambiguity() -> None:
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "SCHEDULE", "label": "Java"},
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
            ),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "SCHEDULE", "label": "Agent"},
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
            ),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "DRAFT", "label": "Redis"},
                desired_changes={"semantic_action": "UPDATE_DRAFT"},
            ),
        ],
    )
    assembled = SimpleNamespace(snapshot=ContextSnapshot(
        active_tasks=[
            {
                "task_id": "task-java",
                "goal": "Java article",
                "resource_index": [{"resource_id": "schedule-java", "resource_kind": "SCHEDULE", "label": "Java"}],
                "objectives": [{"objective_id": "objective-java", "description": "Java", "status": "PENDING"}],
            },
            {
                "task_id": "task-agent",
                "goal": "Agent article",
                "resource_index": [{"resource_id": "schedule-agent", "resource_kind": "SCHEDULE", "label": "Agent"}],
                "objectives": [{"objective_id": "objective-agent", "description": "Agent", "status": "PENDING"}],
            },
            {
                "task_id": "task-redis",
                "goal": "Redis article",
                "resource_index": [{"resource_id": "draft-redis", "resource_kind": "DRAFT", "label": "Redis"}],
                "objectives": [{"objective_id": "objective-redis", "description": "Redis", "status": "PENDING"}],
            },
        ],
    ))

    resolution = coordinator._resolve_delta_objective_target(command, assembled)

    assert resolution is not None and resolution.is_resolved
    assert [
        change.desired_changes.get("target_objective_id")
        for change in command.task_changes
    ] == ["objective-java", "objective-agent", "objective-redis"]
    assert [
        change.target_reference.get("resource_id")
        for change in command.task_changes
    ] == ["schedule-java", "schedule-agent", "draft-redis"]


def test_multi_owner_delta_only_ambiguous_item_blocks_the_turn() -> None:
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "DRAFT", "label": "Java"},
                desired_changes={"semantic_action": "UPDATE_DRAFT"},
            ),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"kind": "DRAFT", "label": "Agent"},
                desired_changes={"semantic_action": "UPDATE_DRAFT"},
            ),
        ],
    )
    assembled = SimpleNamespace(snapshot=ContextSnapshot(
        active_tasks=[
            {
                "task_id": "task-java",
                "goal": "Java article",
                "resource_index": [{"resource_id": "draft-java", "resource_kind": "DRAFT", "label": "Java"}],
                "objectives": [{"objective_id": "objective-java", "description": "Java", "status": "PENDING"}],
            },
            {
                "task_id": "task-agent-a",
                "goal": "Agent article A",
                "resource_index": [{"resource_id": "draft-agent-a", "resource_kind": "DRAFT", "label": "Agent"}],
                "objectives": [{"objective_id": "objective-agent-a", "description": "Agent", "status": "PENDING"}],
            },
            {
                "task_id": "task-agent-b",
                "goal": "Agent article B",
                "resource_index": [{"resource_id": "draft-agent-b", "resource_kind": "DRAFT", "label": "Agent"}],
                "objectives": [{"objective_id": "objective-agent-b", "description": "Agent", "status": "PENDING"}],
            },
        ],
    ))

    resolution = coordinator._resolve_delta_objective_target(command, assembled)

    assert resolution is not None and resolution.is_ambiguous
    assert command.task_changes[0].desired_changes["target_objective_id"] == "objective-java"


@pytest.mark.asyncio
async def test_coordinator_propagates_ambiguous_target_state() -> None:
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = Command(
        type=CommandType.MODIFY,
        target=CommandTarget(
            kind=TargetKind.POST,
            reference_type="ACTIVE",
        ),
        task_changes=[_update_draft_delta(title="new")],
    )
    resolution = await coordinator._resolve_target(
        command,
        CommandContext(targets=[
            {"kind": "POST", "id": "post-a", "task_id": "task-a"},
            {"kind": "POST", "id": "post-b", "task_id": "task-b"},
        ]),
    )

    assert resolution is not None and resolution.is_ambiguous
    assert command.target_resolution == "AMBIGUOUS"
    assert len(resolution.candidates) == 2
    assert {item["id"] for item in command.target_candidates} == {"post-a", "post-b"}

    state = coordinator._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=resolution,
        timezone="Asia/Shanghai",
    )
    assert {item["id"] for item in state.target_candidates} == {"post-a", "post-b"}


@pytest.mark.asyncio
async def test_query_target_reference_reaches_resolver_and_clarifies_not_found() -> None:
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = Command(
        type=CommandType.QUERY,
        semantic_operation="QUERY",
        target=CommandTarget(kind=TargetKind.DRAFT, label="RAG"),
        needs_clarification=True,
    )

    resolution = await coordinator._resolve_target(
        command,
        CommandContext(targets=[]),
    )

    assert resolution is not None
    assert resolution.status.value == "NOT_FOUND"
    assert command.target_resolution == "NOT_FOUND"
    state = coordinator._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=resolution,
        timezone="Asia/Shanghai",
    )
    assert state.clarification_required is True


def test_provider_acknowledgement_clarification_evidence_routes_to_chat() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.QUERY,
        needs_clarification=True,
    ).model_copy(update={"ambiguity": "confirmation phrase has no actionable request"})

    decision = gate.decide(command)

    assert decision.route == TurnRoute.CHAT
    assert decision.reason == "no_action_chat"


def test_actionable_command_is_not_collapsed_to_chat_by_ack_guard() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        needs_clarification=True,
    )

    decision = gate.decide(command)

    assert decision.route == TurnRoute.CLARIFY


def test_provider_acknowledgement_evidence_is_removed_from_resolved_state() -> None:
    coordinator = object.__new__(TurnCoordinator)
    command = _command(
        ctype=CommandType.QUERY,
        needs_clarification=True,
    ).model_copy(update={"ambiguity": "confirmation phrase has no actionable request"})

    state = coordinator._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert state.clarification_required is False
    assert state.clarification_reason == ""


def test_confirm_query_without_action_is_still_conversational_chat() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.QUERY,
        semantic_operation="CONFIRM",
        needs_clarification=True,
        task_changes=[TaskDelta(operation=TaskDeltaOperation.NO_CHANGE)],
    ).model_copy(update={"ambiguity": "confirmation phrase has no actionable request"})

    decision = gate.decide(command)

    assert decision.route == TurnRoute.CHAT
    assert decision.reason == "no_action_chat"


def test_resolved_target_overrides_stale_provider_ambiguity_evidence() -> None:
    coordinator = object.__new__(TurnCoordinator)
    command = Command(
        type=CommandType.CANCEL,
        semantic_operation="DELETE_POST",
        target=CommandTarget(kind=TargetKind.POST, reference_type="ACTIVE"),
        target_resolution="RESOLVED",
        ambiguity="provider guessed multiple posts",
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "POST", "reference_type": "ACTIVE"},
            desired_changes={"semantic_action": "DELETE_POST"},
        )],
    )

    state = coordinator._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=Resolved(
            target=_target_candidate(task_id="task-1", kind="POST", resource_id="post-1"),
            candidates=[_target_candidate(task_id="task-1", kind="POST", resource_id="post-1")],
        ),
        timezone="Asia/Shanghai",
    )

    assert state.clarification_required is False
    assert state.clarification_reason == ""


# ── FastPathGate routing ────────────────────────────────────────────────


def test_fast_write_update_draft_returns_fast() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[_update_draft_delta(title="Java 并发指南")],
    )
    decision = gate.decide(command, target_resolution=_resolved_task())
    assert decision.route == TurnRoute.FAST
    assert decision.semantic_actions == ["UPDATE_DRAFT"]


def test_fast_write_update_schedule_requires_time() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            _update_draft_delta(semantic_action="UPDATE_SCHEDULE", run_at="2026-08-15T17:00:00")
        ],
    )
    # No resolved run_at -> the gate must NOT mark it FAST.
    decision = gate.decide(command, target_resolution=_resolved_task(), run_at=None)
    assert decision.route == TurnRoute.CLARIFY
    # With a resolved run_at it may be FAST.
    decision = gate.decide(command, target_resolution=_resolved_task(), run_at="2026-08-15T09:00:00Z")
    assert decision.route == TurnRoute.FAST


def test_fast_write_cancel_schedule_returns_fast() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            _update_draft_delta(semantic_action="CANCEL_SCHEDULE")
        ],
    )
    decision = gate.decide(command, target_resolution=_resolved_task())
    assert decision.route == TurnRoute.FAST


def test_ambiguous_target_never_fast() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[_update_draft_delta(title="x")],
    )
    ambiguous = Ambiguous(
        candidates=[
            _target_candidate(task_id="task-a", kind="TASK"),
            _target_candidate(task_id="task-b", kind="TASK"),
        ]
    )
    decision = gate.decide(command, target_resolution=ambiguous)
    assert decision.route == TurnRoute.CLARIFY
    assert decision.reason == "ambiguous_target"


def test_multiple_actions_returns_complex() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            _update_draft_delta(title="a"),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                target_reference={"label": "x"},
                desired_changes={"semantic_action": "DELETE_DRAFT"},
            ),
        ],
    )
    decision = gate.decide(command, target_resolution=_resolved_task())
    assert decision.route == TurnRoute.COMPLEX


def test_multi_task_returns_complex() -> None:
    gate = FastPathGate()
    # Two deltas (two tasks), even with the same action, must not be FAST.
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[
            _update_draft_delta(title="a"),
            _update_draft_delta(title="b"),
        ],
    )
    decision = gate.decide(command, target_resolution=_resolved_task())
    assert decision.route == TurnRoute.COMPLEX


def test_fast_read_lists_drafts_returns_query() -> None:
    gate = FastPathGate()
    command = _command(ctype=CommandType.QUERY, required_capabilities=["LIST_DRAFTS"])
    decision = gate.decide(command)
    assert decision.route == TurnRoute.QUERY
    assert decision.semantic_actions == ["LIST_DRAFTS"]


def test_temporal_resolution_uses_structured_item_time() -> None:
    coordinator = TurnCoordinator()
    command = Command(
        type=CommandType.CREATE,
        goal="create an Agent learning post",
        items=[CommandItem(
            topic="Agent learning",
            capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            temporal_text="2026-08-20 14:00",
        )],
    )

    assert coordinator._resolve_temporal(command, "Asia/Shanghai") == "2026-08-20T06:00:00Z"


def test_chat_no_action_returns_chat() -> None:
    gate = FastPathGate()
    command = _command(ctype=CommandType.QUERY, required_capabilities=[])
    decision = gate.decide(command)
    assert decision.route == TurnRoute.CHAT


def test_search_capability_only_query_enters_complex_search_path() -> None:
    gate = FastPathGate()
    # The structured provider contract may emit QUERY + SEARCH_COMMUNITY with
    # no deliverable items.  That is still an explicit read request, not chat.
    command = _command(
        ctype=CommandType.QUERY,
        semantic_operation="SEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.COMPLEX
    assert decision.semantic_actions == ["SEARCH_POSTS"]


def test_query_operation_marker_without_actions_returns_chat() -> None:
    # The interpreter may preserve QUERY as a semantic operation while the
    # structured envelope contains no capability or mutation.  That is still
    # ordinary chat, not a reason to enter the ActionLoop.
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.QUERY,
        semantic_operation="QUERY",
        required_capabilities=[],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.CHAT


def test_unknown_query_operation_without_capabilities_returns_chat() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.QUERY,
        semantic_operation="EXPLAIN",
        required_capabilities=[],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.CHAT


def test_topic_only_generation_is_not_allowed_to_create_a_draft() -> None:
    gate = FastPathGate()
    command = Command(
        type=CommandType.CREATE,
        semantic_operation="GENERATE_CONTENT",
        required_capabilities=["GENERATE_CONTENT"],
        items=[CommandItem(topic="travel packing advice", requirements=["give advice"])],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.CHAT
    assert decision.reason == "unqualified_content_request"


def test_titled_generation_remains_on_complex_creation_path() -> None:
    gate = FastPathGate()
    command = Command(
        type=CommandType.CREATE,
        semantic_operation="GENERATE_CONTENT",
        required_capabilities=["GENERATE_CONTENT"],
        items=[CommandItem(title="Java guide", topic="Java", capabilities=["GENERATE_CONTENT"])],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.COMPLEX


def test_search_action_stays_complex() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.QUERY,
        semantic_operation="SEARCH_POSTS",
        required_capabilities=["SEARCH_COMMUNITY"],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.COMPLEX


def test_goal_create_stays_complex() -> None:
    gate = FastPathGate()
    command = _command(
        ctype=CommandType.CREATE,
        semantic_operation="CREATE_DRAFT",
        required_capabilities=["GENERATE_CONTENT"],
    )
    decision = gate.decide(command)
    assert decision.route == TurnRoute.COMPLEX


# ── ContextAssembler isolation / completed-task reference ───────────────


class _FakeBuilder:
    def __init__(self, snapshot: ContextSnapshot) -> None:
        self._snapshot = snapshot

    def build(self, **kwargs):
        return self._snapshot


def _snapshot_with_two_tasks() -> ContextSnapshot:
    return ContextSnapshot(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        timezone="Asia/Shanghai",
        active_tasks=[
            {"task_id": "task-java", "status": "RUNNING", "goal": "Java 并发"},
            {"task_id": "task-agent", "status": "COMPLETED", "goal": "Agent 文章"},
        ],
        artifacts=[
            {"artifact_id": "a1", "task_id": "task-java", "resource_id": "draft-java",
             "resource_kind": "DRAFT", "title": "Java 草稿"},
            {"artifact_id": "a2", "task_id": "task-agent", "resource_id": "draft-agent",
             "resource_kind": "DRAFT", "title": "Agent 草稿"},
        ],
        available_resources=[
            {"id": "draft-java", "task_id": "task-java", "kind": "DRAFT"},
            {"id": "draft-agent", "task_id": "task-agent", "kind": "DRAFT"},
        ],
        execution_states=[
            {"task_id": "task-java", "status": "RESULT_UNKNOWN"},
            {"task_id": "task-agent", "status": "COMPLETED"},
            {"task_id": "task-java", "status": "SUBMITTED"},
        ],
    )


@pytest.mark.asyncio
async def test_context_scopes_artifacts_resources_to_focus_task() -> None:
    assembler = ContextAssembler(
        _FakeBuilder(_snapshot_with_two_tasks()),
        budget=TurnBudget(),
    )
    assembled = await assembler.assemble(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        focus_task_ids=["task-java"],
    )
    assert all(item["task_id"] == "task-java" for item in assembled.selected_artifacts)
    assert [item["artifact_id"] for item in assembled.selected_artifacts] == ["a1"]
    assert all(item["task_id"] == "task-java" for item in assembled.selected_resources)
    # Only pending / result-unknown executions for the focus task.
    assert all(item["status"].upper() in _PENDING_STATUSES for item in assembled.selected_executions)
    assert {item["status"] for item in assembled.selected_executions} == {"RESULT_UNKNOWN", "SUBMITTED"}


# ── TurnCoordinator mutation-resource owner fallback ──────────────────────
#
# W3: "刚刚那篇取消发布，草稿保留" produces a top-level TASK target whose id is a
# Draft resource id (DraftA) — it never matches a Task.id, so the resolver
# returns NOT_FOUND.  The mutation task_changes, however, already carry the
# concrete ScheduleA resource.  _resolve_target must resolve the owning Task
# from that resource (via the canonical resource->owner lookup) and return a
# genuinely RESOLVED result so the gate routes FAST instead of CLARIFY.


@pytest.mark.asyncio
async def test_resolve_target_rescues_owner_from_mutation_resource() -> None:
    coordinator = TurnCoordinator()
    assembled = SimpleNamespace(
        selected_tasks=[{
            "task_id": "task-java",
            "kind": "TASK",
            "goal": "Java 并发指南",
            "resource_index": [
                {"resource_id": "draft-123", "resource_kind": "DRAFT", "task_id": "task-java"},
                {"resource_id": "schedule-456", "resource_kind": "SCHEDULE", "task_id": "task-java"},
            ],
        }]
    )
    command = Command(
        type=CommandType.CANCEL,
        goal="刚刚那篇取消发布，草稿保留",
        target=CommandTarget(kind=TargetKind.TASK, id="draft-123"),
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "SCHEDULE", "id": "schedule-456"},
            desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
            needs_target_resolution=False,
        )],
        required_capabilities=["CANCEL_SCHEDULE"],
    )
    # Empty context: no Task matches the top-level Draft id, so the resolver
    # returns NOT_FOUND and only the mutation-resource fallback can ground it.
    resolution = await coordinator._resolve_target(
        command, CommandContext(targets=[]), assembled=assembled
    )

    assert resolution is not None
    assert resolution.is_resolved
    assert command.resolved_target["task_id"] == "task-java"


def test_delta_target_matches_objective_requirement_when_title_is_generated() -> None:
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"label": "Java 学习草稿"},
        desired_changes={"semantic_action": "DELETE_DRAFT"},
    )
    candidates = [
        {
            "goal_id": "objective-a",
            "task_id": "task-a",
            "label": "generated objective A",
            "constraints": {"requirements": ["创建一个标题为 A 的 Java 草稿"]},
            "resource_index": [{
                "resource_id": "draft-a",
                "resource_kind": "DRAFT",
                "title": "generated topic A",
                "objective_id": "objective-a",
            }],
        },
        {
            "goal_id": "objective-b",
            "task_id": "task-b",
            "label": "generated objective B",
            "constraints": {"requirements": ["创建一个标题为 B 的 Java 草稿"]},
            "resource_index": [{
                "resource_id": "draft-b",
                "resource_kind": "DRAFT",
                "title": "generated topic B",
                "objective_id": "objective-b",
            }],
        },
    ]

    resolution = TargetResolver().resolve_task_delta(delta, candidates)

    assert resolution.is_ambiguous
    assert {candidate.resource_id for candidate in resolution.candidates} == {
        "draft-a",
        "draft-b",
    }


@pytest.mark.asyncio
async def test_context_keeps_completed_task_referenceable() -> None:
    assembler = ContextAssembler(
        _FakeBuilder(_snapshot_with_two_tasks()),
        budget=TurnBudget(),
    )
    # No explicit focus -> the conversation's own task set is kept, including
    # the COMPLETED task (so it can still be modified in a follow-up turn).
    assembled = await assembler.assemble(
        conversation_id="c1", user_id="u1", tenant_id="t1",
    )
    ids = {item["task_id"] for item in assembled.selected_tasks}
    assert {"task-java", "task-agent"} <= ids


@pytest.mark.asyncio
async def test_context_snapshot_has_command_context_projection() -> None:
    assembler = ContextAssembler(
        _FakeBuilder(_snapshot_with_two_tasks()),
        budget=TurnBudget(),
    )
    assembled = await assembler.assemble(
        conversation_id="c1", user_id="u1", tenant_id="t1",
    )
    cc = assembled.to_command_context()
    assert cc.conversation_id == "c1"


# ── FastPathExecutor (reads + writes, no fabricated verification) ───────


def _fast_executor(*, read=None, submit=None, tools=None, activity=None) -> FastPathExecutor:
    return FastPathExecutor(
        tool_registry=tools or [StubTool.update_draft()],
        read_handler=read,
        write_submitter=submit,
        activity_callback=activity,
    )


def _turn_request() -> TurnRequest:
    return TurnRequest(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="改标题",
        run_id="run1",
        trace_id="trace1",
        session=None,
        auth=None,
        mcp=None,
    )


def _assembled() -> AssembledTurnContext:
    return AssembledTurnContext(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        timezone="Asia/Shanghai",
        snapshot=ContextSnapshot(conversation_id="c1"),
    )


@pytest.mark.parametrize(
    ("tool_fields", "provided", "expected"),
    [
        (
            {"post_id": {}},
            {"post_id": "post-java", "post_title": "Java", "summary_points": ["x"]},
            {"post_id": "post-java"},
        ),
        (
            {"post_id": {}, "cursor": {}, "size": {}},
            {"post_id": "post-java", "title": "ignored", "size": 10},
            {"post_id": "post-java", "size": 10},
        ),
        (
            {"schedule_id": {}, "run_at": {}, "timezone": {}, "temporal_base": {}},
            {"schedule_id": "schedule-java", "run_at": "2026-08-25T12:00:00Z", "objective_id": "wrong"},
            {"schedule_id": "schedule-java", "run_at": "2026-08-25T12:00:00Z"},
        ),
    ],
)
def test_fast_path_filters_provider_fields_by_selected_tool_contract(
    tool_fields: dict[str, object],
    provided: dict[str, object],
    expected: dict[str, object],
) -> None:
    tool = SimpleNamespace(input_schema={"type": "object", "properties": tool_fields})
    assert FastPathExecutor._filter_arguments_to_tool_schema(tool, provided) == expected


def test_fast_path_filter_never_invents_missing_required_resource() -> None:
    tool = SimpleNamespace(input_schema={"type": "object", "properties": {"post_id": {}}})
    assert FastPathExecutor._filter_arguments_to_tool_schema(
        tool, {"post_title": "Java", "summary_points": ["x"]}
    ) == {}


def test_fast_path_filter_preserves_uncontracted_test_boundary() -> None:
    tool = SimpleNamespace(input_schema={"type": "object"})
    provided = {"post_id": "post-java", "provider_note": "keep boundary"}
    assert FastPathExecutor._filter_arguments_to_tool_schema(tool, provided) == provided


@pytest.mark.asyncio
async def test_fast_read_projects_real_response_and_emits_activity() -> None:
    events: list[dict] = []

    async def read(tool_name=None, arguments=None, request=None):
        return {"ok": True, "content": "已找到 3 篇草稿", "draft_id": "draft-java"}

    executor = _fast_executor(
        read=read,
        tools=[StubTool.list_drafts()],
        activity=lambda event, payload: events.append({"event": event, **payload}),
    )
    from greenbook_agent_core.turn import FastPathDecision

    decision = FastPathDecision(route=TurnRoute.QUERY, semantic_actions=["LIST_DRAFTS"], reason="single_read")
    result = await executor.execute(
        decision,
        _command(ctype=CommandType.QUERY, required_capabilities=["LIST_DRAFTS"]),
        context=_assembled(),
        request=_turn_request(),
    )
    assert result.status == "COMPLETED"
    assert result.success
    assert "已找到 3 篇草稿" in result.content
    assert any(ev["event"] == "FAST_READ" and ev["draft_id"] == "draft-java" for ev in events)


@pytest.mark.asyncio
async def test_fast_read_preserves_structured_user_projection_from_read_boundary() -> None:
    interaction = {
        "kind": "QUERY_RESULT",
        "result": {
            "type": "SEARCH_RESULTS",
            "status": "SUCCESS",
            "title": "找到 9 篇相关内容",
            "search": {
                "count": 9,
                "items": [{
                    "id": "post-java-1",
                    "title": "Java 集合详解",
                    "summary": "从 List 到 Map。",
                    "href": "/post/post-java-1",
                }],
            },
        },
    }

    async def read(tool_name=None, arguments=None, request=None):
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id="run1",
            trace_id="trace1",
            content="找到 9 篇相关内容",
            summary="找到 9 篇相关内容",
            artifacts=[{"ok": True, "content": "找到 9 篇相关内容"}],
            partial_results={"user_facing_interaction": interaction},
        )

    executor = _fast_executor(
        read=read,
        tools=[StubTool.list_drafts()],
    )
    from greenbook_agent_core.turn import FastPathDecision

    result = await executor.execute(
        FastPathDecision(route=TurnRoute.QUERY, semantic_actions=["LIST_DRAFTS"], reason="single_read"),
        _command(ctype=CommandType.QUERY, required_capabilities=["LIST_DRAFTS"]),
        context=_assembled(),
        request=_turn_request(),
    )

    assert result.partial_results["user_facing_interaction"] == interaction
    assert result.content == "找到 9 篇相关内容"


@pytest.mark.asyncio
async def test_rc03_ambiguous_destructive_target_is_fail_closed() -> None:
    writes: list[dict] = []
    command = Command(
        type=CommandType.CANCEL,
        semantic_operation="DELETE_POST",
        target=CommandTarget(kind=TargetKind.POST, reference_type="ACTIVE"),
        required_capabilities=["DELETE_POST"],
    )
    resolution = TargetResolver().resolve(command, CommandContext(targets=[
        {"kind": "POST", "id": "post-a", "task_id": "task-a"},
        {"kind": "POST", "id": "post-b", "task_id": "task-b"},
    ]))
    decision = FastPathGate().decide(command, target_resolution=resolution)

    async def submit(**kwargs):
        writes.append(kwargs)
        return {"ok": True, "execution_id": "must-not-exist"}

    assert resolution.status.value == "AMBIGUOUS"
    assert decision.route == TurnRoute.CLARIFY
    # The runtime does not invoke FastPathExecutor after a clarification gate.
    assert writes == []


@pytest.mark.asyncio
async def test_rc03_unique_target_executes_once_and_keeps_typed_identity() -> None:
    writes: list[dict] = []
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="UPDATE_DRAFT",
        target=CommandTarget(
            kind=TargetKind.DRAFT,
            id="draft-java",
            reference_type="IDENTIFIER",
        ),
        required_capabilities=["MANAGE_DRAFT"],
    )
    resolution = TargetResolver().resolve(command, CommandContext(targets=[
        {"kind": "DRAFT", "id": "draft-java", "task_id": "task-java"},
        {"kind": "POST", "id": "post-java", "task_id": "task-java"},
    ]))
    decision = FastPathGate().decide(command, target_resolution=resolution)

    async def submit(**kwargs):
        writes.append(kwargs)
        return {"ok": True, "execution_id": "execution-java"}

    executor = _fast_executor(submit=submit)
    result = await executor.execute(
        decision,
        command,
        context=_assembled(),
        request=_turn_request(),
        target_resolution=resolution,
    )

    assert resolution.status.value == "RESOLVED"
    assert decision.route == TurnRoute.FAST
    assert result.success is True
    assert len(writes) == 1
    assert writes[0]["arguments"]["draft_id"] == "draft-java"


@pytest.mark.asyncio
async def test_fast_read_accepts_canonical_runtime_result_projection() -> None:
    """The production read boundary may return RuntimeResult, not only a dict."""
    async def read(**_kwargs):
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            content="3 posts",
            artifacts=[{"ok": True, "content": "3 posts", "data": {"total": 3}}],
        )

    executor = _fast_executor(read=read, tools=[StubTool.list_drafts()])
    from greenbook_agent_core.turn import FastPathDecision

    result = await executor.execute(
        FastPathDecision(route=TurnRoute.QUERY, semantic_actions=["LIST_DRAFTS"], reason="query"),
        _command(ctype=CommandType.QUERY, required_capabilities=["LIST_DRAFTS"]),
        context=_assembled(),
        request=_turn_request(),
    )
    assert result.status == "COMPLETED"
    assert result.content == "3 posts"


@pytest.mark.asyncio
async def test_fast_write_routes_to_durable_submission_boundary() -> None:
    submitted: list[dict] = []

    async def submit(tool_name=None, arguments=None, capability=None, semantic_action=None,
                     command=None, request=None):
        submitted.append({
            "tool": tool_name, "capability": capability, "semantic_action": semantic_action,
            "arguments": arguments,
        })
        return RuntimeResult(
            success=True, status="COMPLETED", run_id="run1",
            execution_id="exec-1", execution_path="fast_path",
            content="草稿已更新", summary="草稿已更新",
        )

    executor = _fast_executor(
        submit=submit,
        tools=[StubTool.update_draft()],
    )
    from greenbook_agent_core.turn import FastPathDecision

    decision = FastPathDecision(route=TurnRoute.FAST, semantic_actions=["UPDATE_DRAFT"], reason="single_explicit_write")
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[_update_draft_delta(title="Java 并发指南", draft_id="draft-java")],
    )
    result = await executor.execute(
        decision,
        command,
        context=_assembled(),
        request=_turn_request(),
        target_resolution=_resolved_task(),
    )
    assert result.status == "COMPLETED"
    assert result.execution_id == "exec-1"
    assert submitted, "durable write submission boundary must be called"
    assert submitted[0]["semantic_action"] == "UPDATE_DRAFT"
    assert submitted[0]["arguments"].get("title") == "Java 并发指南"


@pytest.mark.asyncio
async def test_fast_write_approval_required_submits_durable_execution() -> None:
    submitted = []

    async def submit(**kwargs):
        submitted.append(True)
        return RuntimeResult(
            success=False,
            status="WAITING_APPROVAL",
            execution_id="exec-approval",
            approval_id="approval-1",
        )

    executor = _fast_executor(
        submit=submit,
        tools=[StubTool.publish_now()],  # requires approval in its policy
    )
    from greenbook_agent_core.turn import FastPathDecision

    decision = FastPathDecision(route=TurnRoute.FAST, semantic_actions=["PUBLISH_NOW"], reason="single_explicit_write")
    command = _command(
        ctype=CommandType.MODIFY,
        task_changes=[_update_draft_delta(semantic_action="PUBLISH_NOW", draft_id="draft-java")],
    )
    result = await executor.execute(
        decision,
        command,
        context=_assembled(),
        request=_turn_request(),
        target_resolution=_resolved_task(),
    )
    assert result.status == "WAITING_APPROVAL"
    assert result.execution_id == "exec-approval"
    assert result.approval_id == "approval-1"
    assert submitted, "an approval-gated write must reach the canonical durable boundary"


# ── TurnCoordinator integration (stubbed, no real IO) ───────────────────

@pytest.mark.asyncio
async def test_turn_coordinator_chat_creates_no_execution() -> None:
    from greenbook_agent_api.services.turn_coordinator import TurnCoordinator

    class _StubInterpreter:
        async def interpret(self, text, context, **kwargs):
            return _command(ctype=CommandType.QUERY, required_capabilities=[])

    class _StubComplex:
        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return RuntimeResult(success=True, status="COMPLETED")

    complex_path = _StubComplex()
    coordinator = TurnCoordinator(
        command_runtime=_StubInterpreter(),
        complex_path=complex_path,
        target_resolver=type("TR", (), {"resolve": lambda self, c, x: None})(),
    )
    result = await coordinator.execute(
        conversation_id="c1", user_id="u1", tenant_id="t1", message="你好",
    )
    assert result.status == "COMPLETED"
    assert result.execution_path == "fast_path"
    assert complex_path.calls == 0, "CHAT must not touch the Complex Path or create an execution"
