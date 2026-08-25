"""Structured extractors for reusable Memory records."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import MemoryRecord, MemoryStatus, MemoryType
from .policy import MemoryWriteDecision


class PreferenceExtraction(BaseModel):
    """Structured classifier output for one completed Conversation turn."""

    memory_type: Literal["preference"] = "preference"
    content: str = ""
    preference_key: str = ""
    preference_value: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_long_term: bool = False
    decision: MemoryWriteDecision = MemoryWriteDecision.SKIP
    reason: str = ""

    @property
    def should_write(self) -> bool:
        return self.decision == MemoryWriteDecision.WRITE and self.is_long_term


class PreferenceMemoryExtractor:
    """Classify explicit durable preferences without saving raw task requests.

    This MVP is deterministic and returns the same structured contract a
    future LLM structured-output adapter must satisfy.  It intentionally
    recognizes a small, high-confidence vocabulary rather than guessing a
    preference from every message.
    """

    _TRANSIENT_MARKERS = (
        "今天",
        "明天",
        "这次",
        "当前",
        "帮我",
        "请写",
        "写一篇",
        "发布",
        "创建",
        "生成",
        "安排",
        "today",
        "tomorrow",
        "this time",
        "for this",
        "write a",
        "create a",
        "publish",
        "schedule",
    )
    _DURABLE_MARKERS = (
        "以后",
        "今后",
        "从现在起",
        "请记住",
        "记住",
        "长期",
        "默认",
        "习惯",
        "喜欢",
        "偏好",
        "更喜欢",
        "prefer",
        "from now on",
        "always",
        "usually",
        "my preferred",
        "i use",
    )

    @classmethod
    def extract(cls, user_message: str) -> PreferenceExtraction:
        text = " ".join(str(user_message or "").split()).strip()
        folded = text.casefold()
        if len(text) < 4:
            return cls._skip("invalid_empty_or_short_input")

        has_durable_marker = any(marker in folded for marker in cls._DURABLE_MARKERS)
        has_transient_marker = any(marker in folded for marker in cls._TRANSIENT_MARKERS)
        if has_transient_marker and not has_durable_marker:
            return cls._skip("one_off_task_or_time_bound_request")

        candidate = cls._match_known_preference(text, folded)
        if candidate is None:
            return cls._skip("no_high_confidence_long_term_preference")
        return candidate

    @classmethod
    def to_record(
        cls,
        extraction: PreferenceExtraction,
        *,
        user_id: str,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
    ) -> MemoryRecord | None:
        if not extraction.should_write:
            return None
        source_id = cls.source_id(
            conversation_id=conversation_id,
            user_message=user_message,
        )
        return MemoryRecord(
            user_id=user_id,
            tenant_id=tenant_id,
            source_conversation_id=conversation_id or None,
            memory_type=MemoryType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            content=extraction.content,
            structured_metadata={
                "preference_type": extraction.preference_key,
                "value": extraction.preference_value,
                "source": "EXTRACTED",
                "extraction_reason": extraction.reason,
                "is_long_term": True,
            },
            importance=max(0.5, min(extraction.confidence * 0.9, 0.95)),
            confidence=extraction.confidence,
            source_type="CONVERSATION_PREFERENCE_EXTRACTION",
            source_id=source_id,
        )

    @staticmethod
    def source_id(*, conversation_id: str, user_message: str) -> str:
        digest = hashlib.sha256(
            f"{conversation_id}\n{user_message}".encode()
        ).hexdigest()[:32]
        return f"preference:{digest}"

    @classmethod
    def _match_known_preference(
        cls,
        text: str,
        folded: str,
    ) -> PreferenceExtraction | None:
        title_avoidance = (
            ("标题" in text or "title" in folded)
            and any(value in folded for value in ("夸张", "标题党", "clickbait", "exaggerated"))
            and any(value in folded for value in ("不要", "避免", "别", "don't", "avoid"))
        )
        if title_avoidance:
            return cls._write(
                key="title_style",
                value="avoid clickbait titles",
                content="avoid clickbait titles",
                confidence=0.92,
                reason="explicit durable title-style instruction",
            )

        deep_content = any(
            value in folded
            for value in (
                "技术深度",
                "深度文章",
                "深度技术",
                "technical depth",
                "deep technical",
                "in-depth technical",
            )
        ) and any(value in folded for value in ("喜欢", "偏好", "更喜欢", "prefer", "want"))
        if deep_content:
            return cls._write(
                key="writing_depth",
                value="prefer technical deep articles",
                content="prefer technical deep articles",
                confidence=0.9,
                reason="explicit durable content-depth preference",
            )

        concise_replies = (
            any(value in folded for value in ("简洁回复", "回复简洁", "concise replies", "brief replies"))
            and any(value in folded for value in ("喜欢", "偏好", "更喜欢", "prefer", "want", "以后", "默认"))
        )
        if concise_replies:
            return cls._write(
                key="response_style",
                value="prefer concise replies",
                content="prefer concise replies",
                confidence=0.9,
                reason="explicit durable response-style preference",
            )

        java_or_python = re.search(r"(?i)(?<![A-Za-z])(java|python)(?![A-Za-z])", text)
        stack_signal = any(
            value in folded
            for value in ("技术栈", "使用", "采用", "technology stack", "i use", "use")
        )
        if java_or_python and stack_signal:
            language = java_or_python.group(1)
            return cls._write(
                key="technology_stack",
                value=f"use {language.title()} technology stack",
                content=f"use {language.title()} technology stack",
                confidence=0.88,
                reason="explicit durable technology-stack preference",
            )
        return None

    @staticmethod
    def _skip(reason: str) -> PreferenceExtraction:
        return PreferenceExtraction(decision=MemoryWriteDecision.SKIP, reason=reason)

    @staticmethod
    def _write(
        *,
        key: str,
        value: str,
        content: str,
        confidence: float,
        reason: str,
    ) -> PreferenceExtraction:
        return PreferenceExtraction(
            content=content,
            preference_key=key,
            preference_value=value,
            confidence=confidence,
            is_long_term=True,
            decision=MemoryWriteDecision.WRITE,
            reason=reason,
        )


class PreferenceMemoryService:
    """Run extraction at a completed-turn boundary and persist only writes."""

    def __init__(
        self,
        memory_manager: Any,
        *,
        extractor: type[PreferenceMemoryExtractor] = PreferenceMemoryExtractor,
    ) -> None:
        self._memory = memory_manager
        self._extractor = extractor

    def process_completed_turn(
        self,
        *,
        user_id: str,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
    ) -> tuple[PreferenceExtraction, MemoryRecord | None]:
        extraction = self._extractor.extract(user_message)
        record = self._extractor.to_record(
            extraction,
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            user_message=user_message,
        )
        if record is not None:
            record = self._memory.remember(record)
        return extraction, record


class ProceduralMemoryExtractor:
    @staticmethod
    def extract(
        *,
        user_id: str,
        goal_category: str = "",
        plan_source: str = "",
        status: str = "",
        tool_count: int = 0,
        step_count: int = 0,
        error_code: str = "",
    ) -> MemoryRecord | None:
        if step_count <= 1 and status == "COMPLETED":
            return None
        success = status == "COMPLETED"
        pattern = ProceduralMemoryExtractor._derive_pattern(
            goal_category, plan_source, success, tool_count, error_code
        )
        return MemoryRecord(
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            content=pattern["description"],
            structured_metadata={
                "pattern": pattern["key"],
                "goal_category": goal_category,
                "plan_source": plan_source,
                "success": success,
                "tool_count": tool_count,
                "step_count": step_count,
                "error_code": error_code,
                "confidence": pattern["confidence"],
            },
            importance=pattern["importance"],
        )

    @staticmethod
    def _derive_pattern(
        goal_category: str,
        plan_source: str,
        success: bool,
        tool_count: int,
        error_code: str,
    ) -> dict[str, Any]:
        key = f"{goal_category}:{plan_source}"
        if success:
            description = f"[{goal_category}] resolved execution succeeded with {tool_count} tool calls"
            return {"key": key, "description": description, "confidence": min(0.5 + tool_count * 0.1, 0.9), "importance": 0.4}
        reason = error_code or "unknown"
        return {
            "key": key,
            "description": f"[{goal_category}] resolved execution failed (reason: {reason})",
            "confidence": 0.2,
            "importance": 0.3,
        }


__all__ = [
    "PreferenceExtraction",
    "PreferenceMemoryExtractor",
    "PreferenceMemoryService",
    "ProceduralMemoryExtractor",
]
