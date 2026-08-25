from types import SimpleNamespace

import pytest

from greenbook_agent_api.services.action_loop_executor import ActionLoopExecutor
from greenbook_agent_core.actionloop.loop import _resource_kind_by_id
from greenbook_agent_core.actionloop import ActionLoop
from greenbook_agent_core.actionloop.models import ActionDecision, ActionDecisionType
from greenbook_agent_core.task.models import Objective, Task, TaskResourceRef


class _Events:
    def __init__(self):
        self.items = []

    def append(self, event):
        self.items.append(event)


def _executor(store):
    return ActionLoopExecutor(adapter=SimpleNamespace(), decision_event_store=store)


def test_deterministic_decision_has_zero_llm_usage() -> None:
    store = _Events()
    executor = _executor(store)
    executor._latest_actionloop_llm = {
        "category": "ACTIONLOOP", "latency_ms": 6400,
        "input_tokens": 1000, "output_tokens": 100,
    }
    executor._record_decision(
        run_id="r1", task_id="t1", objective_id="o1", iteration=2,
        decision=ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="CREATE_SCHEDULE",
        ),
        decision_source="DETERMINISTIC",
        llm_called=False,
    )
    payload = store.items[0].payload
    assert payload["decision_source"] == "DETERMINISTIC"
    assert payload["llm_called"] is False
    assert payload["llm_latency_ms"] == 0
    assert payload["llm_input_tokens"] == 0
    assert payload["llm_output_tokens"] == 0
    assert payload["llm"] == {
        "category": "", "latency_ms": 0,
        "input_tokens": 0, "output_tokens": 0,
    }


def test_llm_decision_uses_current_usage() -> None:
    store = _Events()
    executor = _executor(store)
    executor._latest_actionloop_llm = {
        "category": "ACTIONLOOP", "latency_ms": 123,
        "input_tokens": 12, "output_tokens": 7,
    }
    executor._record_decision(
        run_id="r1", task_id="t1", objective_id="o1", iteration=1,
        decision=ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
        ),
    )
    payload = store.items[0].payload
    assert payload["decision_source"] == "LLM"
    assert payload["llm_called"] is True
    assert payload["llm_latency_ms"] == 123
    assert payload["llm_input_tokens"] == 12
    assert payload["llm_output_tokens"] == 7


def test_resource_identity_preserves_same_id_with_different_kinds() -> None:
    task = Task(
        task_id="t1", conversation_id="c1", user_id="u1", tenant_id="n1",
        resource_index=[
            TaskResourceRef(resource_id="post-1", resource_kind="SEARCH_RESULT"),
            TaskResourceRef(resource_id="post-1", resource_kind="POST"),
        ],
    )
    assert _resource_kind_by_id(task)["post-1"] == {"SEARCH_RESULT", "POST"}


def test_resource_identity_is_objective_scoped() -> None:
    task = Task(
        task_id="t1", conversation_id="c1", user_id="u1", tenant_id="n1",
        objectives=[
            Objective(task_id="t1", objective_id="a", required_capabilities=["GET_POST_DETAIL"]),
            Objective(task_id="t1", objective_id="b", required_capabilities=["GET_POST_DETAIL"]),
        ],
        resource_index=[
            TaskResourceRef(resource_id="post-1", resource_kind="POST", objective_id="a"),
        ],
    )
    assert task.objectives[0].related_resource_ids == []
    assert task.objectives[1].related_resource_ids == []


@pytest.mark.asyncio
async def test_schedule_continuation_is_observed_as_deterministic() -> None:
    task = Task(
        task_id="t1", conversation_id="c1", user_id="u1", tenant_id="n1",
        objectives=[Objective(
            task_id="t1", objective_id="o1",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            constraints={"run_at": "2026-08-21T06:00:00Z"},
        )],
    )
    events = []

    async def decide(_context):
        return ActionDecision(
            decision=ActionDecisionType.GENERATE_CONTENT,
            semantic_action="CREATE_DRAFT",
            arguments={"title": "Java", "instruction": "short"},
        )

    async def write(**kwargs):
        action = kwargs["semantic_action"]
        return {
            "ok": True, "status": "COMPLETED",
            "resource_id": "draft-1" if action == "CREATE_DRAFT" else "schedule-1",
        }

    class Store:
        async def _record(self, *_args, **_kwargs):
            return None

        async def _record_resource(self, task, resource_id, resource_kind, title="", content="", objective_id=""):
            task.resource_index.append({
                "resource_id": resource_id, "resource_kind": resource_kind,
                "objective_id": objective_id, "title": title, "content": content,
            })

    async def observe(**kwargs):
        events.append(kwargs)

    result = await ActionLoop(
        decision_maker=decide, write_submitter=write,
        task_store=Store(), decision_observer=observe, max_iterations=4,
    ).run(task, request=SimpleNamespace(run_id="r1", trace_id="x1"))
    assert result.status == "COMPLETED"
    assert [event["decision_source"] for event in events] == ["LLM", "DETERMINISTIC"]
    assert [event["llm_called"] for event in events] == [True, False]
