"""Stage E-2.1 tests for validation issues and repair observability."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from greenbook_assistant_core.task.intent_models import (
    ActionType,
    IntentMode,
    IntentSpec,
)
from greenbook_assistant_core.task.intent_validator import IntentValidator
from greenbook_assistant_core.task.understanding import TaskUnderstanding


def test_empty_actions_detection() -> None:
    result = IntentValidator().validate(
        IntentSpec(mode=IntentMode.COMPOSITE),
        "search and publish",
    )

    assert result.needs_repair is True
    issue = next(issue for issue in result.issues if issue.type == "EMPTY_ACTIONS")
    assert issue.expected_fields == ["actions"]
    assert issue.suggestion == ["ADD_ACTION_FROM_MESSAGE"]


def test_missing_condition_detection() -> None:
    result = IntentValidator().validate(
        IntentSpec(
            mode=IntentMode.CONDITIONAL,
            actions=[],
        ),
        "conditional request",
    )

    assert result.needs_repair is True
    assert any(issue.type == "MISSING_CONDITION" for issue in result.issues)


def test_missing_approval_detection() -> None:
    spec = IntentSpec(
        actions=[],
    )
    with patch.object(IntentValidator, "_has_approval_text", return_value=True):
        result = IntentValidator().validate(spec, "publish after confirmation")

    issue_types = {issue.type for issue in result.issues}
    assert "MISSING_APPROVAL" in issue_types
    assert "MISSING_PUBLISH_ACTION" in issue_types


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages: list[dict[str, str]] = []

    async def create(self, **kwargs):
        self.last_messages = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class _SequenceCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def create(self, **kwargs):
        self.messages.append(kwargs["messages"])
        content = self.contents[self.calls]
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(content))


class _SequenceLLM:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(completions=_SequenceCompletions(contents))


class _RepairTaskUnderstanding(TaskUnderstanding):
    async def _llm_understand_direct_v2(self, text, existing_tasks=None):
        return IntentSpec(
            mode=IntentMode.SIMPLE,
            actions=[],
            confidence=0.9,
        )


@pytest.mark.asyncio
async def test_repair_trace_records_full_lifecycle() -> None:
    repaired = {
        "mode": "SIMPLE",
        "goal": "publish",
        "actions": [{"action": "PUBLISH", "resource": "CONTENT"}],
        "conditions": [],
        "constraints": [{"type": "APPROVAL", "value": "BEFORE_PUBLISH"}],
        "target_hint": None,
        "confidence": 0.9,
    }
    llm = _FakeLLM(json.dumps(repaired))
    tu = _RepairTaskUnderstanding(llm=llm, model="test")

    with patch.object(IntentValidator, "_has_approval_text", return_value=True):
        result = await tu._try_l2_v2("publish after confirmation")

    assert result is not None
    assert tu._repair_stats["attempts"] == 1
    assert tu._repair_stats["successes"] == 1
    assert len(tu.validation_traces) == 1

    trace = tu.validation_traces[0]
    assert trace.raw_intent_spec["actions"] == []
    assert trace.repair_triggered is True
    assert trace.repair_prompt is not None
    assert "MISSING_APPROVAL" in trace.repair_prompt
    assert trace.repair_response is not None
    assert trace.final_result is not None
    assert trace.final_result["actions"][0]["action"] == "PUBLISH"


@pytest.mark.asyncio
async def test_empty_llm_response_retries_with_context() -> None:
    valid = json.dumps({
        "mode": "SIMPLE",
        "goal": "创建文章",
        "actions": [{"action": "CREATE", "resource": "CONTENT"}],
        "conditions": [],
        "constraints": [],
        "target_hint": None,
        "confidence": 0.9,
    })
    llm = _SequenceLLM(["", valid])
    tu = TaskUnderstanding(llm=llm, model="test")

    result = await tu._try_l2_v2("创建一篇 Java 文章")

    assert result is not None
    assert result.actions[0].action == ActionType.CREATE
    assert llm.chat.completions.calls == 2
    assert len(tu.validation_traces) == 1
    issue_types = {issue["type"] for issue in tu.validation_traces[0].validation_errors}
    assert "EMPTY_LLM_RESPONSE" in issue_types
    assert tu.validation_traces[0].final_result is not None


@pytest.mark.asyncio
async def test_complex_intent_spec_preserves_required_actions() -> None:
    complex_spec = json.dumps({
        "mode": "CONDITIONAL",
        "goal": "运营 Agent 学习专题",
        "actions": [
            {"action": "SEARCH", "resource": "POST"},
            {"action": "ANALYZE", "resource": "POST"},
            {"action": "UPDATE_OR_CREATE", "resource": "DRAFT"},
            {"action": "PUBLISH", "resource": "CONTENT"},
        ],
        "conditions": [{
            "type": "IF_EXISTS",
            "resource": "DRAFT",
            "then_action": "UPDATE",
            "else_action": "CREATE",
        }],
        "constraints": [
            {"type": "APPROVAL", "value": "BEFORE_PUBLISH"},
            {"type": "TIME", "value": "5 分钟后"},
        ],
        "target_hint": "Agent 学习草稿",
        "confidence": 0.95,
    })
    tu = TaskUnderstanding(llm=_FakeLLM(complex_spec), model="test")

    result = await tu._try_l2_v2("复杂运营专题")

    assert result is not None
    assert {action.action for action in result.actions} == {
        ActionType.SEARCH,
        ActionType.ANALYZE,
        ActionType.UPDATE_OR_CREATE,
        ActionType.PUBLISH,
    }
    assert result.conditions[0].type.value == "IF_EXISTS"
    assert {constraint.type.value for constraint in result.constraints} == {
        "APPROVAL",
        "TIME",
    }
