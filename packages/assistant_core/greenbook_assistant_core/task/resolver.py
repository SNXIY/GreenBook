"""TaskResolver — resolve a TaskIntent's target reference to a concrete task_id.

Phase 2.5: standalone resolver, no dependency on agent.py or MCP.
"""

from __future__ import annotations

import re
from typing import Any

from .models import ResolvedTaskTarget, Task, TaskIntent, TaskStatus

# ── temporal reference patterns ──────────────────────────────────────

_TEMPORAL = re.compile(
    r"(?:刚才|刚刚|上次|之前|最近|刚刚那个|上一次|上回|刚刚那个)"
)
_ORDINAL = re.compile(r"(?:第\s*(\d+)\s*(?:个|项|条|篇|次))|(?:上\s*一\s*(?:个|项|条|篇|次))")

# ── public API ───────────────────────────────────────────────────────

class TaskResolver:
    """Resolve a TaskIntent's target reference to a concrete Task.

    Matching priority (descending confidence):
      L1 — explicit task_id in the intent  (1.0)
      L2 — label substring match on goal   (0.90)
      L3 — artifact summary/resource match (0.70)
      L4 — same goal_category, most recent (0.50)
      L5 — most recent task overall        (0.30)
    """

    # ── main entry ───────────────────────────────────────────────

    def resolve(
        self,
        intent: TaskIntent,
        tasks: list[Task],
    ) -> ResolvedTaskTarget | None:
        """Return the best-matching Task for *intent*, or None.

        *tasks* should be ordered by recency (newest first) — the caller is
        responsible for providing a list sorted by ``updated_at DESC``.
        """
        if not tasks:
            return None

        # If the caller already resolved a concrete id (e.g. L2 LLM did it),
        # short-circuit at the highest confidence level.
        if intent.target_task_id:
            for t in tasks:
                if t.task_id == intent.target_task_id:
                    return ResolvedTaskTarget(
                        task_id=t.task_id,
                        goal=t.goal,
                        goal_category=t.goal_category,
                        confidence=1.0,
                        match_reason="exact_id",
                        match_level=1,
                    )

        hint = (intent.target_task_hint or "").strip()

        # ── L2: label match ──────────────────────────────────────
        if hint and not self._is_temporal_only(hint):
            result = self._match_by_label(hint, tasks)
            if result is not None:
                return result

        # ── L3: artifact match ───────────────────────────────────
        if hint and not self._is_temporal_only(hint):
            result = self._match_by_artifact(hint, tasks)
            if result is not None:
                return result

        # ── L4: category match ───────────────────────────────────
        if intent.goal_category:
            result = self._match_by_category(intent.goal_category, tasks)
            if result is not None:
                return result

        # ── L5: recency fallback ─────────────────────────────────
        result = self._match_by_recency(tasks)

        # Ambiguity check: when hint is purely temporal and multiple
        # tasks share the same goal_category, flag as ambiguous.
        if hint and self._is_temporal_only(hint) and len(tasks) >= 2:
            same_category = [
                t for t in tasks
                if t.goal_category == result.goal_category
            ]
            if len(same_category) >= 2:
                result.is_ambiguous = True
                result.confidence = min(result.confidence, 0.40)
                result.candidates = [t.task_id for t in same_category
                                     if t.task_id != result.task_id]

        return result

    # ── matching methods ─────────────────────────────────────────

    @staticmethod
    def _match_by_label(
        hint: str,
        tasks: list[Task],
    ) -> ResolvedTaskTarget | None:
        """Match *hint* against task.goal / goal_summary."""
        hint_lower = hint.lower().strip()
        exact_matches: list[Task] = []
        char_matches: list[Task] = []

        for t in tasks:
            goal_lower = t.goal.lower()
            summary_lower = (t.goal_summary or "").lower()

            # Level A: contiguous substring
            if hint_lower in goal_lower or hint_lower in summary_lower:
                exact_matches.append(t)
                continue

            # Level B: CJK char + ASCII token match
            if _hint_matches_text(hint_lower, goal_lower):
                char_matches.append(t)
                continue
            if summary_lower and _hint_matches_text(hint_lower, summary_lower):
                char_matches.append(t)

        candidates = exact_matches or char_matches
        if not candidates:
            return None

        if len(candidates) == 1:
            t = candidates[0]
            conf = 0.90 if candidates is exact_matches else 0.75
            reason = "label_match" if candidates is exact_matches else "label_char_match"
            return ResolvedTaskTarget(
                task_id=t.task_id,
                goal=t.goal,
                goal_category=t.goal_category,
                confidence=conf,
                match_reason=reason,
                match_level=2,
            )

        best = candidates[0]
        return ResolvedTaskTarget(
            task_id=best.task_id,
            goal=best.goal,
            goal_category=best.goal_category,
            confidence=0.60,
            match_reason="label_match_ambiguous",
            match_level=2,
            candidates=[t.task_id for t in candidates[1:]],
        )

    @staticmethod
    def _match_by_artifact(
        hint: str,
        tasks: list[Task],
    ) -> ResolvedTaskTarget | None:
        """Match *hint* against artifact summaries / resource kinds."""
        hint_lower = hint.lower()
        for t in tasks:
            for a in t.artifacts:
                summary = (a.summary or "").lower()
                kind = (a.resource_kind or "").lower()
                if _hint_matches_text(hint_lower, summary) or _hint_matches_text(hint_lower, kind):
                    return ResolvedTaskTarget(
                        task_id=t.task_id,
                        goal=t.goal,
                        goal_category=t.goal_category,
                        confidence=0.70,
                        match_reason="artifact_match",
                        match_level=3,
                    )
        return None

    @staticmethod
    def _match_by_category(
        category: str,
        tasks: list[Task],
    ) -> ResolvedTaskTarget | None:
        """Find the most recent task with the same goal_category."""
        for t in tasks:
            if t.goal_category == category:
                return ResolvedTaskTarget(
                    task_id=t.task_id,
                    goal=t.goal,
                    goal_category=t.goal_category,
                    confidence=0.50,
                    match_reason="category_match",
                    match_level=4,
                )
        return None

    @staticmethod
    def _match_by_recency(tasks: list[Task]) -> ResolvedTaskTarget:
        """Fallback: most recently updated task."""
        t = tasks[0]
        return ResolvedTaskTarget(
            task_id=t.task_id,
            goal=t.goal,
            goal_category=t.goal_category,
            confidence=0.30,
            match_reason="recent_fallback",
            match_level=5,
        )

    @staticmethod
    def _is_temporal_only(hint: str) -> bool:
        """True when the hint is purely temporal/deictic with no content."""
        stripped = _TEMPORAL.sub("", hint).strip()
        stripped = _ORDINAL.sub("", stripped).strip()
        # Remove demonstratives: 那个, 这个, 那篇, 这篇, …
        stripped = re.sub(r"[那这][个篇条次项种些]", "", stripped).strip()
        return len(stripped) < 2


