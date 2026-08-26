from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.api.routes import _extract_completed_turn_semantic
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.memory import (
    SEMANTIC_MEMORY_CONTRACT,
    SEMANTIC_MEMORY_ROLE,
    SEMANTIC_SOURCE_TYPE,
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryQuery,
    MemoryRecord,
    MemoryRetriever,
    MemoryStatus,
    MemoryType,
    PreferenceRetriever,
    SemanticAdmissionDecision,
    SemanticAdmissionPolicy,
    SemanticCandidate,
    SemanticCandidateBuilder,
    SemanticMemoryService,
)
from greenbook_contracts.identity import AuthContext

USER_ID = "user-semantic-1"
TENANT_ID = "tenant-semantic-a"
OBSERVED_AT = "2026-08-26T08:00:00+00:00"


def _service(
    repository: InMemoryMemoryRepository,
    *,
    enabled: bool = True,
) -> SemanticMemoryService:
    return SemanticMemoryService(
        MemoryManager(repository=repository),
        enabled=enabled,
    )


def _active_semantic(
    repository: InMemoryMemoryRepository,
    *,
    user_id: str = USER_ID,
    tenant_id: str = TENANT_ID,
) -> list[MemoryRecord]:
    return repository.search(MemoryQuery(
        user_id=user_id,
        tenant_id=tenant_id,
        type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE,
        metadata_filters={
            "memory_contract": SEMANTIC_MEMORY_CONTRACT,
            "memory_role": SEMANTIC_MEMORY_ROLE,
        },
        limit=100,
        sort_by="created_at",
    ))


def _all_semantic(
    repository: InMemoryMemoryRepository,
    *,
    user_id: str = USER_ID,
    tenant_id: str = TENANT_ID,
) -> list[MemoryRecord]:
    return repository.search(MemoryQuery(
        user_id=user_id,
        tenant_id=tenant_id,
        type=MemoryType.SEMANTIC,
        status=None,
        limit=100,
        sort_by="created_at",
    ))


def _strict_retriever(
    repository: InMemoryMemoryRepository,
    *,
    semantic_only: bool = True,
) -> MemoryRetriever:
    return MemoryRetriever(
        repository,
        memory_types=(MemoryType.SEMANTIC,),
        status=MemoryStatus.ACTIVE,
        include_legacy_episodic=False,
        require_tenant_scope=True,
        semantic_contract=SEMANTIC_MEMORY_CONTRACT,
        include_preference_alias=not semantic_only,
        relevance_threshold=0.5,
        confidence_threshold=0.5,
    )


def test_explicit_statement_builds_only_supported_semantic_facts() -> None:
    candidates = SemanticCandidateBuilder().build(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
        source_id="message-1",
    )

    assert {
        (item.subject, item.predicate, item.object)
        for item in candidates
    } == {
        ("user", "occupation_domain", "java_backend"),
        ("user", "learning_focus", "ai_agent"),
    }
    assert all(item.source_type == SEMANTIC_SOURCE_TYPE for item in candidates)
    assert all(
        item.provenance["source"] == "explicit_user_statement"
        and item.provenance["author_role"] == "user"
        for item in candidates
    )
    assert all(
        runtime_id not in item.normalized_fact
        for item in candidates
        for runtime_id in ("run_id", "execution_id", "operation_id", "draft_id")
    )


def test_duplicate_explicit_fact_keeps_one_active_record() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)

    first = service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-1",
        observed_at=OBSERVED_AT,
    )
    second = service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-2",
        observed_at=OBSERVED_AT,
    )

    assert len(first) == len(second) == 2
    active = _active_semantic(repository)
    assert len(active) == 2
    assert {item.memory_id for item in first} == {item.memory_id for item in second}
    assert all(item.metadata.get("evidence_count") == 2 for item in active)


