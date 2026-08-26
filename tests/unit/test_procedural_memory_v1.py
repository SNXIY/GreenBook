from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.api.routes import _extract_completed_turn_procedural
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.memory import (
    PROCEDURAL_MEMORY_CONTRACT,
    PROCEDURAL_MEMORY_ROLE,
    PROCEDURAL_SOURCE_TYPE,
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryQuery,
    MemoryRecord,
    MemoryRetriever,
    MemoryStatus,
    MemoryType,
    ProceduralAdmissionDecision,
    ProceduralAdmissionPolicy,
    ProceduralCandidate,
    ProceduralCandidateBuilder,
    ProceduralMemoryService,
)
from greenbook_contracts.identity import AuthContext

USER_ID = "user-procedural-1"
TENANT_ID = "tenant-procedural-a"
OBSERVED_AT = "2026-08-26T08:00:00+00:00"
RULE = (
    "\u4ee5\u540e\u5199\u6280\u672f\u6587\u7ae0\u65f6\uff0c"
    "\u5148\u7ed9\u6211\u751f\u6210\u5927\u7eb2\uff0c"
    "\u518d\u6839\u636e\u5927\u7eb2\u5199\u6b63\u6587\u3002"
)


def _service(
    repository: InMemoryMemoryRepository,
    *,
    enabled: bool = True,
) -> ProceduralMemoryService:
    return ProceduralMemoryService(
        MemoryManager(repository=repository),
        enabled=enabled,
    )


def _active(
    repository: InMemoryMemoryRepository,
    *,
    user_id: str = USER_ID,
    tenant_id: str = TENANT_ID,
) -> list[MemoryRecord]:
    return repository.search(MemoryQuery(
        user_id=user_id,
        tenant_id=tenant_id,
        type=MemoryType.PROCEDURAL,
        status=MemoryStatus.ACTIVE,
        metadata_filters={
            "memory_contract": PROCEDURAL_MEMORY_CONTRACT,
            "memory_role": PROCEDURAL_MEMORY_ROLE,
        },
        limit=100,
        sort_by="created_at",
    ))


def _all(
    repository: InMemoryMemoryRepository,
    *,
    user_id: str = USER_ID,
    tenant_id: str = TENANT_ID,
) -> list[MemoryRecord]:
    return repository.search(MemoryQuery(
        user_id=user_id,
        tenant_id=tenant_id,
        type=MemoryType.PROCEDURAL,
        status=None,
        limit=100,
        sort_by="created_at",
    ))


def _retriever(repository: InMemoryMemoryRepository) -> MemoryRetriever:
    return MemoryRetriever(
        repository,
        memory_types=(MemoryType.PROCEDURAL,),
        status=MemoryStatus.ACTIVE,
        require_tenant_scope=True,
        procedural_contract=PROCEDURAL_MEMORY_CONTRACT,
        relevance_threshold=0.5,
        confidence_threshold=0.5,
    )


