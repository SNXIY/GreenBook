"""Phase 3.5 adaptive control plane tests.

The first LLM understanding may emit ``first_action`` (a canonical
capability) and ``request_complexity``. SIMPLE requests bootstrap that
validated action (skip GoalDecomposer and the first AgentLoop reason);
COMPLEX requests keep the full decomposition path. Any validation mismatch
upgrades to the full path instead of guessing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.agent.actions import AgentAction, AgentActionType
from greenbook_agent_core.agent.loop import AgentLoop
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    StructuredCommandOutput,
)
from greenbook_agent_core.goal.models import GoalTree
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_contracts.tool_contract import ToolMetadata


class _FakeToolRegistry:
    def list_tool_metadata(self) -> list[ToolMetadata]:
        def tool(name: str, capabilities: tuple[str, ...]) -> ToolMetadata:
            return ToolMetadata(
                name=name,
                description=name,
                capabilities=capabilities,
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {}},
            )

        return [
            tool("search_community", ("SEARCH_COMMUNITY",)),
            tool("generate_content_a", ("GENERATE_CONTENT",)),
            tool("generate_content_b", ("GENERATE_CONTENT",)),
        ]


def _adapter(container: RuntimeContainer) -> ConversationRuntimeAdapter:
    return ConversationRuntimeAdapter(
        container=container,
        runtime_service=SimpleNamespace(container=container),
        task_provider=SimpleNamespace(),
        task_manager=SimpleNamespace(),
        tool_registry=_FakeToolRegistry(),
    )


def _command(**overrides: Any) -> Command:
    values = dict(
        type=CommandType.QUERY,
        goal="帮我找几篇关于 Agent 的帖子并总结共同方法",
        objective="帮我找几篇关于 Agent 的帖子并总结共同方法",
        first_action="SEARCH_COMMUNITY",
        request_complexity="SIMPLE",
        required_capabilities=["SEARCH_COMMUNITY"],
        entities={"topic": "Agent"},
        raw_input="帮我找几篇关于 Agent 的帖子并总结共同方法",
    )
    values.update(overrides)
    return Command(**values)


# ── _bootstrap_action: SIMPLE / COMPLEX / validation upgrades ────────────


def test_simple_with_valid_first_action_yields_bootstrap_tool() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    action = adapter._bootstrap_action(_command())
    assert action is not None
    assert action.action == AgentActionType.TOOL_CALL
    assert action.tool_name  # unique catalog tool for SEARCH_COMMUNITY
    assert "SEARCH_COMMUNITY" in action.reason
    # The bootstrap must carry the tool's required arguments — never emit a
    # parameterless search (observed: missing required argument 'query').
    assert action.tool_args.get("query") == "Agent"


def test_multi_capability_goal_never_bootstraps() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    # search → summarize → write → schedule: the bootstrap tree has a single
    # node and would silently skip every later capability.
    assert adapter._bootstrap_action(
        _command(
            first_action="SEARCH_COMMUNITY",
            required_capabilities=[
                "SEARCH_COMMUNITY",
                "ANALYZE_CONTENT_PATTERNS",
                "GENERATE_CONTENT",
                "SCHEDULE_PUBLISH",
            ],
        )
    ) is None


def test_single_search_without_derivable_query_upgrades() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    # SIMPLE single-capability search with no structured query must NOT emit a
    # parameterless TOOL_CALL; the full path lets the model supply the query.
    assert adapter._bootstrap_action(
        _command(entities={}, parameters={})
    ) is None


def test_complex_request_never_bootstraps() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    assert adapter._bootstrap_action(_command(request_complexity="COMPLEX")) is None


def test_first_action_outside_required_capabilities_upgrades() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    # Semantic monotonicity: first_action must be one of the Command's own
    # required capabilities; a mismatch must not bypass decomposition.
    assert adapter._bootstrap_action(
        _command(first_action="PUBLISH_NOW", required_capabilities=["SEARCH_COMMUNITY"])
    ) is None


def test_first_action_with_no_unique_tool_upgrades() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    # An ambiguous capability (several tools) must not pick a tool by Python;
    # it upgrades to the full path where AgentLoop/ToolSelector decide.
    assert adapter._bootstrap_action(
        _command(first_action="GENERATE_CONTENT", required_capabilities=["GENERATE_CONTENT"])
    ) is None


def test_empty_first_action_upgrades() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    assert adapter._bootstrap_action(_command(first_action="")) is None


# ── _bootstrap_goal_tree: deterministic single-goal tree ─────────────────


def test_bootstrap_goal_tree_carries_command_semantics() -> None:
    container = RuntimeContainer.for_testing()
    adapter = _adapter(container)
    action = adapter._bootstrap_action(_command())
    assert action is not None
    tree = adapter._bootstrap_goal_tree(_command(), action)
    assert isinstance(tree, GoalTree)
    assert tree.source == "COMMAND_BOOTSTRAP"
    tree.validate_tree()
    assert tree.root is not None
    assert tree.root.required_capabilities == ["SEARCH_COMMUNITY"]
    assert tree.root.semantic_operation == ""
    assert len(tree.task_nodes) == 1
    assert tree.task_nodes[0].capability == "SEARCH_COMMUNITY"
    assert tree.task_nodes[0].goal_id == tree.root.goal_id


# ── AgentLoop: bootstrap action skips the first reason ───────────────────


class _CountingReasoner:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, observation: Any, state: Any) -> AgentAction:
        self.calls += 1
        return AgentAction(action=AgentActionType.FINISH, reason="done")


@pytest.mark.asyncio
async def test_bootstrap_action_skips_first_reason_and_keeps_continuation() -> None:
    reasoner = _CountingReasoner()
    loop = AgentLoop(llm=None, reasoner=reasoner)
    command = _command()
    tree = GoalTree(
        root=__import__("greenbook_agent_core.goal.models", fromlist=["Goal"]).Goal(
            goal_id="g1",
            description=command.goal,
            required_capabilities=["SEARCH_COMMUNITY"],
        ),
        source="COMMAND_BOOTSTRAP",
    )
    bootstrap = AgentAction(
        action=AgentActionType.TOOL_CALL,
        tool_name="search_community_tool",
        reason="bootstrap",
    )

    # First iteration consumes the bootstrap action; the reasoner must not run.
    await loop.run(
        command,
        tree,
        bootstrap_action=bootstrap,
        max_iterations=1,
    )
    assert reasoner.calls == 0


@pytest.mark.asyncio
async def test_no_bootstrap_reasons_normally() -> None:
    reasoner = _CountingReasoner()
    loop = AgentLoop(llm=None, reasoner=reasoner)
    command = _command()
    tree = GoalTree(
        root=__import__("greenbook_agent_core.goal.models", fromlist=["Goal"]).Goal(
            goal_id="g1",
            description=command.goal,
            required_capabilities=["SEARCH_COMMUNITY"],
        ),
        source="LLM_STRUCTURED_OUTPUT",
    )
    await loop.run(command, tree, max_iterations=1)
    assert reasoner.calls == 1


# ── StructuredCommandOutput schema carries the new fields ────────────────


def test_structured_output_accepts_new_fields() -> None:
    parsed = StructuredCommandOutput(
        command=CommandType.QUERY,
        goal="find posts",
        first_action="SEARCH_COMMUNITY",
        request_complexity="SIMPLE",
        required_capabilities=["SEARCH_COMMUNITY"],
    )
    assert parsed.first_action == "SEARCH_COMMUNITY"
    assert parsed.request_complexity == "SIMPLE"
    schema = StructuredCommandOutput.model_json_schema()
    assert "first_action" in schema["properties"]
    assert "request_complexity" in schema["properties"]
