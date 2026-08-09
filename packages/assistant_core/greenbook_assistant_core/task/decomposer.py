"""TaskDecomposer — split a composite user message into SubTaskContexts.

Phase 6.0.1: decomposition only.  No execution, no GroupExecutor.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


# ── SubTaskContext ───────────────────────────────────────────────────

class SubTaskContext(BaseModel):
    """One sub-task extracted from a composite user message."""

    sub_index: int = 0
    user_message: str = ""
    task_intent: Any = None          # TaskIntent (lazy import)
    task_id: str = ""                # assigned after execution (Phase 6.1)
    resolved_resources: Any = None   # ResourceResolutionResult
    result: Any = None               # RuntimeResult

    # cross-task reference
    depends_on_task_index: int | None = None
    depends_on_hint: str = ""        # "第一篇文章"

    # Phase 6.1: resolved dependency resources (injected into sub_ctx)
    dependency_resources: dict[str, str] = {}
    # {"draft_id": "draft-a", "schedule_id": "sched-a"}


class TaskDependency(BaseModel):
    """dependent_task depends on source_task."""
    dependent_task_index: int        # 引用方 (Task C)
    source_task_index: int           # 被引用方 (Task A)
    hint: str = ""                   # "第一篇文章"
    ref_type: str = "ordinal"


class TaskGroup(BaseModel):
    """A group of SubTasks with cross-references."""
    group_id: str = ""
    sub_tasks: list[SubTaskContext] = []
    dependencies: list[TaskDependency] = []


# ── TaskDecomposer ───────────────────────────────────────────────────

# Sentence-level split markers: after sentence/strong-pause boundary
_SPLIT_PATTERN = re.compile(
    r"(?:^|[。！\n])\s*"
    r"(?:然后|接着|再(?!次)|最后|另外|同时|此外|并且|还有|其次|接下来)\s*"
    r"(?:帮我|请|再|顺便|麻烦|帮忙|来|去|你)?\s*"
)

# Weak split: comma followed by action verb
_WEAK_SPLIT = re.compile(
    r"(?:，|,)\s*"
    r"(?:再|另外|此外|还有|然后|接着|最后)\s*"
    r"(?:帮我|请|再|顺便|麻烦)?\s*"
)

# Numbered list pattern: "1. xxx" or "1) xxx"
_NUMBERED_PATTERN = re.compile(
    r"(?:^|\n)\s*\d+\s*[.、)）]\s*"
)

# Ordinal reference: "第一篇文章", "第二个任务"
_ORDINAL_REF = re.compile(
    r"第\s*([一二三四五六七八九十\d]+)\s*[个篇条项次]"
)

# Ordinal character → int
_ORDINAL_MAP: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


class TaskDecomposer:
    """Split a user message into independent SubTaskContexts.

    Deterministic — zero LLM calls.  Each candidate chunk is validated
    through the existing TaskUnderstanding (L1 fast path) to decide
    whether it can stand alone as a task.
    """

    # ── public API ───────────────────────────────────────────────

    async def decompose(
        self,
        user_message: str,
        tu: Any,  # TaskUnderstanding
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> list[SubTaskContext]:
        """Return SubTaskContext list.  Length 1 = no split needed."""

        # 1. Split
        chunks = self._split(user_message)
        if len(chunks) <= 1:
            intent = await tu.understand(
                user_message, existing_tasks=existing_tasks,
            )
            return [SubTaskContext(sub_index=0, user_message=user_message,
                                   task_intent=intent)]

        # 2. Analyse each chunk independently
        analysed: list[dict[str, Any]] = []
        for chunk in chunks:
            intent = await tu.understand(
                chunk, existing_tasks=existing_tasks,
            )
            analysed.append({"text": chunk, "intent": intent})

        # 3. Merge non-standalone chunks
        merged = self._merge_dependents(analysed)
        if len(merged) <= 1:
            # Everything merged back → single task (reuse chunk intent if available)
            intent = merged[0]["intent"] if merged else None
            if intent is None:
                intent = await tu.understand(
                    user_message, existing_tasks=existing_tasks,
                )
            return [SubTaskContext(sub_index=0, user_message=user_message,
                                   task_intent=intent)]

        # 4. Build SubTaskContexts + detect cross-references
        sub_tasks: list[SubTaskContext] = []
        for i, m in enumerate(merged):
            st = SubTaskContext(
                sub_index=i,
                user_message=m["text"],
                task_intent=m["intent"],
            )
            # Detect ordinal cross-reference
            if m_ord := _ORDINAL_REF.search(m["text"]):
                ref_val = m_ord.group(1)
                ref_idx = _parse_ordinal(ref_val) - 1  # 0-based
                if 0 <= ref_idx < i:  # references a PREVIOUS sub-task
                    st.depends_on_task_index = ref_idx
                    st.depends_on_hint = m_ord.group(0)
            sub_tasks.append(st)

        return sub_tasks

    # ── splitting ────────────────────────────────────────────────

    @staticmethod
    def _split(text: str) -> list[str]:
        """Split on sentence-level markers."""
        trimmed = text.strip()

        # Try numbered list first
        numbered_chunks = _NUMBERED_PATTERN.split(trimmed)
        if len(numbered_chunks) > 1:
            return _clean_chunks(numbered_chunks)

        # Try strong sentence markers
        parts = _SPLIT_PATTERN.split(trimmed)
        if len(parts) > 1:
            return _clean_chunks(parts)

        # Try weak comma markers
        parts = _WEAK_SPLIT.split(trimmed)
        if len(parts) > 1:
            return _clean_chunks(parts)

        return [trimmed]

    # ── merging ──────────────────────────────────────────────────

    @staticmethod
    def _merge_dependents(
        analysed: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge chunks that can't stand alone into the previous chunk."""
        if not analysed:
            return []

        merged: list[dict[str, Any]] = [analysed[0]]

        for item in analysed[1:]:
            if _is_standalone(item["intent"]):
                merged.append(item)
            else:
                # Merge into previous
                merged[-1]["text"] += "。" + item["text"]
                # Keep the previous intent (dominant)

        return merged


# ── helpers ──────────────────────────────────────────────────────────

def _is_standalone(intent: Any) -> bool:
    """Can this intent stand as an independent Task?"""
    if intent is None:
        return False
    gc = getattr(intent, "goal_category", "QUERY_INFO")
    rel = getattr(intent, "relation", "DIRECT")
    # Not DIRECT and not QUERY_INFO → has a real goal
    return gc != "QUERY_INFO" and rel != "DIRECT"


def _clean_chunks(parts: list[str]) -> list[str]:
    return [p.strip().rstrip("。，,!！？?") for p in parts if p.strip()]


def _parse_ordinal(val: str) -> int:
    if val.isdigit():
        return int(val)
    return _ORDINAL_MAP.get(val, 1)