def test_explicit_update_supersedes_old_fact_for_same_predicate() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)

    service.process_user_statement(
        "我现在主要学习 Java。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-java",
        observed_at=OBSERVED_AT,
    )
    service.process_user_statement(
        "我现在主要学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-agent",
        observed_at=OBSERVED_AT,
    )

    active_learning = [
        item for item in _active_semantic(repository)
        if item.metadata.get("predicate") == "learning_focus"
    ]
    all_learning = [
        item for item in _all_semantic(repository)
        if item.metadata.get("predicate") == "learning_focus"
    ]
    assert len(active_learning) == 1
    assert active_learning[0].metadata["object"] == "ai_agent"
    assert any(
        item.metadata.get("object") == "java"
        and item.status == MemoryStatus.SUPERSEDED
        for item in all_learning
    )


def test_different_predicate_is_not_superseded_by_fact_update() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)

    service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Java。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-initial",
        observed_at=OBSERVED_AT,
    )
    service.process_user_statement(
        "我现在主要学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-update",
        observed_at=OBSERVED_AT,
    )

    active = _active_semantic(repository)
    occupation = [
        item for item in active
        if item.metadata.get("predicate") == "occupation_domain"
    ]
    learning = [
        item for item in active
        if item.metadata.get("predicate") == "learning_focus"
    ]
    assert len(occupation) == len(learning) == 1
    assert occupation[0].metadata["object"] == "java_backend"
    assert learning[0].metadata["object"] == "ai_agent"


@pytest.mark.parametrize(
    "statement",
    [
        "我喜欢 Java 内容。",
        "我昨天发布了一篇 Java 帖子。",
        "当前正在发布 Java 帖子。",
        "我写 Java 帖子，所以我可能是 Java 后端开发者。",
    ],
)
def test_preference_history_task_and_inference_do_not_become_semantic(
    statement: str,
) -> None:
    repository = InMemoryMemoryRepository()

    records = _service(repository).process_user_statement(
        statement,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="negative-case",
        observed_at=OBSERVED_AT,
    )

    assert records == []
    assert _active_semantic(repository) == []


def test_non_explicit_candidate_is_unknown_and_write_disabled() -> None:
    candidate = SemanticCandidate(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        subject="user",
        predicate="occupation_domain",
        object="java_backend",
        normalized_fact="The user is a Java backend developer.",
        confidence=0.99,
        source_type="LLM_INFERRED_PROFILE",
        provenance={
            "source": "model_inference",
            "author_role": "assistant",
        },
        observed_at=OBSERVED_AT,
    )

    result = SemanticAdmissionPolicy().evaluate(candidate)

    assert result.decision == SemanticAdmissionDecision.UNKNOWN
    assert result.effective_decision == SemanticAdmissionDecision.DROP
    assert result.should_write is False


def test_feature_flag_off_keeps_semantic_storage_unchanged() -> None:
    repository = InMemoryMemoryRepository()

    records = _service(repository, enabled=False).process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
    )

    assert records == []
    assert repository.count() == 0


@pytest.mark.asyncio
async def test_retrieval_is_cross_session_scoped_and_context_labels_fact() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    owned = service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="old-conversation-message",
        observed_at=OBSERVED_AT,
    )
    service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id="other-user",
        tenant_id=TENANT_ID,
        source_id="other-user-message",
        observed_at=OBSERVED_AT,
    )
    service.process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id="other-tenant",
        source_id="other-tenant-message",
        observed_at=OBSERVED_AT,
    )

    retriever = _strict_retriever(repository)
    values = await retriever.retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="Agent",
        touch=False,
    )
    snapshot = await ContextBuilder(memory_retriever=retriever).build(
        conversation_id="new-conversation",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="Agent",
    )
    provider_context = project_interpreter_context(snapshot)

    assert {item.memory_id for item in values} == {
        item.memory_id for item in owned if item.metadata["predicate"] == "learning_focus"
    }
    assert snapshot.user_preferences == []
    assert snapshot.recalled_memories[0]["memory_role"] == "relevant_fact"
    assert provider_context["recalled_memories"][0]["memory_role"] == "relevant_fact"
    assert snapshot.memory_ids_used == [owned[1].memory_id]
    assert all(
        item["memory_id"] == owned[1].memory_id
        for item in snapshot.recalled_memories
    )


