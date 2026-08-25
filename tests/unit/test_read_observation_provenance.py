"""READ Observation, provenance, identity and convergence contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from greenbook_agent_core.actionloop import ActionDecision, ActionDecisionType, ActionLoop
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.task.models import Objective, Task, TaskResourceRef
from greenbook_contracts.tool_result import ToolResult
from greenbook_mcp_server.tools.community import search_public_posts


class _Store:
    def _record(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def _record_resource(
        self,
        task: Task,
        resource_id: str,
        resource_kind: str,
        title: str = "",
        *,
        content: str = "",
        objective_id: str = "",
    ) -> None:
        key = (str(resource_id), str(resource_kind).upper())
        if not any(
            (str(item.resource_id), str(item.resource_kind).upper()) == key
            for item in task.resource_index
        ):
            task.resource_index.append(
                TaskResourceRef(
                    resource_id=str(resource_id),
                    resource_kind=str(resource_kind),
                    objective_id=str(objective_id) or None,
                    title=title or None,
                )
            )


def _task(
    capabilities: list[str],
    *,
    task_id: str = "task-read",
    objective_id: str = "objective-read",
    result_requirement: str = "RESOURCE_MUTATION",
) -> Task:
    return Task(
        task_id=task_id,
        conversation_id=f"conversation-{task_id}",
        user_id="user-1",
        tenant_id="tenant-1",
        objectives=[
            Objective(
                task_id=task_id,
                objective_id=objective_id,
                description="Research and create",
                intent="Research and create",
                required_capabilities=capabilities,
                result_requirement=result_requirement,
            )
        ],
    )


def _command() -> Command:
    return Command(type=CommandType.CREATE, goal="research and create", raw_input="research")


def _search_result(post_ids: list[str], *, query: str = "Agent Memory") -> dict[str, Any]:
    return {
        "ok": True,
        "code": "OK",
        "data": {
            "items": [
                {"post_id": post_id, "title": f"Title {post_id}", "summary": f"Summary {post_id}"}
                for post_id in post_ids
            ],
            "query": query,
            "total": len(post_ids),
        },
        "resource_refs": [
            {
                "ref": f"post:{post_id}",
                "kind": "POST",
                "resource_id": post_id,
                "title": f"Title {post_id}",
            }
            for post_id in post_ids
        ],
        "provenance": ["COMMUNITY_DATA"],
    }


def _view_result(post_id: str, body: str) -> dict[str, Any]:
    return {
        "ok": True,
        "code": "OK",
        "data": {"post_id": post_id, "title": f"Title {post_id}", "body": body},
        "resource_refs": [{"kind": "POST", "resource_id": post_id}],
        "provenance": ["COMMUNITY_DATA"],
    }


@pytest.mark.asyncio
async def test_search_tool_result_populates_typed_resource_refs() -> None:
    class _Java:
        async def search_posts(self, **_kwargs: Any) -> ToolResult[Any]:
            return ToolResult.success(SimpleNamespace(items=[
                SimpleNamespace(post_id="post-123", title="A"),
                SimpleNamespace(post_id="post-456", title="B"),
            ]))

    context = SimpleNamespace(
        java=_Java(),
        auth=SimpleNamespace(raw_access_token="token"),
        trace_id="trace-1",
        conversation_id="conversation-1",
    )
    result = await search_public_posts(context, "Agent Memory")

    assert [ref.resource_id for ref in result.resource_refs] == ["post-123", "post-456"]
    assert [ref.kind for ref in result.resource_refs] == ["POST", "POST"]
    assert result.resource_refs[0].title == "A"
    assert result.resource_refs[0].tool == "community.search_public_posts"
    assert result.provenance == ["COMMUNITY_DATA"]


@pytest.mark.asyncio
async def test_read_observation_has_identity_query_provenance_and_timestamp() -> None:
    async def read(**_kwargs: Any) -> dict[str, Any]:
        return _search_result(["post-123", "post-456"])

    loop = ActionLoop(read_handler=read)
    observation = await loop._do_read(
        _task(["SEARCH_COMMUNITY"]),
        None,
        None,
        "SEARCH_POSTS",
        "SEARCH_COMMUNITY",
        "community.search_public_posts",
        {"query": "Agent Memory", "page": 1},
    )

    assert observation.outcome == "SUCCESS"
    assert observation.ok is True
    assert observation.tool_name == "community.search_public_posts"
    assert observation.query == "Agent Memory"
    assert observation.input_fingerprint
    assert observation.task_id == "task-read"
    assert [ref.resource_id for ref in observation.resource_refs] == ["post-123", "post-456"]
    assert observation.resource_refs[0].resource_type == "POST"
    assert observation.provenance == ["COMMUNITY_DATA"]
    assert observation.verified_facts["resource_count"] == 2
    assert observation.occurred_at.endswith("+00:00")


@pytest.mark.asyncio
async def test_view_observation_keeps_exact_resource_identity() -> None:
    async def read(**_kwargs: Any) -> dict[str, Any]:
        return _view_result("post-exact", "verified body")

    loop = ActionLoop(read_handler=read)
    observation = await loop._do_read(
        _task(["GET_POST_DETAIL"]),
        None,
        None,
        "GET_POST",
        "GET_POST_DETAIL",
        "community.get_post",
        {"post_id": "post-exact"},
    )

    assert observation.resource_id == "post-exact"
    assert observation.resource_kind == "POST"
    assert [(ref.kind, ref.resource_id) for ref in observation.resource_refs] == [("POST", "post-exact")]
    assert observation.detail["structured_data"]["body"] == "verified body"


@pytest.mark.asyncio
async def test_search_then_create_uses_read_evidence_without_finishing_early() -> None:
    task = _task(["SEARCH_COMMUNITY", "GENERATE_CONTENT"])
    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []

    async def decide(_context: Any) -> ActionDecision:
        if not reads:
            return ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action="SEARCH_POSTS",
                arguments={"query": "Agent Memory"},
            )
        return ActionDecision(
            decision=ActionDecisionType.GENERATE_CONTENT,
            semantic_action="CREATE_DRAFT",
            arguments={"title": "Research", "instruction": "Use the search evidence."},
        )

    async def read(**kwargs: Any) -> dict[str, Any]:
        reads.append(kwargs["arguments"])
        return _search_result(["post-123", "post-456"])

    async def write(**kwargs: Any) -> dict[str, Any]:
        writes.append(kwargs)
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-1"}

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        write_submitter=write,
        task_store=_Store(),
    ).run(task, _command())

    assert result.status == "COMPLETED"
    assert len(reads) == 1
    assert len(writes) == 1
    assert {item.resource_id for item in task.resource_index} >= {"post-123", "post-456", "draft-1"}


@pytest.mark.asyncio
async def test_equivalent_search_converges_without_third_read() -> None:
    task = _task(["SEARCH_COMMUNITY", "GENERATE_CONTENT"])
    calls = 0

    async def decide(_context: Any) -> ActionDecision:
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": "same"},
        )

    async def read(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _search_result(["post-same"], query="same")

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        task_store=_Store(),
        max_iterations=8,
    ).run(task, _command())

    assert result.error_code == "ACTION_LOOP_NO_PROGRESS"
    assert calls == 2
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_same_search_can_progress_when_second_result_adds_resource() -> None:
    task = _task(["SEARCH_COMMUNITY", "GENERATE_CONTENT"])
    calls = 0
    writes: list[str] = []

    async def decide(_context: Any) -> ActionDecision:
        if calls < 2:
            return ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action="SEARCH_POSTS",
                arguments={"query": "same"},
            )
        return ActionDecision(
            decision=ActionDecisionType.GENERATE_CONTENT,
            semantic_action="CREATE_DRAFT",
            arguments={"title": "Research", "instruction": "Create."},
        )

    async def read(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _search_result(["post-one"] if calls == 1 else ["post-one", "post-two"], query="same")

    async def write(**kwargs: Any) -> dict[str, Any]:
        writes.append(kwargs["semantic_action"])
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-new"}

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        write_submitter=write,
        task_store=_Store(),
    ).run(task, _command())

    assert result.status == "COMPLETED"
    assert calls == 2
    assert writes == ["CREATE_DRAFT"]
    assert "post-two" in {item.resource_id for item in task.resource_index}


@pytest.mark.asyncio
async def test_different_search_query_is_not_equivalent_read() -> None:
    task = _task(["SEARCH_COMMUNITY", "GENERATE_CONTENT"])
    queries: list[str] = []
    writes: list[str] = []

    async def decide(_context: Any) -> ActionDecision:
        if len(queries) == 0:
            query = "Java"
        elif len(queries) == 1:
            query = "Python"
        else:
            return ActionDecision(
                decision=ActionDecisionType.GENERATE_CONTENT,
                semantic_action="CREATE_DRAFT",
                arguments={"title": "Compare", "instruction": "Create."},
            )
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": query},
        )

    async def read(**kwargs: Any) -> dict[str, Any]:
        query = str(kwargs["arguments"]["query"])
        queries.append(query)
        return _search_result([f"post-{query.lower()}"], query=query)

    async def write(**kwargs: Any) -> dict[str, Any]:
        writes.append(kwargs["semantic_action"])
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-query"}

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        write_submitter=write,
        task_store=_Store(),
    ).run(task, _command())

    assert result.status == "COMPLETED"
    assert queries == ["Java", "Python"]
    assert writes == ["CREATE_DRAFT"]


@pytest.mark.asyncio
async def test_multiple_objectives_keep_read_evidence_owner_scoped() -> None:
    task = Task(
        task_id="task-multi-read",
        conversation_id="conversation-multi-read",
        user_id="user-1",
        tenant_id="tenant-1",
        objectives=[
            Objective(task_id="task-multi-read", objective_id="a", required_capabilities=["SEARCH_COMMUNITY"]),
            Objective(task_id="task-multi-read", objective_id="b", required_capabilities=["SEARCH_COMMUNITY"]),
        ],
    )

    async def decide(context: Any) -> ActionDecision:
        objective_id = str(context["current_objective"]["objective_id"])
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": objective_id},
        )

    async def read(**kwargs: Any) -> dict[str, Any]:
        objective_id = str(kwargs["arguments"]["query"])
        return _search_result([f"post-{objective_id}"], query=objective_id)

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        task_store=_Store(),
        max_iterations=4,
    ).run(task, _command())

    assert result.status == "COMPLETED"
    assert task.objectives[0].related_resource_ids == ["post-a"]
    assert task.objectives[1].related_resource_ids == ["post-b"]


@pytest.mark.asyncio
async def test_read_failure_has_no_resource_ref_and_never_result_unknown() -> None:
    async def read(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "VALIDATION_ERROR",
            "message": "query is required",
            "resource_refs": [{"kind": "POST", "resource_id": "should-not-bind"}],
        }

    result = await ActionLoop(
        decision_maker=lambda _context: ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": ""},
        ),
        read_handler=read,
        task_store=_Store(),
        max_iterations=1,
    ).run(_task(["SEARCH_COMMUNITY"]), _command())

    observation = result.observations[0]
    assert observation.outcome == "FAILED"
    assert observation.error_code == "VALIDATION_ERROR"
    assert observation.resource_refs == []
    assert observation.outcome != "RESULT_UNKNOWN"


@pytest.mark.asyncio
async def test_cross_turn_historical_resource_ref_drives_exact_view() -> None:
    task = _task(
        ["GET_POST_DETAIL"],
        task_id="task-cross-read",
        objective_id="objective-cross-read",
        result_requirement="GROUNDED_SYNTHESIS",
    )
    task.objectives[0].related_resource_ids = ["post-historical"]
    task.resource_index = [
        TaskResourceRef(
            resource_id="post-historical",
            resource_kind="SEARCH_RESULT",
            objective_id="objective-cross-read",
        )
    ]
    requested: list[str] = []

    async def read(**kwargs: Any) -> dict[str, Any]:
        requested.append(str(kwargs["arguments"].get("post_id") or ""))
        return _view_result("post-historical", "historical evidence")

    result = await ActionLoop(
        read_handler=read,
        task_store=_Store(),
        max_iterations=4,
    ).run(task, _command())

    assert result.status == "COMPLETED"
    assert requested == ["post-historical"]


@pytest.mark.asyncio
async def test_view_new_facts_continue_then_stale_facts_converge() -> None:
    task = _task(["GET_POST_DETAIL", "GENERATE_CONTENT"])
    calls = 0

    async def decide(_context: Any) -> ActionDecision:
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="GET_POST",
            arguments={"post_id": "post-facts"},
        )

    async def read(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _view_result("post-facts", "fact-v1" if calls == 1 else "fact-v2")

    result = await ActionLoop(
        decision_maker=decide,
        read_handler=read,
        task_store=_Store(),
        max_iterations=8,
    ).run(task, _command())

    assert result.error_code == "ACTION_LOOP_NO_PROGRESS"
    assert calls == 3


@pytest.mark.asyncio
async def test_action_hard_ceiling_stops_unique_read_loop_without_write() -> None:
    task = _task(["SEARCH_COMMUNITY", "GENERATE_CONTENT"])
    calls = 0
    writes: list[str] = []

    async def read(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _search_result([f"post-{calls}"], query="moving")

    async def write(**kwargs: Any) -> dict[str, Any]:
        writes.append(kwargs["semantic_action"])
        return {"ok": True, "status": "COMPLETED", "resource_id": "must-not-write"}

    result = await ActionLoop(
        decision_maker=lambda _context: ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": "moving"},
        ),
        read_handler=read,
        write_submitter=write,
        task_store=_Store(),
        max_iterations=3,
    ).run(task, _command())

    assert result.error_code == "ACTION_LOOP_ITERATION_BUDGET"
    assert calls == 3
    assert writes == []
