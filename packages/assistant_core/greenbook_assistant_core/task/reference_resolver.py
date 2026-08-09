"""TaskReferenceResolver — resolve natural language references to historical Tasks.

Phase 6.2.2-A: standalone module.  No modifications to any existing files.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from .models import ResolvedTaskTarget, Task, TaskIntent


# ── ReferenceHint ────────────────────────────────────────────────────

# Time expressions → (min_seconds_ago, max_seconds_ago)
_TIME_WINDOWS: dict[str, tuple[int, int]] = {
    "刚才": (0, 300),            # < 5 minutes
    "刚刚": (0, 300),
    "今天": (0, 86400),          # < 24 hours
    "昨天": (86400, 172800),     # 24-48 hours ago
    "前天": (172800, 259200),    # 48-72 hours ago
    "上周": (604800, 1209600),   # 7-14 days ago
    "这周": (0, 604800),         # this week
    "上一次": (0, 86400 * 365),  # any time, prefer 2nd most recent
    "上次": (0, 86400 * 365),
    "之前": (0, 86400 * 365),
}

_TIME_PATTERN = re.compile(
    r"(刚才|刚刚|今天|昨天|前天|上周|这周|上一次|上次|之前)"
)

_ORDINAL_PATTERN = re.compile(
    r"第\s*([一二三四五六七八九十\d]+)\s*[个篇条项次]"
)

# Category hints derived from keywords
_KEYWORD_CATEGORY: dict[str, str] = {
    "文章": "CREATE_CONTENT",
    "帖子": "CREATE_CONTENT",
    "发布": "PUBLISH_CONTENT",
    "定时": "PUBLISH_CONTENT",
    "搜索": "ANALYZE_COMMUNITY",
    "分析": "ANALYZE_COMMUNITY",
    "修改": "IMPROVE_CONTENT",
    "创建": "CREATE_CONTENT",
}


class ReferenceHint(BaseModel):
    """Structured decomposition of a natural language reference."""
    raw: str = ""
    time_ref: str = ""              # "昨天" | "刚才" | "上周" | ""
    ordinal: int | None = None      # 1, 2, 3 (第X篇)
    keyword: str = ""               # "文章" | "帖子" | "发布"
    category_hint: str = ""         # derived from keyword → goal_category
    is_temporal_only: bool = False  # "刚才那个" — no content hints


class ReferenceResolution(BaseModel):
    """Result of resolving one natural language reference."""
    hint: ReferenceHint = Field(default_factory=ReferenceHint)
    targets: list[ResolvedTaskTarget] = []
    best_match: ResolvedTaskTarget | None = None
    is_ambiguous: bool = False
    needs_clarification: bool = False
    resolution_path: str = ""       # "task_group" | "conversation"


# ── TaskReferenceResolver ────────────────────────────────────────────

class TaskReferenceResolver:
    """Resolve natural language references to historical Tasks.

    Usage::

        resolver = TaskReferenceResolver()
        result = resolver.resolve("昨天那个文章", tasks)
        if result.best_match:
            print(result.best_match.task_id)
    """

    # ── main entry ───────────────────────────────────────────────

    def resolve(
        self,
        hint_text: str,
        tasks: list[Task],
        *,
        group_context: Any = None,  # TaskGroup | None (Phase 6.1)
    ) -> ReferenceResolution:
        """Resolve *hint_text* against *tasks*."""
        if not hint_text.strip():
            return ReferenceResolution()

        # 1. Parse hint
        hint = self._parse_hint(hint_text)
        if hint is None:
            return ReferenceResolution()

        # 2. TaskGroup ordinal reference
        if hint.ordinal is not None and group_context is not None:
            target = self._resolve_ordinal_in_group(
                hint.ordinal, group_context,
            )
            if target is not None:
                return ReferenceResolution(
                    hint=hint, best_match=target, targets=[target],
                    resolution_path="task_group",
                )

        # 3. Time-filter tasks
        candidates = self._filter_by_time(tasks, hint.time_ref)

        # 4. Keyword + category match
        if hint.keyword or hint.category_hint:
            candidates = self._match_by_keyword_category(
                candidates, hint,
            )

        # 5. Detect ambiguity
        if not candidates:
            # Retry without time filter
            candidates = self._match_by_keyword_category(tasks, hint)

        return self._build_result(hint, candidates)

    # ── parsing ──────────────────────────────────────────────────

    @staticmethod
    def _parse_hint(text: str) -> ReferenceHint | None:
        text = text.strip()
        if not text:
            return None

        hint = ReferenceHint(raw=text)

        # Extract time reference
        if m := _TIME_PATTERN.search(text):
            hint.time_ref = m.group(1)

        # Extract ordinal
        if m := _ORDINAL_PATTERN.search(text):
            val = m.group(1)
            hint.ordinal = _parse_ordinal(val)

        # Extract keyword
        for kw in _KEYWORD_CATEGORY:
            if kw in text:
                hint.keyword = kw
                hint.category_hint = _KEYWORD_CATEGORY[kw]
                break

        # Pure temporal?
        remaining = text
        if hint.time_ref:
            remaining = remaining.replace(hint.time_ref, "")
        if hint.ordinal is not None:
            remaining = _ORDINAL_PATTERN.sub("", remaining)
        remaining = re.sub(r"[的那这该]", "", remaining).strip()
        if len(remaining) < 2:
            hint.is_temporal_only = True

        return hint

    # ── time filtering ───────────────────────────────────────────

    @staticmethod
    def _filter_by_time(tasks: list[Task], time_ref: str) -> list[Task]:
        if not time_ref or time_ref not in _TIME_WINDOWS:
            return list(tasks)

        lo, hi = _TIME_WINDOWS[time_ref]
        now = datetime.now(UTC)
        result: list[Task] = []
        for t in tasks:
            try:
                created = datetime.fromisoformat(t.created_at)
            except (ValueError, TypeError):
                continue
            age = (now - created).total_seconds()
            if lo <= age <= hi:
                result.append(t)
        return result

    # ── keyword + category matching ──────────────────────────────

    @staticmethod
    def _match_by_keyword_category(
        tasks: list[Task], hint: ReferenceHint,
    ) -> list[Task]:
        """Filter tasks by keyword in goal, and/or category match."""
        results: list[Task] = list(tasks)

        # Filter by category if hint specifies one
        if hint.category_hint:
            cat_matches = [t for t in results
                           if t.goal_category == hint.category_hint]
            if cat_matches:
                results = cat_matches
            else:
                return []  # No task matches the required category

        # Filter by keyword in goal
        if hint.keyword and hint.keyword not in ("文章", "帖子", "发布", "定时",
                                                   "搜索", "分析", "修改", "创建"):
            # The keyword is specific content (like "Java"), not a category hint
            kw_matches = [t for t in results
                          if hint.keyword in t.goal]
            if kw_matches:
                results = kw_matches

        return results

    # ── ordinal in group ─────────────────────────────────────────

    @staticmethod
    def _resolve_ordinal_in_group(
        ordinal: int, group: Any,
    ) -> ResolvedTaskTarget | None:
        """Resolve '第一篇文章' within a TaskGroup."""
        try:
            sub_tasks = group.sub_tasks
        except AttributeError:
            return None
        idx = ordinal - 1  # 0-based
        if 0 <= idx < len(sub_tasks):
            sub = sub_tasks[idx]
            if sub.task_id:
                return ResolvedTaskTarget(
                    task_id=sub.task_id,
                    goal=sub.user_message,
                    goal_category=getattr(
                        sub.task_intent, "goal_category", "",
                    ) if sub.task_intent else "",
                    confidence=1.0,
                    match_reason="group_ordinal",
                    match_level=1,
                )
        return None

    # ── build result ─────────────────────────────────────────────

    @staticmethod
    def _build_result(
        hint: ReferenceHint, candidates: list[Task],
    ) -> ReferenceResolution:
        if not candidates:
            return ReferenceResolution(hint=hint)

        if len(candidates) == 1:
            t = candidates[0]
            target = ResolvedTaskTarget(
                task_id=t.task_id,
                goal=t.goal,
                goal_category=t.goal_category,
                confidence=0.70,
                match_reason="reference_resolved",
                match_level=2,
            )
            return ReferenceResolution(
                hint=hint, best_match=target, targets=[target],
                resolution_path="conversation",
            )

        # Multiple candidates → ambiguity
        targets = [
            ResolvedTaskTarget(
                task_id=t.task_id, goal=t.goal,
                goal_category=t.goal_category,
                confidence=0.40, match_reason="ambiguous_candidate",
                match_level=5,
            )
            for t in candidates[:5]
        ]
        return ReferenceResolution(
            hint=hint,
            targets=targets,
            best_match=targets[0] if targets else None,
            is_ambiguous=True,
            needs_clarification=True,
            resolution_path="conversation",
        )


# ── helpers ──────────────────────────────────────────────────────────

_ORDINAL_MAP: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _parse_ordinal(val: str) -> int:
    if val.isdigit():
        return int(val)
    return _ORDINAL_MAP.get(val, 1)