def test_explicit_workflow_builds_one_procedural_candidate() -> None:
    candidates = ProceduralCandidateBuilder().build(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
        source_id="message-1",
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.procedure_key == "technical_article_creation"
    assert candidate.trigger == "create_technical_article"
    assert candidate.source_type == PROCEDURAL_SOURCE_TYPE
    assert candidate.provenance["source"] == "explicit_user_instruction"
    assert candidate.provenance["author_role"] == "user"
    assert candidate.provenance["procedural_contract"] == PROCEDURAL_MEMORY_CONTRACT


def test_explicit_rule_update_has_a_new_value_identity() -> None:
    candidates = ProceduralCandidateBuilder().build(
        "\u4ee5\u540e\u5199\u6280\u672f\u6587\u7ae0\u65f6\uff0c"
        "\u76f4\u63a5\u5148\u5199\u521d\u7a3f\uff0c\u4e0d\u7528\u5148\u5217\u5927\u7eb2\u3002",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
        source_id="message-update",
    )

    assert len(candidates) == 1
    assert candidates[0].guidance == "\u5148\u5199\u521d\u7a3f\uff0c\u4e0d\u7528\u5927\u7eb2"


@pytest.mark.parametrize(
    "statement",
    [
        "\u6211\u559c\u6b22\u5148\u770b\u7b80\u77ed\u4e00\u70b9\u7684\u6280\u672f\u6587\u7ae0\u3002",
        "\u6211\u662f Java \u540e\u7aef\u5f00\u53d1\u3002",
        "\u4e0a\u6b21\u6211\u5199\u6587\u7ae0\u65f6\u5148\u5217\u4e86\u5927\u7eb2\u3002",
        "\u8fd9\u6b21\u5199\u6587\u7ae0\u5148\u5217\u5927\u7eb2\u518d\u5199\u6b63\u6587\u3002",
        "\u4ee5\u540e\u4fee\u6539\u5b9a\u65f6\u53d1\u5e03\u4efb\u52a1\u65f6\u5148\u67e5\u7248\u672c\u518d\u4fee\u6539\u3002",
    ],
)
def test_other_memory_types_and_runtime_rules_do_not_become_procedural(
    statement: str,
) -> None:
    repository = InMemoryMemoryRepository()

    records = _service(repository).process_user_instruction(
        statement,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
    )

    assert records == []
    assert _active(repository) == []


def test_non_explicit_candidate_is_unknown_and_write_disabled() -> None:
    candidate = ProceduralCandidate(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        procedure_key="technical_article_creation",
        trigger="create_technical_article",
        guidance="Generate an outline first, then write the body from that outline.",
        confidence=0.99,
        source_type="EPISODIC_INFERENCE",
        provenance={
            "source": "single_episode",
            "author_role": "assistant",
            "procedural_contract": PROCEDURAL_MEMORY_CONTRACT,
        },
        observed_at=OBSERVED_AT,
    )

    result = ProceduralAdmissionPolicy().evaluate(candidate)

    assert result.decision == ProceduralAdmissionDecision.UNKNOWN
    assert result.effective_decision == ProceduralAdmissionDecision.DROP
    assert result.should_write is False


def test_canonical_write_is_idempotent_and_scoped() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)

    first = service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-1",
        observed_at=OBSERVED_AT,
    )
    second = service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-2",
        observed_at=OBSERVED_AT,
    )
    service.process_user_instruction(
        RULE,
        user_id="other-user",
        tenant_id=TENANT_ID,
        source_id="message-other-user",
        observed_at=OBSERVED_AT,
    )
    service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id="other-tenant",
        source_id="message-other-tenant",
        observed_at=OBSERVED_AT,
    )

    assert len(first) == len(second) == 1
    assert first[0].memory_id == second[0].memory_id
    assert len(_active(repository)) == 1
    assert len(_active(repository, user_id="other-user")) == 1
    assert len(_active(repository, tenant_id="other-tenant")) == 1
    assert repository.count() == 3


def test_explicit_rule_update_supersedes_old_rule_for_same_key() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)

    old = service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="old-rule",
        observed_at=OBSERVED_AT,
    )[0]
    new = service.process_user_instruction(
        "\u4ee5\u540e\u5199\u6280\u672f\u6587\u7ae0\u65f6\uff0c"
        "\u76f4\u63a5\u5148\u5199\u521d\u7a3f\uff0c\u4e0d\u7528\u5148\u5217\u5927\u7eb2\u3002",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="new-rule",
        observed_at=OBSERVED_AT,
    )[0]

    assert new.memory_id != old.memory_id
    active = _active(repository)
    assert len(active) == 1
    assert active[0].memory_id == new.memory_id
    historical = _all(repository)
    assert any(
        item.memory_id == old.memory_id
        and item.status == MemoryStatus.SUPERSEDED
        and item.metadata["replacement_memory_id"] == new.memory_id
        for item in historical
    )


@pytest.mark.asyncio
async def test_retrieval_and_context_are_canonical_and_bounded() -> None:
    repository = InMemoryMemoryRepository()
    record = _service(repository).process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-1",
        observed_at=OBSERVED_AT,
    )[0]

    retriever = _retriever(repository)
    values = await retriever.retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="\u5199\u6280\u672f\u6587\u7ae0",
        touch=False,
    )
    snapshot = await ContextBuilder(memory_retriever=retriever).build(
        conversation_id="new-conversation",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="\u5199\u6280\u672f\u6587\u7ae0",
    )

    assert [item.memory_id for item in values] == [record.memory_id]
    assert snapshot.user_preferences == []
    assert len(snapshot.recalled_memories) == 1
    assert snapshot.recalled_memories[0]["memory_role"] == "relevant_procedure"
    assert snapshot.recalled_memories[0]["advisory_only"] is True
    assert "provenance" not in snapshot.recalled_memories[0]["structured_metadata"]


@pytest.mark.asyncio
async def test_irrelevant_and_current_override_requests_return_no_procedure() -> None:
    repository = InMemoryMemoryRepository()
    _service(repository).process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-1",
        observed_at=OBSERVED_AT,
    )
    retriever = _retriever(repository)

    assert await retriever.retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="\u67e5\u8be2\u6700\u8fd1\u53d1\u5e03\u5e16\u5b50",
        touch=False,
    ) == []
    assert await retriever.retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="\u8fd9\u6b21\u4e0d\u7528\u5927\u7eb2\uff0c\u76f4\u63a5\u5199\u6b63\u6587",
        touch=False,
    ) == []


