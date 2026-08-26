"""Focused contracts for converging legacy Memory out of Runtime."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryType,
    PreferenceRetriever,
)
from greenbook_agent_core.memory.manager import MemoryManager
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)


@pytest.mark.asyncio
async def test_preference_recall_uses_canonical_gate_and_allows_no_memory() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(
        user_id="u1",
        tenant_id="tenant-a",
        memory_type=MemoryType.PREFERENCE,
        content="prefer concise replies",
        confidence=0.9,
        structured_metadata={
            "preference_type": "response_style",
            "value": "prefer concise replies",
        },
    ))
    retriever = PreferenceRetriever(repository)

    selected = await retriever.retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="Give concise replies",
    )
    unrelated = await retriever.retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="Schedule a post about astronomy",
    )

    assert [item.memory_id for item in selected] == [record.memory_id]
    assert unrelated == []


@pytest.mark.asyncio
async def test_canonical_preference_recall_is_scoped_by_user_and_tenant() -> None:
    repository = InMemoryMemoryRepository()
    owned = repository.save(MemoryRecord(
        user_id="u1",
        tenant_id="tenant-a",
        memory_type=MemoryType.PREFERENCE,
        content="use Java technology stack",
        confidence=0.9,
        structured_metadata={
            "preference_type": "technology_stack",
            "value": "use Java technology stack",
        },
    ))
    repository.save(MemoryRecord(
        user_id="u2",
        tenant_id="tenant-a",
        memory_type=MemoryType.PREFERENCE,
        content="use Java technology stack",
        confidence=0.9,
    ))
    repository.save(MemoryRecord(
        user_id="u1",
        tenant_id="tenant-b",
        memory_type=MemoryType.PREFERENCE,
        content="use Java technology stack",
        confidence=0.9,
    ))

    values = await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="Use Java",
    )

    assert [item.memory_id for item in values] == [owned.memory_id]


@pytest.mark.asyncio
async def test_canonical_retriever_skips_legacy_preference_provider() -> None:
    class LegacyPreferenceProvider:
        calls = 0

        async def list_preferences(self, **_: object) -> list[dict[str, str]]:
            self.calls += 1
            return [{"key": "legacy", "value": "must not be read"}]

    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(
        user_id="u1",
        tenant_id="tenant-a",
        memory_type=MemoryType.PREFERENCE,
        content="prefer concise replies",
        confidence=0.9,
        structured_metadata={
            "preference_type": "response_style",
            "value": "prefer concise replies",
        },
    ))
    legacy = LegacyPreferenceProvider()
    snapshot = await ContextBuilder(
        memory_retriever=PreferenceRetriever(repository),
        preference_provider=legacy,
    ).build(
        conversation_id="c1",
        user_id="u1",
        tenant_id="tenant-a",
        target_query="Give concise replies",
    )

    assert legacy.calls == 0
    assert snapshot.memory_ids_used == [record.memory_id]


def _called_attributes(function: object) -> set[str]:
    source = textwrap.dedent(inspect.getsource(function))
    tree = ast.parse(source)
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_runtime_entrypoint_has_no_legacy_memory_recall_or_write_call() -> None:
    called = _called_attributes(RuntimeAgentService._execute_single)

    assert "_recall_memories" not in called
    assert "_record_episodic" not in called
    assert "_record_procedural" not in called


def test_runtime_fallback_uses_canonical_preference_retriever() -> None:
    class RuntimeStub:
        def __init__(self) -> None:
            self._memory_mgr = MemoryManager(
                repository=InMemoryMemoryRepository(),
            )

    adapter = ConversationRuntimeAdapter(runtime_service=RuntimeStub())

    assert isinstance(
        adapter._context_builder._memory_retriever,
        PreferenceRetriever,
    )
