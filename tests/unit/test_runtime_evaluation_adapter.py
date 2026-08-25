"""Contract tests for the thin Runtime evaluation adapter."""

from __future__ import annotations

import pytest

from greenbook_evaluation.models import EvalCase
from greenbook_evaluation.runtime_adapter import RuntimeEvaluationAdapter


class _FakeRuntimeTransport:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.run_reads: list[str] = []

    async def create_conversation(self, title: str) -> dict:
        return {"conversation_id": "eval-conversation", "title": title}

    async def submit_message(
        self,
        conversation_id: str,
        content: str,
        *,
        timezone: str,
        idempotency_key: str,
    ) -> dict:
        run_id = f"run-{len(self.submissions) + 1}"
        self.submissions.append(
            {
                "conversation_id": conversation_id,
                "content": content,
                "timezone": timezone,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            }
        )
        return {"run_id": run_id, "status": "ACCEPTED"}

    async def get_run(self, run_id: str) -> dict:
        self.run_reads.append(run_id)
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "partial_results": {"execution_ids": ["execution-1"]},
            "budget": {"model_calls": 1, "tool_calls": 2},
        }

    async def list_tasks(self, conversation_id: str) -> list[dict]:
        return [{"task_id": "task-1", "status": "COMPLETED"}]

    async def read_runtime_state(self, **kwargs) -> dict:
        return {
            "objective_count": 1,
            "objectives": [{"objective_id": "objective-1"}],
            "resource_bindings": [
                {
                    "resource_id": "same-business-id",
                    "resource_kind": "DRAFT",
                    "objective_id": "objective-1",
                },
                {
                    "resource_id": "same-business-id",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-1",
                },
            ],
            "schedule": {"status": "SCHEDULED"},
            "duplicate_write_count": 0,
            "java_truth": {"schedule_count": 1},
        }


@pytest.mark.asyncio
async def test_runtime_adapter_drives_cross_turn_and_projects_typed_facts() -> None:
    transport = _FakeRuntimeTransport()
    adapter = RuntimeEvaluationAdapter(
        transport,
        poll_interval_seconds=0,
        timeout_seconds=1,
    )
    case = EvalCase(
        case_id="runtime-cross-turn",
        category="CROSS_TURN",
        conversation_turns=[
            {"role": "user", "content": "Create a Java draft"},
            {"role": "user", "content": "Schedule it tomorrow"},
        ],
        user_message="Schedule it tomorrow",
        expected_terminal_status="COMPLETED",
        expected_task_state="COMPLETED",
        expected_objective_count=1,
        expected_resource_types=["DRAFT", "SCHEDULE"],
        expected_schedule={"status": "SCHEDULED"},
        expected_duplicate_write_count=0,
        expected_ownership_conflicts=0,
    )

    report = await adapter.evaluate([case])

    assert report.total_passed == 1
    assert len(transport.submissions) == 2
    assert [item["content"] for item in transport.submissions] == [
        "Create a Java draft",
        "Schedule it tomorrow",
    ]
    assert len({item["idempotency_key"] for item in transport.submissions}) == 2
    actual = report.results[0]
    assert actual.passed is True
    assert actual.trace["conversation_id"] == "eval-conversation"
