"""Phase 4.3 focused tests: bounded structured-output recovery (design 0813).

A single bad model serialization must never kill the whole Run: normalize the
raw output, repair once, retry the reason once, then fail in a controlled way
with a user-safe message and the raw technical detail kept out of the UI.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentActionType, AgentLoop
from greenbook_agent_core.agent.loop import (
    MAX_STRUCTURED_OUTPUT_REPAIRS,
    USER_SAFE_REASONING_FAILURE,
    StructuredOutputError,
)
from greenbook_agent_core.agent.state import AgentState, Observation
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.goal.models import Goal, GoalTree


class _LLM:
    """Fake LLM whose responses may be dict payloads or raw strings."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"Fake LLM received more calls than expected: {len(self.calls)}")
        raw = self.responses.pop(0)
        content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def _tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="research_ai",
        description="搜索最近 AI 文章",
        goal_type="RESEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
    ))


def _state(**overrides: Any) -> AgentState:
    kwargs: dict[str, Any] = {
        "goal": _tree().root_goal,
        "goal_tree": _tree(),
        "command": Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        "iteration": 1,
    }
    kwargs.update(overrides)
    return AgentState(**kwargs)


@pytest.mark.asyncio
async def test_valid_json_requires_no_repair() -> None:
    llm = _LLM({"action": "FINISH", "reason": "done"})
    action = await AgentLoop(llm=llm).reason(Observation(), _state())
    assert action.action == AgentActionType.FINISH
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_markdown_fenced_json_normalizes_to_parse() -> None:
    fenced = "```json\n{\"action\": \"FINISH\", \"reason\": \"done\"}\n```"
    llm = _LLM(fenced)
    action = await AgentLoop(llm=llm).reason(Observation(), _state())
    assert action.action == AgentActionType.FINISH
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_malformed_json_repaired_once() -> None:
    llm = _LLM(
        '{"action": "FINISH", "reason": "unterminated',  # invalid JSON
        {"action": "FINISH", "reason": "repaired"},
    )
    action = await AgentLoop(llm=llm).reason(Observation(), _state())
    assert action.action == AgentActionType.FINISH
    assert action.reason == "repaired"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_wrong_enum_repaired_once() -> None:
    llm = _LLM(
        {"action": "NOT_A_REAL_ACTION", "reason": "bad enum"},
        {"action": "FINISH", "reason": "repaired"},
    )
    action = await AgentLoop(llm=llm).reason(Observation(), _state())
    assert action.action == AgentActionType.FINISH
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_missing_required_field_repaired_once() -> None:
    llm = _LLM(
        {"tool_name": "community.search_public_posts"},  # missing action
        {"action": "FINISH", "reason": "repaired"},
    )
    action = await AgentLoop(llm=llm).reason(Observation(), _state())
    assert action.action == AgentActionType.FINISH
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_repair_still_invalid_raises_controlled_failure() -> None:
    llm = _LLM(
        "not json at all",
        "still not json",
        "still not json either",
    )
    with pytest.raises(StructuredOutputError) as exc_info:
        await AgentLoop(llm=llm).reason(Observation(), _state())
    assert exc_info.value.code == "STRUCTURED_OUTPUT_INVALID"
    assert str(exc_info.value) == USER_SAFE_REASONING_FAILURE
    assert exc_info.value.technical["repair_attempted"] is True
    assert exc_info.value.technical["parse_failures"]
    # Budget: initial reason + one repair + one reason retry.
    assert len(llm.calls) == 1 + MAX_STRUCTURED_OUTPUT_REPAIRS + 1


@pytest.mark.asyncio
async def test_run_failure_is_user_safe_and_preserves_partial_results() -> None:
    """The whole Run must not die with a raw JSON error visible to the user."""

    llm = _LLM(
        "this is not json",
        "still not json",
        "still not json either",
    )
    result = await AgentLoop(llm=llm, max_iterations=3).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _tree(),
    )
    assert result.success is False
    assert result.error_code == "STRUCTURED_OUTPUT_INVALID"
    assert result.error_message == USER_SAFE_REASONING_FAILURE
    assert "invalid Agent JSON" not in result.error_message
    assert "JSONDecodeError" not in result.error_message
    assert result.state is not None
    assert result.state.reasoning_failure is not None
    assert result.state.reasoning_failure["reasoning_type"] == "reason"


def test_user_safe_error_maps_pydantic_validation_errors() -> None:
    from greenbook_agent_core.agent.loop import _user_safe_error

    pydantic_raw = (
        "13 validation errors for PlanningDecision\n"
        "insert_nodes.task_id\n  Field required [type=missing, input_value=...]\n"
        "insert_nodes.capability\n  Field required [type=missing, ...]\n"
        "insert_nodes.description\n  Extra inputs are not permitted [type=extra_forbidden, ...]"
    )
    assert _user_safe_error("AGENT_LOOP_FAILED", pydantic_raw) == USER_SAFE_REASONING_FAILURE
    assert _user_safe_error("AGENT_LOOP_FAILED", "JSONDecodeError: Expecting value: line 1") == USER_SAFE_REASONING_FAILURE
    assert _user_safe_error("AGENT_LOOP_FAILED", "Dynamic Planner output is not a PlanningDecision.") == USER_SAFE_REASONING_FAILURE
    # A genuine business error message is preserved.
    assert _user_safe_error("BUSINESS_REJECTED", "该草稿已发布，无法再修改。") == "该草稿已发布，无法再修改。"


def test_planner_response_payload_normalizes_fenced_json() -> None:
    from greenbook_agent_core.planning.dynamic import _response_payload

    resp = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content='```json\n{"decision": "CONTINUE", "reason": "keep going"}\n```'
            ),
        )],
    )
    payload = _response_payload(resp)
    assert payload["decision"] == "CONTINUE"
