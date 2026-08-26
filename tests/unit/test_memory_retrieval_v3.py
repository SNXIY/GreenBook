"""Focused regression tests for the canonical V3 memory selection policy."""

from __future__ import annotations

import pytest
from greenbook_agent_core.memory import (
    MemoryRecord,
    MemoryRelevanceGate,
    MemoryType,
)

from scripts.memory_evaluation_harness import _canonical_system_fixture, _system_retriever


@pytest.mark.asyncio
async def test_type_aware_retrieval_covers_required_mixed_types() -> None:
    fixture = _canonical_system_fixture()
    retriever, _ = _system_retriever(fixture["repository"])

    selected = await retriever.retrieve(
        user_id=fixture["user_id"],
        tenant_id=fixture["tenant_id"],
        target_query="deep technical Agent learning outline body",
        touch=False,
    )

    assert {
        fixture["records"]["preference_depth"].memory_id,
        fixture["records"]["semantic_learning"].memory_id,
        fixture["records"]["procedure_article"].memory_id,
    } <= {item.memory_id for item in selected}
    assert fixture["records"]["episode_publication"].memory_id not in {
        item.memory_id for item in selected
    }


@pytest.mark.asyncio
async def test_current_exception_and_unrelated_request_return_no_memory() -> None:
    fixture = _canonical_system_fixture()
    retriever, _ = _system_retriever(fixture["repository"])

    for query in (
        "This time write the technical article directly without an outline.",
        "weather forecast and astronomy",
    ):
        selected = await retriever.retrieve(
            user_id=fixture["user_id"],
            tenant_id=fixture["tenant_id"],
            target_query=query,
            touch=False,
        )
        assert selected == []


def test_relevance_gate_required_type_coverage_stays_bounded() -> None:
    gate = MemoryRelevanceGate(relevance_threshold=0.5, confidence_threshold=0.5)
    preference = MemoryRecord(
        memory_id="coverage-preference",
        memory_type=MemoryType.PREFERENCE,
        content="deep technical articles",
        confidence=0.9,
        structured_metadata={
            "preference_type": "writing_depth",
            "value": "deep",
        },
    )
    semantic = MemoryRecord(
        memory_id="coverage-semantic",
        memory_type=MemoryType.SEMANTIC,
        content="learning Agent",
        confidence=0.9,
        structured_metadata={
            "memory_contract": "SEMANTIC_V1",
            "memory_role": "stable_fact",
        },
    )
    result = gate.evaluate(
        [preference, semantic],
        score=lambda item: 0.4 if item.memory_id == preference.memory_id else 0.9,
        limit=1,
        required_types=("PREFERENCE", "SEMANTIC"),
        type_key=lambda item: (
            "PREFERENCE"
            if item.metadata.get("preference_type")
            else "SEMANTIC"
        ),
        coverage_threshold=0.35,
    )

    assert [item.memory_id for item in result.selected] == [preference.memory_id]
    assert len(result.selected) <= 1