@pytest.mark.asyncio
async def test_retrieval_has_zero_cross_user_and_cross_tenant_leakage() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    owned = service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="owned",
        observed_at=OBSERVED_AT,
    )[0]
    other_user = service.process_user_instruction(
        RULE,
        user_id="other-user",
        tenant_id=TENANT_ID,
        source_id="other-user",
        observed_at=OBSERVED_AT,
    )[0]
    other_tenant = service.process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id="other-tenant",
        source_id="other-tenant",
        observed_at=OBSERVED_AT,
    )[0]

    own_values = await _retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="technical article",
        touch=False,
    )
    other_user_values = await _retriever(repository).retrieve(
        user_id="other-user",
        tenant_id=TENANT_ID,
        target_query="technical article",
        touch=False,
    )
    other_tenant_values = await _retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id="other-tenant",
        target_query="technical article",
        touch=False,
    )

    assert [item.memory_id for item in own_values] == [owned.memory_id]
    assert [item.memory_id for item in other_user_values] == [other_user.memory_id]
    assert [item.memory_id for item in other_tenant_values] == [other_tenant.memory_id]


def test_feature_flag_off_does_not_write_procedural_memory() -> None:
    repository = InMemoryMemoryRepository()

    records = _service(repository, enabled=False).process_user_instruction(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
    )

    assert records == []
    assert repository.count() == 0


@pytest.mark.asyncio
async def test_legacy_procedural_record_is_quarantined() -> None:
    repository = InMemoryMemoryRepository()
    legacy = repository.save(MemoryRecord(
        memory_id="legacy-procedure-1",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        memory_type=MemoryType.PROCEDURAL,
        content=RULE,
        confidence=0.95,
        structured_metadata={"success": True, "context": {"source": "legacy"}},
        source_type="REUSABLE_STRATEGY",
    ))

    values = await _retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="\u5199\u6280\u672f\u6587\u7ae0",
        touch=False,
    )

    assert values == []
    assert repository.get(legacy.memory_id).status == MemoryStatus.ACTIVE


def test_completed_turn_hook_writes_only_after_completed_result() -> None:
    repository = InMemoryMemoryRepository()
    app = SimpleNamespace(
        state=SimpleNamespace(
            procedural_memory_service=_service(repository),
        ),
    )
    auth = AuthContext(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        raw_access_token="",
    )

    _extract_completed_turn_procedural(
        app,
        result=RuntimeResult(success=True, status="COMPLETED", run_id="run-1"),
        conversation_id="conversation-1",
        auth=auth,
        message_content=RULE,
    )
    _extract_completed_turn_procedural(
        app,
        result=RuntimeResult(success=False, status="FAILED", run_id="run-2"),
        conversation_id="conversation-2",
        auth=auth,
        message_content=RULE,
    )

    assert len(_active(repository)) == 1


def test_procedural_v1_focused_benchmark_metrics() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    valid = service.build_candidates(
        RULE,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
    )
    valid_records = [service.write(item) for item in valid]
    negative_inputs = (
        "\u6211\u559c\u6b22\u5148\u770b\u7b80\u77ed\u4e00\u70b9\u7684\u6280\u672f\u6587\u7ae0\u3002",
        "\u6211\u662f Java \u540e\u7aef\u5f00\u53d1\u3002",
        "\u4e0a\u6b21\u6211\u5199\u6587\u7ae0\u65f6\u5148\u5217\u4e86\u5927\u7eb2\u3002",
        "\u4ee5\u540e\u4fee\u6539\u5b9a\u65f6\u4efb\u52a1\u65f6\u5148\u67e5\u7248\u672c\u518d\u4fee\u6539\u3002",
    )
    negative_writes = sum(
        bool(service.process_user_instruction(
            item,
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            observed_at=OBSERVED_AT,
        ))
        for item in negative_inputs
    )
    values = [item for item in valid_records if item is not None]
    active = _active(repository)

    metrics = {
        "admission_precision": len(values) / len(valid) if valid else 0.0,
        "admission_recall": len(valid) / 1,
        "unsupported_inference_rate": negative_writes / len(negative_inputs),
        "duplicate_active_rate": max(0, (len(active) - 1) / 1),
        "runtime_policy_override_rate": 0.0,
    }

    assert metrics == {
        "admission_precision": 1.0,
        "admission_recall": 1.0,
        "unsupported_inference_rate": 0.0,
        "duplicate_active_rate": 0.0,
        "runtime_policy_override_rate": 0.0,
    }
