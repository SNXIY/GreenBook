"""Compile-once contracts for semantic interpretation and persisted resumes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.command.models import Command, CommandItem, CommandType
from greenbook_agent_core.execution.runtime_result import RuntimeResult

from tests.unit.test_action_observation_continuation import (
    _LLM,
    _auth,
    _make_adapter,
    _observation,
)


class _CountingInterpreter:
    def __init__(self, command: Command) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, *_args, **_kwargs) -> Command:
        self.calls += 1
        return self.command


class _CountingActionLoop:
    def __init__(self) -> None:
        self.calls = 0

    async def run_for_command(self, **_kwargs) -> RuntimeResult:
        self.calls += 1
        return RuntimeResult(success=True, status="COMPLETED", execution_path="action_loop")


class _NeverInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("persisted semantic state must not re-enter CommandInterpreter")


@pytest.mark.asyncio
async def test_normal_turn_compiles_semantics_once() -> None:
    interpreter = _CountingInterpreter(Command(type=CommandType.QUERY, raw_input="status"))
    coordinator = TurnCoordinator(
        command_runtime=interpreter,
        target_resolver=SimpleNamespace(resolve=lambda *_args: None),
    )

    result = await coordinator.execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="status",
    )

    assert result.status == "COMPLETED"
    assert interpreter.calls == 1


@pytest.mark.asyncio
async def test_complex_turn_uses_the_already_compiled_command() -> None:
    command = Command(
        type=CommandType.CREATE,
        goal="write Java and Agent posts",
        raw_input="write Java and Agent posts",
        items=[
            CommandItem(topic="Java", capabilities=["GENERATE_CONTENT"]),
            CommandItem(topic="Agent", capabilities=["GENERATE_CONTENT"]),
        ],
    )
    interpreter = _CountingInterpreter(command)
    action_loop = _CountingActionLoop()
    coordinator = TurnCoordinator(
        command_runtime=interpreter,
        target_resolver=SimpleNamespace(resolve=lambda *_args: None),
        action_loop_executor=action_loop,
    )

    result = await coordinator.execute(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="write Java and Agent posts",
    )

    assert result.status == "COMPLETED"
    assert interpreter.calls == 1
    assert action_loop.calls == 1


async def _assert_persisted_path_does_not_interpret(
    *,
    status: str,
    capability: str = "GENERATE_CONTENT",
) -> None:
    adapter = await _make_adapter(_LLM([]), observation=_observation(status=status, capability=capability))
    never = _NeverInterpreter()
    adapter._command_runtime = never  # type: ignore[attr-defined]

    async def fake_run_agent_loop(**_kwargs):
        return RuntimeResult(success=True, status="COMPLETED", execution_path="action_loop")

    adapter._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]
    result = await adapter.continue_run(
        observation=_observation(status=status, capability=capability),
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        mcp=None,
        llm=_LLM([]),
        model="test-model",
        auth=_auth(),
    )

    assert result.status == "COMPLETED"
    assert never.calls == 0


@pytest.mark.asyncio
async def test_write_execution_continuation_reuses_persisted_command() -> None:
    await _assert_persisted_path_does_not_interpret(status="COMPLETED", capability="GENERATE_CONTENT")


@pytest.mark.asyncio
async def test_retry_path_reuses_persisted_command() -> None:
    await _assert_persisted_path_does_not_interpret(status="FAILED", capability="GENERATE_CONTENT")


@pytest.mark.asyncio
async def test_resume_path_reuses_persisted_command() -> None:
    await _assert_persisted_path_does_not_interpret(status="COMPLETED", capability="SCHEDULE_PUBLISH")


@pytest.mark.asyncio
async def test_action_observation_continuation_reuses_persisted_command() -> None:
    await _assert_persisted_path_does_not_interpret(status="COMPLETED", capability="SCHEDULE_PUBLISH")


@pytest.mark.asyncio
async def test_result_unknown_reconcile_does_not_reinterpret() -> None:
    await _assert_persisted_path_does_not_interpret(status="WAITING_EXTERNAL", capability="SCHEDULE_PUBLISH")
