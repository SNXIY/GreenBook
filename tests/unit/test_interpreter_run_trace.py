"""Run-bound semantic interpreter diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from greenbook_agent_core.command.interpreter import CommandInterpreter


class _LLM:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {"command": "QUERY", "goal": "保留中文 2026 Agent"},
                        ensure_ascii=False,
                    ),
                ),
            )],
        )


@pytest.mark.asyncio
async def test_interpreter_diagnostics_are_bound_to_run_and_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_file = tmp_path / "semantic-trace.jsonl"
    monkeypatch.setenv("GREENBOOK_DEBUG_INTERPRETER", "1")
    monkeypatch.setenv("GREENBOOK_DEBUG_INTERPRETER_FILE", str(trace_file))

    command = await CommandInterpreter(llm=_LLM(), model="test").interpret(
        "保留中文 2026 Agent",
        run_id="run-utf8-1",
        turn_id="turn-utf8-1",
    )

    assert command.raw_input == "保留中文 2026 Agent"
    records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    stages = {record["stage"] for record in records}
    assert {"raw", "normalized", "segmentation"} <= stages
    assert all(record["run_id"] == "run-utf8-1" for record in records)
    assert all(record["turn_id"] == "turn-utf8-1" for record in records)
