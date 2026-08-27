"""Fast-track acceptance tests for the canonical Command Runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_core.command import (
    Command,
    CommandContext,
    CommandInterpreter,
    CommandType,
    TargetKind,
)
from greenbook_agent_core.command.interpreter import (
    _input_spans,
    _materialize_request_publication_constraints,
    _normalize_delete_post,
    _normalize_draft_only,
    _normalize_multi_objective_items,
)
from greenbook_agent_core.command.models import (
    CommandItem,
    StructuredCommandOutput,
    TaskDeltaOperation,
)


class _FakeCompletions:
    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = iter(payloads)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = next(self._payloads)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=str_json(payload)))]
        )


class _FakeLLM:
    def __init__(self, payloads: list[dict]) -> None:
        self.completions = _FakeCompletions(payloads)
        self.chat = SimpleNamespace(completions=self.completions)


def test_explicit_draft_only_removes_hallucinated_publication() -> None:
    command = Command(
        type=CommandType.CREATE,
        goal="写一篇 Java 学习短帖",
        first_action="GENERATE_CONTENT",
        request_complexity="COMPLEX",
        required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
        constraints={"run_at": "tomorrow", "publication_intent": "DRAFT_ONLY"},
        items=[CommandItem(title="Java", capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"])],
    )

    _normalize_draft_only(command, "任意语言文本")

    assert command.required_capabilities == ["GENERATE_CONTENT"]
    assert command.first_action == "GENERATE_CONTENT"
    assert command.request_complexity == "SIMPLE"
    assert command.constraints == {}
    assert command.items[0].capabilities == ["GENERATE_CONTENT"]


def test_draft_only_preserves_explicit_search_prerequisite() -> None:
    command = Command(
        type=CommandType.CREATE,
        goal="鎼滅储 Agent 瀛︿範鍐呭鍚庝繚瀛樿崏绋?",
        first_action="SEARCH_COMMUNITY",
        required_capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT", "PUBLISH_NOW"],
        constraints={"publication_intent": "DRAFT_ONLY"},
        items=[
            CommandItem(
                title="Agent",
                capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT", "PUBLISH_NOW"],
            )
        ],
    )

    _normalize_draft_only(command, "structured request")

    assert command.required_capabilities == ["SEARCH_COMMUNITY", "GENERATE_CONTENT"]
    assert command.first_action == "SEARCH_COMMUNITY"
    assert command.items[0].capabilities == ["SEARCH_COMMUNITY", "GENERATE_CONTENT"]


def test_delete_post_normalization_does_not_add_publish_mutation() -> None:
    command = Command(
        type=CommandType.CANCEL,
        goal="delete the post",
        semantic_operation="DELETE",
        target={"kind": "POST", "resource_id": "post-1"},
    )

    _normalize_delete_post(command)

    assert [
        str((delta.desired_changes or {}).get("semantic_action") or "").upper()
        for delta in command.task_changes
    ] == ["DELETE_POST"]


def test_multi_objective_item_normalization_preserves_atomic_deliverables() -> None:
    single = StructuredCommandOutput(command=CommandType.CREATE, items=[
        CommandItem(title="Java", capabilities=["GENERATE_CONTENT"], temporal_text="明天9点")
    ])
    assert len(_normalize_multi_objective_items(single).items) == 1

    split = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "items": [{"title": "merged", "capabilities": ["GENERATE_CONTENT"]}],
        "task_changes": [
            {"operation": "CREATE_TASK", "desired_changes": {
                "title": "Java", "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], "run_at": "明天9点",
            }},
            {"operation": "CREATE_TASK", "desired_changes": {
                "title": "Agent", "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], "run_at": "明天14点",
            }},
        ],
    })
    items = _normalize_multi_objective_items(split).items
    assert [item.title for item in items] == ["Java", "Agent"]

    pipeline = StructuredCommandOutput(command=CommandType.CREATE, items=[
        CommandItem(title="Java", capabilities=["SEARCH_COMMUNITY", "ANALYZE_CONTENT_PATTERNS", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"])
    ])
    assert len(_normalize_multi_objective_items(pipeline).items) == 1

    parallel = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "items": [{"title": "merged", "constraints": {
            "titles": ["Java", "Agent"], "publish_times": ["9点", "14点"]
        }}],
    })
    assert len(_normalize_multi_objective_items(parallel).items) == 2

    embedded = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "task_changes": [{"operation": "CREATE_TASK", "desired_changes": {
            "titles": ["Java", "Agent"],
            "publish_times": ["09:00", "14:00"],
        }}],
    })
    embedded_items = _normalize_multi_objective_items(embedded).items
    assert [item.title for item in embedded_items] == ["Java", "Agent"]
    assert [item.temporal_text for item in embedded_items] == ["09:00", "14:00"]


class _JsonSchemaFallbackCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("400 invalid_request_error: response_format type is unavailable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=str_json(self.payload)))],
        )


class _JsonSchemaFallbackLLM:
    def __init__(self, payload: dict) -> None:
        self.completions = _JsonSchemaFallbackCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


def str_json(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


@pytest.mark.asyncio
async def test_create_java_article_returns_create_command() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "objective": "创建一篇Java文章",
            "target": None,
            "parameters": {"topic": "Java"},
            "confidence": 0.98,
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "创建一篇Java文章"
    )

    assert command.type == CommandType.CREATE
    assert command.command == CommandType.CREATE
    assert llm.completions.calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_deliverable_segmentation_maps_items_without_per_item_calls() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "items": [{"title": "merged", "capabilities": ["GENERATE_CONTENT"]}],
        },
        {
            "assignments": [{"span_id": 1, "group_id": "A"}, {"span_id": 2, "group_id": "B"}]
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "search Java, write article. search Agent, write article."
    )

    assert len(command.items) == 2
    assert len(llm.completions.calls) == 2


@pytest.mark.asyncio
async def test_explicit_items_are_not_collapsed_by_span_grouping() -> None:
    """Raw span grouping must not erase extraction-owned item outcomes."""

    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "required_capabilities": [
                "GENERATE_CONTENT",
                "PUBLISH_NOW",
                "SCHEDULE_PUBLISH",
            ],
            "items": [
                {
                    "topic": "Java",
                    "capabilities": ["GENERATE_CONTENT", "PUBLISH_NOW"],
                    "constraints": {"publication_intent": "PUBLISH_NOW"},
                },
                {
                    "topic": "Agent",
                    "capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
                    "temporal_text": "five minutes later",
                },
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "write Java now and Agent five minutes later",
    )

    assert len(command.items) == 2
    assert [item.topic for item in command.items] == ["Java", "Agent"]
    assert [
        item.constraints["publication_intent"] for item in command.items
    ] == ["IMMEDIATE_PUBLISH", "SCHEDULED_PUBLISH"]
    assert len(llm.completions.calls) == 1


@pytest.mark.asyncio
async def test_mixed_read_and_mutation_does_not_span_group_create_items() -> None:
    """Existing mutations must not duplicate the independent read item."""
    goal = "delete post 350238090952052736 after approval; also search recent Java posts"
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "goal": goal,
            "objective": goal,
            "request_complexity": "COMPLEX",
            "task_changes": [{
                "operation": "UPDATE_GOAL",
                "change_id": "delete_post",
                "target_reference": {
                    "kind": "POST",
                    "id": "350238090952052736",
                    "reference_type": "IDENTIFIER",
                },
                "desired_changes": {
                    "semantic_action": "DELETE_POST",
                    "requires_approval": True,
                },
                "needs_target_resolution": False,
            }],
            "required_capabilities": ["DELETE_POST", "SEARCH_COMMUNITY"],
            "semantic_operation": "DELETE_AND_SEARCH",
            "items": [{
                "item_key": "search_java",
                "topic": "Java",
                "requirements": ["search recent Java posts"],
                "operation": "CREATE",
                "capabilities": ["SEARCH_COMMUNITY"],
            }],
        },
        {
            "deliverables": [{
                "entity_type": "search",
                "item_key": "search_java",
                "topic": "Java",
                "requirements": ["search recent Java posts"],
                "operation_hint": "CREATE",
            }],
        },
        {
            "deliverables": [{
                "entity_type": "search",
                "item_key": "search_java",
                "topic": "Java",
                "requirements": ["search recent Java posts"],
                "operation_hint": "CREATE",
            }],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(goal)

    assert len(command.items) == 1
    assert command.items[0].item_key == "search_java"
    assert command.items[0].capabilities == ["SEARCH_COMMUNITY"]
    assert len(command.task_changes) == 1
    assert command.task_changes[0].operation == TaskDeltaOperation.UPDATE_GOAL
    assert command.task_changes[0].desired_changes["semantic_action"] == "DELETE_POST"
    schema_names = [
        call["response_format"]["json_schema"]["name"]
        for call in llm.completions.calls
    ]
    assert schema_names == [
        "greenbook_command",
        "greenbook_deliverable_segmentation",
        "greenbook_deliverable_segmentation_repair",
    ]


@pytest.mark.asyncio
async def test_immediate_publish_span_marker_is_not_temporal_evidence() -> None:
    text = "write a Java post and publish now. marker E2E-20260821-123456."
    spans = _input_spans(text)
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "SIMPLE",
            "constraints": {"publication_intent": "PUBLISH_NOW"},
            "items": [
                {
                    "topic": "Java",
                    "capabilities": ["GENERATE_CONTENT", "PUBLISH_NOW"],
                    "constraints": {"publication_intent": "PUBLISH_NOW"},
                },
            ],
        },
        {
            "assignments": [
                {"span_id": span.span_id, "group_id": "A"}
                for span in spans
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(text)

    assert len(spans) > 1
    assert command.items[0].temporal_text == ""


@pytest.mark.asyncio
async def test_shared_unresolved_publication_constraint_survives_segmentation() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
            "needs_clarification": True,
        },
        {
            "deliverables": [
                {"operation_hint": "CREATE"},
                {"operation_hint": "CREATE"},
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "create two articles with publication times",
    )

    assert len(command.items) == 2
    assert command.needs_clarification is True
    assert command.constraints["publication_intent"] == "SCHEDULED_PUBLISH"
    assert command.required_capabilities == ["SCHEDULE_PUBLISH"]
    assert all(
        item.constraints["publication_intent"] == "SCHEDULED_PUBLISH"
        and "SCHEDULE_PUBLISH" in item.capabilities
        and "PUBLISH_NOW" not in item.capabilities
        and item.temporal_text == ""
        for item in command.items
    )


def test_request_level_unresolved_publication_materializes_single_item() -> None:
    structured = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
        "items": [{"title": "Java"}],
    })

    materialized = _materialize_request_publication_constraints(structured)

    assert materialized.needs_clarification is True
    assert materialized.items[0].constraints["publication_intent"] == "SCHEDULED_PUBLISH"
    assert "SCHEDULE_PUBLISH" in materialized.items[0].capabilities
    assert materialized.items[0].temporal_text == ""


@pytest.mark.parametrize("item_count", [2, 3])
def test_shared_unresolved_publication_preserves_item_cardinality(item_count: int) -> None:
    structured = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
        "items": [{"topic": f"item-{index}"} for index in range(item_count)],
    })

    materialized = _materialize_request_publication_constraints(structured)

    assert len(materialized.items) == item_count
    assert all(
        item.constraints["publication_intent"] == "SCHEDULED_PUBLISH"
        and item.temporal_text == ""
        for item in materialized.items
    )


def test_draft_only_multi_deliverable_does_not_gain_schedule_requirement() -> None:
    structured = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "constraints": {"publication_intent": "DRAFT_ONLY"},
        "items": [
            {"topic": "Java", "constraints": {"publication_intent": "DRAFT_ONLY"}},
            {"topic": "Agent", "constraints": {"publication_intent": "DRAFT_ONLY"}},
        ],
    })

    materialized = _materialize_request_publication_constraints(structured)

    assert len(materialized.items) == 2
    assert all(item.constraints["publication_intent"] == "DRAFT_ONLY" for item in materialized.items)
    assert all("SCHEDULE_PUBLISH" not in item.capabilities for item in materialized.items)


def test_item_scoped_schedule_does_not_broadcast_to_unowned_item() -> None:
    structured = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "items": [
            {"topic": "draft", "constraints": {"publication_intent": "DRAFT_ONLY"}},
            {"topic": "scheduled", "constraints": {"publication_intent": "SCHEDULED_PUBLISH"}},
        ],
    })

    materialized = _materialize_request_publication_constraints(structured)

    assert materialized.items[0].constraints["publication_intent"] == "DRAFT_ONLY"
    assert materialized.items[1].constraints["publication_intent"] == "SCHEDULED_PUBLISH"
    assert "SCHEDULE_PUBLISH" not in materialized.items[0].capabilities
    assert "SCHEDULE_PUBLISH" in materialized.items[1].capabilities


def test_mixed_draft_and_scheduled_items_keep_publication_ownership() -> None:
    structured = StructuredCommandOutput.model_validate({
        "command": "CREATE",
        "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        "items": [
            {
                "title": "draft",
                "capabilities": ["GENERATE_CONTENT"],
                "constraints": {"publication_intent": "DRAFT_ONLY"},
            },
            {
                "title": "scheduled",
                "capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
            },
        ],
    })

    materialized = _materialize_request_publication_constraints(structured)

    assert materialized.items[0].constraints["publication_intent"] == "DRAFT_ONLY"
    assert "SCHEDULE_PUBLISH" not in materialized.items[0].capabilities
    assert materialized.items[1].constraints["publication_intent"] == "SCHEDULED_PUBLISH"
    assert "SCHEDULE_PUBLISH" in materialized.items[1].capabilities


@pytest.mark.asyncio
async def test_segmentation_preserves_mixed_publication_ownership() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        },
        {
            "deliverables": [
                {
                    "operation_hint": "CREATE",
                    "topic": "draft",
                    "constraints": {"publication_intent": "DRAFT_ONLY"},
                },
                {
                    "operation_hint": "CREATE",
                    "topic": "scheduled",
                    "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
                },
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "write one draft and schedule another",
    )

    assert len(command.items) == 2
    assert command.items[0].constraints["publication_intent"] == "DRAFT_ONLY"
    assert "SCHEDULE_PUBLISH" not in command.items[0].capabilities
    assert command.items[1].constraints["publication_intent"] == "SCHEDULED_PUBLISH"
    assert "SCHEDULE_PUBLISH" in command.items[1].capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("deliverable_count", [2, 3])
async def test_complex_create_preserves_explicit_deliverable_cardinality(
    deliverable_count: int,
) -> None:
    """Placeholder segments still represent independent final outcomes."""

    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "items": [{"title": "merged", "capabilities": ["GENERATE_CONTENT"]}],
        },
        {
            "deliverables": [
                {"operation_hint": "CREATE"}
                for _ in range(deliverable_count)
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "create independent deliverables",
    )

    assert len(command.items) == deliverable_count
    assert len(llm.completions.calls) == 2


@pytest.mark.asyncio
async def test_complex_connected_workflow_remains_one_deliverable() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "items": [{
                "title": "summary",
                "capabilities": ["SEARCH_COMMUNITY", "GENERATE_CONTENT"],
            }],
        },
        {
            "deliverables": [{
                "text": "search then write one summary",
                "entity_type": "article",
                "requirements": ["use search results"],
            }],
        },
        {
            "deliverables": [{
                "text": "search then write one summary",
                "entity_type": "article",
                "requirements": ["use search results"],
            }],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "search sources then write one summary",
    )

    assert len(command.items) == 1
    assert len(llm.completions.calls) == 3


@pytest.mark.asyncio
async def test_typed_draft_and_schedule_segments_remain_one_connected_deliverable() -> None:
    """A schedule is a dependent lifecycle step, not a second CREATE target."""
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
            "items": [{
                "title": "Java reliability",
                "topic": "Java reliability",
                "capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                "temporal_text": "tomorrow 10:00",
                "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
            }],
        },
        {
            "deliverables": [
                {
                    "entity_type": "draft",
                    "title": "Java reliability",
                    "topic": "Java reliability",
                    "requirements": ["create the draft"],
                },
                {
                    "entity_type": "schedule",
                    "title": "Java reliability",
                    "topic": "Java reliability",
                    "requirements": ["schedule the same draft"],
                    "temporal_text": "tomorrow 10:00",
                },
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "create a Java reliability draft and schedule the same draft tomorrow 10:00",
    )

    assert len(command.items) == 1
    item = command.items[0]
    assert item.title == "Java reliability"
    assert item.temporal_text == "tomorrow 10:00"
    assert item.capabilities == ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    assert item.requirements == ["create the draft", "schedule the same draft"]
    assert len(llm.completions.calls) == 2


@pytest.mark.asyncio
async def test_typed_draft_and_schedule_segments_with_different_identity_stay_separate() -> None:
    """Matching types alone must not collapse independent business targets."""
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "request_complexity": "COMPLEX",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
            "items": [{"capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]}],
        },
        {
            "deliverables": [
                {"entity_type": "draft", "title": "Java", "topic": "Java"},
                {"entity_type": "schedule", "title": "Agent", "topic": "Agent", "temporal_text": "tomorrow 10:00"},
            ],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "create Java and schedule Agent tomorrow 10:00",
    )

    assert len(command.items) == 2


@pytest.mark.asyncio
async def test_unsupported_json_schema_uses_json_object_fallback() -> None:
    llm = _JsonSchemaFallbackLLM(
        {
            "command": "CREATE",
            "objective": "总结时间管理帖子",
            "target": None,
            "parameters": {},
            "confidence": 0.9,
        },
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "帮我总结时间管理帖子",
    )

    assert command.type == CommandType.CREATE
    assert [call["response_format"]["type"] for call in llm.completions.calls] == [
        "json_schema",
        "json_object",
    ]
    assert '"entities"' in llm.completions.calls[1]["messages"][0]["content"]
    assert '"required_capabilities"' in llm.completions.calls[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_modify_previous_target_is_resolved_from_active_context() -> None:
    llm = _FakeLLM([
        {
            "command": "MODIFY",
            "objective": "调整发布时间",
            "target": {
                "kind": "SCHEDULE",
                "reference_type": "ACTIVE",
            },
            "parameters": {"run_at": "22:00"},
            "confidence": 0.94,
        }
    ])
    context = CommandContext(
        active_target={"kind": "SCHEDULE", "id": "schedule-java"},
        targets=[{"kind": "SCHEDULE", "id": "schedule-java", "status": "SCHEDULED"}],
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "把刚才那个改成晚上10点",
        context,
    )

    assert command.type == CommandType.MODIFY
    assert command.target is not None
    assert command.target.id == "schedule-java"
    assert command.target_resolution == "RESOLVED"


@pytest.mark.asyncio
async def test_cancel_yesterday_schedule_returns_cancel_command() -> None:
    llm = _FakeLLM([
        {
            "command": "CANCEL",
            "objective": "取消昨天安排的发布任务",
            "target": {
                "kind": "SCHEDULE",
                "id": "schedule-yesterday",
                "reference_type": "IDENTIFIER",
            },
            "parameters": {},
            "confidence": 0.96,
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "取消昨天安排的发布任务",
        CommandContext(
            targets=[
                {
                    "kind": TargetKind.SCHEDULE,
                    "id": "schedule-yesterday",
                    "status": "SCHEDULED",
                }
            ]
        ),
    )

    assert command.type == CommandType.CANCEL
    assert command.target_exists is True
    assert command.target_resolution == "RESOLVED"


@pytest.mark.asyncio
async def test_interpret_tolerates_echoed_envelope_fields() -> None:
    """Real-chain regression: a reasoning model echoes envelope fields
    (source/version/tasks) while expressing a multi-task message; the owned
    fields — including task_changes — must still validate instead of failing
    the whole request with COMMAND_SCHEMA_INVALID."""
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "goal": "安排三篇帖子并发布",
            "objective": "安排三篇帖子并发布",
            "first_action": "SEARCH_COMMUNITY",
            "request_complexity": "COMPLEX",
            "required_capabilities": ["SEARCH_COMMUNITY", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "task_changes": [
                {
                    "operation": "CREATE_TASK",
                    "desired_changes": {
                        "description": "Java 面试帖子",
                        "required_capabilities": ["SEARCH_COMMUNITY", "GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                        "constraints": {"run_at": "2026-08-15T09:00:00+08:00"},
                    },
                }
            ],
            # Model-echoed fields the Command schema forbids:
            "tasks": [{"title": "Java 面试"}],
            "source": "LLM_STRUCTURED_OUTPUT",
            "version": 1,
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "写三篇帖子并安排发布",
        CommandContext(),
    )

    assert command.type == CommandType.CREATE
    assert len(command.task_changes) == 1
    assert command.task_changes[0].operation == TaskDeltaOperation.CREATE_TASK


def test_strip_unknown_command_fields_keeps_owned_keys() -> None:
    from greenbook_agent_core.command.interpreter import (
        _strip_unknown_command_fields,
    )

    payload = {
        "command": "CREATE",
        "goal": "多任务",
        "task_changes": [{"operation": "CREATE_TASK"}],
        "tasks": [{"title": "x"}],
        "source": "LLM",
    }
    stripped = _strip_unknown_command_fields(payload)
    assert "tasks" not in stripped
    assert "source" not in stripped
    assert stripped["command"] == "CREATE"
    assert stripped["task_changes"] == payload["task_changes"]
    # Already-valid payloads are returned unchanged.


# ── deterministic repair of common schema violations ───────────────────────


@pytest.mark.asyncio
async def test_interpret_normalizes_publish_command_variant() -> None:
    """Regression: '发布一篇…五分钟之后发布' made the model emit
    command=PUBLISH, which is outside the schema enum.  The deterministic
    repair must map it to CREATE instead of failing with
    COMMAND_SCHEMA_INVALID."""
    llm = _FakeLLM([
        {
            "command": "PUBLISH",
            "goal": "发布一篇如何学习agent的沙箱管理的帖子",
            "first_action": "GENERATE_CONTENT",
            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            "constraints": {"run_at": "2026-08-14T13:44:51Z"},
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "发布一篇如何学习agent的沙箱管理的帖子，五分钟之后发布",
        CommandContext(),
    )

    assert command.type == CommandType.CREATE
    assert "SCHEDULE_PUBLISH" in command.required_capabilities
    assert command.constraints.get("run_at")


@pytest.mark.asyncio
async def test_interpret_repairs_delta_extra_fields_and_operation_variants() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "goal": "安排新任务",
            "task_changes": [
                {
                    "operation": "CREATE",  # variant of CREATE_TASK
                    "desired_changes": {"description": "写一篇文章"},
                    "bogus_field": "echoed",  # TaskDelta forbids extra
                }
            ],
            "constraints": "tomorrow",  # wrong container type
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "安排一个新任务",
        CommandContext(),
    )

    assert command.type == CommandType.CREATE
    assert command.task_changes[0].operation == TaskDeltaOperation.CREATE_TASK
    assert command.constraints == {}


@pytest.mark.asyncio
async def test_interpret_llm_repair_pass_recovers_after_both_deterministic_steps() -> None:
    """When strip + deterministic repair both fail, one bounded LLM repair pass
    with the concrete validation error must recover the request."""
    llm = _FakeLLM([
        {"command": "NOT_A_COMMAND", "goal": "坏输出"},
        {  # repair pass returns the corrected shape
            "command": "QUERY",
            "goal": "坏输出",
            "required_capabilities": [],
        },
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "坏输出",
        CommandContext(),
    )

    assert command.type == CommandType.QUERY
    # The repair pass carried the concrete validation error back to the model.
    repair_content = llm.completions.calls[1]["messages"][1]["content"]
    assert "contract_repair" in repair_content


def test_repair_command_output_normalizes_enum_and_containers() -> None:
    from greenbook_agent_core.command.interpreter import _repair_command_output

    repaired = _repair_command_output({
        "command": "SCHEDULE",
        "constraints": None,
        "parameters": "x",
        "required_capabilities": ["SEARCH_COMMUNITY", 3, None],
        "task_changes": [
            {"operation": "ADD", "extra": 1},
            "not-a-dict",
        ],
    })
    assert repaired["command"] == "CREATE"
    assert repaired["constraints"] == {}
    assert repaired["parameters"] == {}
    assert repaired["required_capabilities"] == ["SEARCH_COMMUNITY", "3"]
    assert repaired["task_changes"] == [{"operation": "ADD_GOAL"}]
    from greenbook_agent_core.command.interpreter import _strip_unknown_command_fields
    assert _strip_unknown_command_fields({"command": "QUERY"}) == {"command": "QUERY"}


@pytest.mark.asyncio
async def test_raw_text_cannot_override_structured_query_candidate() -> None:
    message = "Publish this draft immediately"
    llm = _FakeLLM([{
        "command": "QUERY",
        "goal": "\u67e5\u770b\u5f53\u524d\u4efb\u52a1\u7684\u72b6\u6001",
        "required_capabilities": [],
    }])
    context = CommandContext(
        active_target={"kind": "DRAFT", "id": "draft-1"},
        targets=[{"kind": "DRAFT", "id": "draft-1"}],
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(message, context)

    assert command.type == CommandType.QUERY
    assert command.semantic_operation == ""
    assert command.required_capabilities == []
    assert command.task_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "\u8fd9\u7bc7\u53d1\u5e03\u4e86\u5417",
        "\u73b0\u5728\u4ec0\u4e48\u72b6\u6001",
        "\u4ec0\u4e48\u65f6\u5019\u53d1\u5e03",
        "\u770b\u4e00\u4e0b\u53d1\u5e03\u72b6\u6001",
    ],
)
async def test_publication_status_questions_remain_query(message: str) -> None:
    llm = _FakeLLM([{
        "command": "QUERY",
        "goal": "\u67e5\u770b\u53d1\u5e03\u72b6\u6001",
        "required_capabilities": [],
    }])
    context = CommandContext(
        active_target={"kind": "DRAFT", "id": "draft-1"},
        targets=[{"kind": "DRAFT", "id": "draft-1"}],
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(message, context)

    assert command.type == CommandType.QUERY
    assert command.semantic_operation == ""
    assert command.required_capabilities == []
