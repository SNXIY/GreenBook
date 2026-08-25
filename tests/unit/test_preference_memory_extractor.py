from __future__ import annotations

from types import SimpleNamespace

from greenbook_agent_api.api.routes import _extract_completed_turn_preference
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryWriteDecision,
    PreferenceMemoryExtractor,
    PreferenceMemoryService,
)
from greenbook_contracts.identity import AuthContext


def test_extractor_returns_structured_long_term_preference() -> None:
    result = PreferenceMemoryExtractor.extract("以后写文章标题不要太夸张")

    assert result.decision == MemoryWriteDecision.WRITE
    assert result.memory_type == "preference"
    assert result.is_long_term is True
    assert result.preference_key == "title_style"
    assert result.preference_value == "avoid clickbait titles"
    assert result.content == "avoid clickbait titles"
    assert result.confidence >= 0.9
    assert result.model_dump()["decision"] == "WRITE"


def test_extractor_supports_other_stable_preferences() -> None:
    deep = PreferenceMemoryExtractor.extract("我喜欢技术深度文章")
    concise = PreferenceMemoryExtractor.extract("以后请给我简洁回复")
    stack = PreferenceMemoryExtractor.extract("我使用Java技术栈")

    assert deep.preference_value == "prefer technical deep articles"
    assert concise.preference_value == "prefer concise replies"
    assert stack.preference_value == "use Java technology stack"
    assert all(item.decision == MemoryWriteDecision.WRITE for item in (deep, concise, stack))


def test_extractor_rejects_one_off_task_requests() -> None:
    values = [
        PreferenceMemoryExtractor.extract("今天写Java文章"),
        PreferenceMemoryExtractor.extract("明天发布文章"),
        PreferenceMemoryExtractor.extract("帮我写一篇Java文章"),
    ]

    assert all(item.decision == MemoryWriteDecision.SKIP for item in values)
    assert all(item.is_long_term is False for item in values)


def test_extractor_rejects_invalid_input_and_unknown_preferences() -> None:
    empty = PreferenceMemoryExtractor.extract("")
    unknown = PreferenceMemoryExtractor.extract("请告诉我今天的天气")

    assert empty.decision == MemoryWriteDecision.SKIP
    assert empty.reason == "invalid_empty_or_short_input"
    assert unknown.decision == MemoryWriteDecision.SKIP
    assert unknown.content == ""


def test_service_persists_only_classified_preferences_with_provenance() -> None:
    repository = InMemoryMemoryRepository()
    service = PreferenceMemoryService(MemoryManager(repository=repository))

    extraction, record = service.process_completed_turn(
        user_id="user-1",
        tenant_id="tenant-a",
        conversation_id="conversation-1",
        user_message="以后写文章标题不要太夸张",
    )

    assert extraction.should_write is True
    assert record is not None
    assert record.tenant_id == "tenant-a"
    assert record.source_conversation_id == "conversation-1"
    assert record.metadata["preference_type"] == "title_style"
    assert record.metadata["source"] == "EXTRACTED"
    assert "以后写文章标题" not in record.content

    _, duplicate = service.process_completed_turn(
        user_id="user-1",
        tenant_id="tenant-a",
        conversation_id="conversation-1",
        user_message="以后写文章标题不要太夸张",
    )
    assert duplicate is not None
    assert duplicate.memory_id == record.memory_id
    assert repository.count() == 1


def test_service_does_not_save_normal_turns_or_one_off_tasks() -> None:
    repository = InMemoryMemoryRepository()
    service = PreferenceMemoryService(MemoryManager(repository=repository))

    extraction, record = service.process_completed_turn(
        user_id="user-1",
        tenant_id="tenant-a",
        conversation_id="conversation-1",
        user_message="帮我写一篇Java文章",
    )

    assert extraction.decision == MemoryWriteDecision.SKIP
    assert record is None
    assert repository.count() == 0


def test_completed_turn_hook_runs_extraction_without_touching_action_loop() -> None:
    repository = InMemoryMemoryRepository()
    service = PreferenceMemoryService(MemoryManager(repository=repository))
    app = SimpleNamespace(state=SimpleNamespace(preference_memory_service=service))
    auth = AuthContext(user_id="user-1", tenant_id="tenant-a", raw_access_token="")

    _extract_completed_turn_preference(
        app,
        result=RuntimeResult(success=True, status="COMPLETED", run_id="run-1"),
        conversation_id="conversation-1",
        auth=auth,
        message_content="以后写文章标题不要太夸张",
    )
    _extract_completed_turn_preference(
        app,
        result=RuntimeResult(success=False, status="FAILED", run_id="run-2"),
        conversation_id="conversation-2",
        auth=auth,
        message_content="以后请给我简洁回复",
    )

    assert repository.count() == 1