@pytest.mark.asyncio
async def test_irrelevant_query_returns_no_semantic_memory() -> None:
    repository = InMemoryMemoryRepository()
    _service(repository).process_user_statement(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="message-1",
        observed_at=OBSERVED_AT,
    )

    values = await _strict_retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="查一下最近帖子",
        touch=False,
    )

    assert values == []


@pytest.mark.asyncio
async def test_preference_alias_does_not_return_semantic_as_preference() -> None:
    repository = InMemoryMemoryRepository()
    preference = repository.save(MemoryRecord(
        memory_id="preference-1",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        memory_type=MemoryType.PREFERENCE,
        content="The user likes Java content.",
        confidence=0.95,
        structured_metadata={
            "preference_type": "content_topic",
            "value": "Java",
        },
    ))
    semantic = _service(repository).process_user_statement(
        "我是 Java 后端开发。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        source_id="semantic-1",
        observed_at=OBSERVED_AT,
    )[0]

    preferences = await PreferenceRetriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        query="Java backend",
        touch=False,
    )
    semantic_only = await _strict_retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="Java backend",
        touch=False,
    )

    assert [item.memory_id for item in preferences] == [preference.memory_id]
    assert semantic.memory_id not in {item.memory_id for item in preferences}
    assert [item.memory_id for item in semantic_only] == [semantic.memory_id]


@pytest.mark.asyncio
async def test_legacy_semantic_record_is_quarantined_from_canonical_v1() -> None:
    repository = InMemoryMemoryRepository()
    legacy = repository.save(MemoryRecord(
        memory_id="legacy-semantic-1",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        memory_type=MemoryType.SEMANTIC,
        content="The user is learning AI Agent.",
        confidence=0.95,
        structured_metadata={
            "predicate": "learning_focus",
            "object": "ai_agent",
        },
    ))

    values = await _strict_retriever(repository).retrieve(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        target_query="Agent",
        touch=False,
    )

    assert values == []
    assert repository.get(legacy.memory_id).status == MemoryStatus.ACTIVE


def test_completed_turn_hook_writes_only_after_completed_result() -> None:
    repository = InMemoryMemoryRepository()
    app = SimpleNamespace(
        state=SimpleNamespace(
            semantic_memory_service=_service(repository),
        ),
    )
    auth = AuthContext(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        raw_access_token="",
    )

    _extract_completed_turn_semantic(
        app,
        result=RuntimeResult(success=True, status="COMPLETED", run_id="run-1"),
        conversation_id="conversation-1",
        auth=auth,
        message_content="我是 Java 后端开发，现在在学习 Agent。",
    )
    _extract_completed_turn_semantic(
        app,
        result=RuntimeResult(success=False, status="FAILED", run_id="run-2"),
        conversation_id="conversation-2",
        auth=auth,
        message_content="我现在主要学习 Java。",
    )

    assert len(_active_semantic(repository)) == 2


def test_semantic_v1_focused_benchmark_metrics() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    valid = service.build_candidates(
        "我是 Java 后端开发，现在在学习 Agent。",
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        observed_at=OBSERVED_AT,
    )
    valid_records = [service.write(item) for item in valid]
    negative_inputs = (
        "我喜欢 Java 内容。",
        "我昨天发布了一篇 Java 帖子。",
        "当前正在发布 Java 帖子。",
        "我写 Java 帖子，所以我可能是 Java 后端开发者。",
    )
    negative_writes = sum(
        bool(service.process_user_statement(
            item,
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            observed_at=OBSERVED_AT,
        ))
        for item in negative_inputs
    )
    values = [item for item in valid_records if item is not None]

    metrics = {
        "fact_extraction_precision": len(valid) / 2,
        "fact_extraction_recall": len(valid) / 2,
        "unsupported_inference_rate": negative_writes / len(negative_inputs),
        "duplicate_active_fact_rate": (
            (len(_active_semantic(repository)) - 2) / 2
        ),
    }

    assert len(values) == 2
    assert metrics == {
        "fact_extraction_precision": 1.0,
        "fact_extraction_recall": 1.0,
        "unsupported_inference_rate": 0.0,
        "duplicate_active_fact_rate": 0.0,
    }
