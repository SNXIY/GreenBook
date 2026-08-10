"""Runtime IntentSpecProvider boundary tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_assistant_core.task.intent_models import ActionType, IntentMode
from greenbook_assistant_core.task.intent_spec_provider import (
    IntentSpecProvider,
    IntentSpecProviderError,
)
from greenbook_assistant_core.task.models import TaskIntent


class _SequenceCompletions:
    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0

    async def create(self, **kwargs):
        del kwargs
        content = self._contents[self.calls]
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


class _SequenceLLM:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(
            completions=_SequenceCompletions(contents),
        )


class _StubUnderstanding:
    def __init__(self, intent: TaskIntent) -> None:
        self.intent = intent

    async def understand(self, user_message, *, existing_tasks=None):
        del user_message, existing_tasks
        return self.intent


@pytest.mark.asyncio
async def test_l1_create_content_returns_validated_intent_spec() -> None:
    message = "帮我写一篇AI Agent学习路线帖子"

    spec = await IntentSpecProvider().resolve(message)

    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions[0].action == ActionType.CREATE
    assert spec.actions[0].resource.value == "CONTENT"
    assert spec.source == "L1"
    assert spec.goal == message


@pytest.mark.asyncio
async def test_direct_l2_uses_existing_parse_validation_and_repair_path() -> None:
    initial_invalid = {
        "mode": "SIMPLE",
        "goal": "发布前确认",
        "actions": [],
        "conditions": [],
        "constraints": [],
        "target_hint": None,
        "confidence": 0.9,
    }
    repaired = {
        "mode": "SIMPLE",
        "goal": "发布前确认",
        "actions": [{"action": "PUBLISH", "resource": "CONTENT"}],
        "conditions": [],
        "constraints": [{"type": "APPROVAL", "value": "BEFORE_PUBLISH"}],
        "target_hint": None,
        "confidence": 0.9,
    }
    llm = _SequenceLLM([
        json.dumps(initial_invalid),
        json.dumps(repaired),
    ])

    spec = await IntentSpecProvider(llm=llm, model="test-model").resolve(
        "发布前让我确认",
    )

    assert spec.source == "L2"
    assert spec.actions[0].action == ActionType.PUBLISH
    assert spec.constraints[0].type.value == "APPROVAL"
    assert llm.chat.completions.calls == 2


@pytest.mark.asyncio
async def test_invalid_spec_is_rejected_after_schema_and_semantic_validation() -> None:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal="创建文章",
        goal_category="CREATE_CONTENT",
        source="L2",
        intent_spec={
            "mode": "SIMPLE",
            "goal": "创建文章",
            "actions": [],
            "conditions": [],
            "constraints": [],
            "target_hint": None,
            "confidence": 0.9,
        },
    )
    provider = IntentSpecProvider(_StubUnderstanding(intent))

    with pytest.raises(IntentSpecProviderError) as exc_info:
        await provider.resolve("创建文章")

    assert exc_info.value.code == "INTENT_VALIDATION_FAILED"
    assert provider.last_validation_result is not None
    assert provider.last_validation_result.is_valid is False


@pytest.mark.asyncio
async def test_schema_invalid_spec_is_rejected() -> None:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal="创建文章",
        goal_category="CREATE_CONTENT",
        source="L2",
        intent_spec={
            "mode": "SIMPLE",
            "goal": "创建文章",
            "actions": [{"action": "NOT_AN_ACTION", "resource": "CONTENT"}],
        },
    )
    provider = IntentSpecProvider(_StubUnderstanding(intent))

    with pytest.raises(IntentSpecProviderError) as exc_info:
        await provider.resolve("创建文章")

    assert exc_info.value.code == "INTENT_SPEC_INVALID"


@pytest.mark.asyncio
async def test_l2_task_intent_without_formal_spec_never_uses_compatibility_fallback() -> None:
    legacy_only = TaskIntent(
        relation="NEW_TASK",
        goal="创建文章",
        goal_category="CREATE_CONTENT",
        source="L2",
        intent_spec=None,
    )
    provider = IntentSpecProvider(_StubUnderstanding(legacy_only))

    with pytest.raises(IntentSpecProviderError) as exc_info:
        await provider.resolve("创建文章")

    assert exc_info.value.code == "INTENT_SPEC_UNAVAILABLE"