_ASCII_TOKEN = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


def _hint_matches_text(hint: str, text: str) -> bool:
    """Check whether *hint* meaningfully refers to *text*.

    Strategy (tolerant of CJK substring variations):
    1. Contiguous substring → direct match.
    2. All hint CJK chars appear in text AND all ASCII tokens appear → match.
    3. Partial: at least one ASCII token or 50%+ of CJK chars match.
    """
    hint = hint.lower().strip()
    text = text.lower().strip()
    if not hint:
        return False

    # 1. Contiguous substring
    if hint in text:
        return True

    # 2. Decompose into ASCII tokens + CJK chars
    hint_ascii = set(t.lower() for t in _ASCII_TOKEN.findall(hint))
    hint_cjk = [c for c in hint if '一' <= c <= '鿿' or '㐀' <= c <= '䶿']

    text_ascii = set(t.lower() for t in _ASCII_TOKEN.findall(text))
    text_cjk = [c for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿']
    text_cjk_set = set(text_cjk)

    # 2a. All ASCII tokens + all CJK chars found
    ascii_ok = hint_ascii.issubset(text_ascii) if hint_ascii else True
    cjk_ok = all(c in text_cjk_set for c in hint_cjk) if hint_cjk else True
    if ascii_ok and cjk_ok:
        return True

    # 2b. Partial: at least one ASCII token AND most CJK chars match
    if hint_ascii and hint_cjk:
        ascii_hit = bool(hint_ascii & text_ascii)
        cjk_hit_count = sum(1 for c in hint_cjk if c in text_cjk_set)
        cjk_ratio = cjk_hit_count / len(hint_cjk) if hint_cjk else 0
        if ascii_hit and cjk_ratio >= 0.5:
            return True

    # 2c. ASCII-only hint: at least one token matches
    if hint_ascii and not hint_cjk:
        return bool(hint_ascii & text_ascii)

    # 2d. CJK-only hint: at least half the chars match
    if hint_cjk and not hint_ascii:
        cjk_hit_count = sum(1 for c in hint_cjk if c in text_cjk_set)
        return cjk_hit_count >= max(1, len(hint_cjk) * 0.5)

    return False


# ── integration helper ───────────────────────────────────────────────

def resolve_target(
    intent: TaskIntent,
    tasks: list[Task],
) -> TaskIntent:
    """Mutate *intent* in-place: fill target_task_id from hint resolution.

    This is the integration seam — call it after TaskUnderstanding and
    before persisting the intent.
    """
    if intent.target_task_id:
        return intent  # already resolved
    if intent.relation in ("NEW_TASK", "DIRECT"):
        return intent  # no target needed

    # Sort by recency (newest first) for matching
    sorted_tasks = sorted(
        tasks,
        key=lambda t: t.updated_at,
        reverse=True,
    )

    resolver = TaskResolver()
    resolved = resolver.resolve(intent, sorted_tasks)

    if resolved is not None:
        intent.target_task_id = resolved.task_id
        # Bump confidence based on match quality
        if resolved.match_level <= 2:
            intent.confidence = max(intent.confidence, resolved.confidence)

    return intent
