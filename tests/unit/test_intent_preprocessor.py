"""Stage E-2.4 tests for structural intent context hints."""

from greenbook_assistant_core.task.intent_preprocessor import (
    IntentContextHint,
    build_intent_context_hint,
)


def test_complex_operation_context_hint() -> None:
    hint = build_intent_context_hint(
        "帮我运营一个专题：\n"
        "1. 搜索热门文章\n"
        "2. 分析原因\n"
        "3. 如果有旧草稿就优化，没有就创建\n"
        "4. 发布前确认\n"
        "5. 确认后五分钟发布"
    )

    assert hint.has_condition is True
    assert hint.has_multiple_actions is True
    assert hint.has_number_list is True
    assert hint.has_approval is True
    assert hint.has_time_constraint is True
    assert hint.has_reference is True
    assert "搜索" in hint.action_keyword_signals
    assert "分析" in hint.action_keyword_signals


def test_simple_creation_context_hint() -> None:
    hint = build_intent_context_hint("创建一篇 Java 文章")

    assert isinstance(hint, IntentContextHint)
    assert hint.has_condition is False
    assert hint.has_number_list is False
    assert hint.has_multiple_actions is False
    assert "创建" in hint.action_keyword_signals
