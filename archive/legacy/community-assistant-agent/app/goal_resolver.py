"""Pure, goal-level resolution for multi-goal conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.domain import ConversationGoal, GoalMatch, GoalResolution, TurnIntent


@dataclass(frozen=True)
class _ScoredGoal:
    goal: ConversationGoal
    score: float
    method: str


class GoalResolver:
    """Resolve one TurnIntent without mutating any Goal or target state."""

    CONFIDENCE_THRESHOLD = 0.58
    CLOSE_SCORE_GAP = 0.10

    @staticmethod
    def resolve_selection(
        *,
        message: str,
        candidates: list[GoalMatch],
    ) -> str | None:
        """Match a user reply against goal clarification candidates.

        Returns the selected goal_id, or None when the reply does not
        match any candidate (user is abandoning the clarification).
        """
        text = message.strip()
        # Ordinal: "A", "a", "1", "第1个"
        ordinal = GoalResolver._selection_ordinal(text)
        if ordinal is not None and 0 <= ordinal < len(candidates):
            return candidates[ordinal].goal_id
        # Label substring: user types all or part of the goal label
        normalized = re.sub(r"[^0-9a-z一-鿿]+", "", text.lower())
        best: tuple[int, str | None] = (0, None)
        for candidate in candidates:
            label_norm = re.sub(r"[^0-9a-z一-鿿]+", "", (candidate.label or "").lower())
            if label_norm and label_norm in normalized:
                score = len(label_norm)
                if score > best[0]:
                    best = (score, candidate.goal_id)
        return best[1]

    @staticmethod
    def _selection_ordinal(text: str) -> int | None:
        """Parse "A", "B", "1", "第1个" from a user reply."""
        lowered = text.strip().lower()
        # Single letter: a, b, c, ...
        for i, ch in enumerate("abcdefgh"):
            if lowered == ch or lowered.startswith(f"{ch} ") or lowered.startswith(f"{ch}."):
                return i
        # Number: 1, 2, 第1个
        match = re.match(r"(?:第\s*)?(\d+)\s*(?:个|项|[.、)）:\s])", text)
        if match:
            return int(match.group(1)) - 1
        match = re.match(r"^(\d+)$", lowered)
        if match:
            return int(match.group(1)) - 1
        return None

    def resolve(
        self,
        *,
        turn_intent: TurnIntent,
        goals: Iterable[ConversationGoal],
        raw_message: str = "",
        focus_goal_refs: list[str] | None = None,
    ) -> GoalResolution:
        candidates = list(goals)
        if turn_intent.operation == "CREATE_POST":
            return GoalResolution(outcome="NEW_GOAL", confidence=turn_intent.confidence)
        if turn_intent.operation == "OPEN_PLAN" and not candidates:
            return GoalResolution(outcome="NEW_GOAL", confidence=turn_intent.confidence)
        if not candidates:
            return GoalResolution(outcome="NOT_FOUND")

        focus_ids = self._focus_goal_ids(focus_goal_refs)
        # Ordinal on the focus stack: "第一个任务" / "刚才那个" / "上一个".
        focus_pick = self._focus_ordinal_match(
            raw_message or turn_intent.raw_message,
            candidates,
            focus_ids,
        )
        if focus_pick is not None:
            match = self._match(focus_pick, 0.9, "RECENT_ACTIVE")
            return GoalResolution(
                outcome="RESOLVED",
                goal_id=focus_pick.goal_id,
                candidates=[match],
                confidence=0.9,
            )

        explicit = self._explicit_matches(turn_intent.explicit_refs, candidates)
        if len(explicit) == 1:
            match = self._match(explicit[0], 1.0, "EXPLICIT_ID")
            return GoalResolution(
                outcome="RESOLVED",
                goal_id=explicit[0].goal_id,
                candidates=[match],
                confidence=1.0,
            )
        if len(explicit) > 1:
            matches = [self._match(goal, 1.0, "EXPLICIT_ID") for goal in explicit]
            return GoalResolution(
                outcome="NEEDS_CLARIFICATION",
                candidates=matches[:8],
                confidence=1.0,
            )

        # Use the raw user message for substring / LCS matching against
        # goal metadata.  Prefer the explicit parameter (worker passes
        # the original prompt) over the TurnIntent field (which may not
        # be set by older parser versions).
        raw = self._normalize(
            raw_message or turn_intent.raw_message or turn_intent.semantic_subject
        )
        subject = self._normalize(turn_intent.semantic_subject)
        if len(candidates) == 1 and not subject:
            match = self._match(candidates[0], 0.72, "SOLE_GOAL")
            return GoalResolution(
                outcome="RESOLVED",
                goal_id=candidates[0].goal_id,
                candidates=[match],
                confidence=0.72,
            )

        recency = {
            goal.goal_id: max(0.0, 0.05 - index * 0.01)
            for index, goal in enumerate(
                sorted(
                    candidates,
                    key=lambda item: item.updated_at.isoformat() if item.updated_at else "",
                    reverse=True,
                )
            )
        }
        # Focus stack bonus: prefer goals the workspace still treats as in-focus
        # over a merely newer unrelated ACTIVE goal.
        for index, goal_id in enumerate(focus_ids):
            if goal_id in recency:
                recency[goal_id] = max(recency[goal_id], 0.08 - index * 0.015)
        # Words that appear in many goals are not discriminative.  The
        # structural template "十分钟之后发布一条...的帖子" is shared by
        # every goal created from the same prompt pattern and must not
        # drown out the content words ("通信", "协同", "skill") that
        # actually distinguish the goals.
        common_words = self._common_words(candidates)
        scored = [
            self._score(goal, raw, subject, recency[goal.goal_id], common_words)
            for goal in candidates
        ]
        ranked = sorted(scored, key=lambda item: item.score, reverse=True)
        matches = [
            self._match(item.goal, item.score, item.method)
            for item in ranked[:8]
        ]
        top = ranked[0]
        if not subject or top.score < self.CONFIDENCE_THRESHOLD:
            outcome = "NEEDS_CLARIFICATION" if len(candidates) > 1 else "NOT_FOUND"
            return GoalResolution(
                outcome=outcome,
                candidates=matches,
                confidence=top.score,
            )
        # Exact substring containment ("the user named the post") is
        # definitive.  LCS against a shared template phrase
        # ("一份给初学者的实用指南") can inflate the wrong goal above the
        # correct exact match.  When the runner-up has an _EXACT match
        # that the top lacks, prefer the runner-up.
        if len(ranked) > 1:
            top_exact = top.method.endswith("_EXACT")
            second_exact = ranked[1].method.endswith("_EXACT")
            if top_exact and not second_exact:
                return GoalResolution(
                    outcome="RESOLVED",
                    goal_id=top.goal.goal_id,
                    candidates=matches,
                    confidence=top.score,
                )
            if second_exact and not top_exact and ranked[1].score >= 0.85:
                return GoalResolution(
                    outcome="RESOLVED",
                    goal_id=ranked[1].goal.goal_id,
                    candidates=matches,
                    confidence=ranked[1].score,
                )
        if len(ranked) > 1 and top.score - ranked[1].score < self.CLOSE_SCORE_GAP:
            return GoalResolution(
                outcome="NEEDS_CLARIFICATION",
                candidates=matches,
                confidence=top.score,
            )
        return GoalResolution(
            outcome="RESOLVED",
            goal_id=top.goal.goal_id,
            candidates=matches,
            confidence=top.score,
        )

    @classmethod
    def _score(
        cls,
        goal: ConversationGoal,
        raw: str,
        subject: str,
        recency_bonus: float,
        common_words: set[str],
    ) -> _ScoredGoal:
        title_values = [
            cls._normalize(value)
            for value in [*goal.artifact_titles, *goal.artifact_topics]
            if value
        ]
        summary_values = [
            cls._normalize(value)
            for value in [goal.summary or "", *goal.aliases]
            if value
        ]
        # Exact substring containment is the strongest signal, but only
        # for values with meaningful semantic weight.  A single tag like
        # "agent" or "学习" must never produce a high-confidence match
        # because it is shared by too many goals in the same conversation.
        meaningful_titles = [v for v in title_values if cls._meaningful(v)]
        # Substring / LCS checks use the raw user message because the
        # semantic_subject strips structural words that are essential for
        # matching against goal summaries ("如何学go的帖子" must find
        # "十分钟之后发布一条如何学go的帖子").
        if raw and any(
            raw in value or value in raw for value in meaningful_titles
        ):
            return _ScoredGoal(goal, min(1.0, 0.92 + recency_bonus), "ARTIFACT_TITLE_EXACT")
        if raw and any(raw in value or value in raw for value in summary_values):
            return _ScoredGoal(goal, min(1.0, 0.86 + recency_bonus), "GOAL_SUMMARY_EXACT")

        # Longest-common-substring against artifact titles.
        lcs_score = cls._lcs_match(raw, meaningful_titles)
        if lcs_score >= 0.25:
            return _ScoredGoal(
                goal,
                min(1.0, 0.68 + lcs_score * 0.55 + recency_bonus),
                "ARTIFACT_TITLE",
            )
        # Also try LCS against summaries (user prompts) with raw message.
        lcs_score = cls._lcs_match(raw, summary_values)
        if lcs_score >= 0.40:
            return _ScoredGoal(
                goal,
                min(1.0, 0.64 + lcs_score * 0.50 + recency_bonus),
                "GOAL_SUMMARY",
            )

        # Fallback: distinctive word overlap against titles.
        if subject and meaningful_titles:
            subject_words = cls._word_tokens(subject)
            for title in meaningful_titles:
                title_words = cls._word_tokens(title)
                distinctive = subject_words & title_words - common_words
                shared = subject_words & title_words & common_words
                if len(distinctive) >= 2 or (
                    len(distinctive) >= 1
                    and len(shared) >= 4
                    and len(distinctive | shared) >= len(title_words) * 0.15
                ):
                    effective = len(distinctive) * 5 + len(shared)
                    boost = min(0.16, effective * 0.012)
                    score = min(1.0, 0.62 + boost + recency_bonus)
                    return _ScoredGoal(goal, score, "ARTIFACT_TITLE")

        semantic_values = [
            *title_values,
            *summary_values,
            cls._normalize(goal.intent),
        ]
        similarity = max(
            (cls._similarity(subject, value) for value in semantic_values if value),
            default=0.0,
        )
        score = min(1.0, 0.28 + 0.58 * similarity + recency_bonus)
        method = "SEMANTIC_SIMILARITY" if similarity else "RECENT_ACTIVE"
        return _ScoredGoal(goal, score, method)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _common_words(candidates: list[ConversationGoal]) -> set[str]:
        """Words that appear in multiple goals and are therefore not discriminative."""
        all_sets: list[set[str]] = []
        for goal in candidates:
            words: set[str] = set()
            for value in [*goal.artifact_titles, goal.summary or ""]:
                words.update(GoalResolver._word_tokens(GoalResolver._normalize(value)))
            all_sets.append(words)
        common: set[str] = set()
        for i, a in enumerate(all_sets):
            for j, b in enumerate(all_sets):
                if i < j:
                    common.update(a & b)
        return common

    @staticmethod
    def _meaningful(value: str) -> bool:
        """Require enough CJK content for reliable matching.

        Mixed strings like '多agent' are brand prefixes (6 chars, 1 CJK)
        that match every goal in a multi-agent conversation.  Require at
        least 8 total chars OR 4+ CJK chars to qualify as a signal.
        """
        cjk_chars = len(re.findall(r"[一-鿿]", value))
        return len(value) >= 8 or cjk_chars >= 4

    @classmethod
    def _lcs_match(cls, subject: str, candidates: list[str]) -> float:
        """Longest common substring between subject and any candidate, as a ratio."""
        if not subject or not candidates:
            return 0.0
        best = 0
        s_len = len(subject)
        for cand in candidates:
            lcs_len = cls._lcs_length(subject, cand)
            ratio = lcs_len / max(1, min(s_len, len(cand)))
            if ratio > best:
                best = ratio
        return best

    @staticmethod
    def _lcs_length(a: str, b: str) -> int:
        """Length of the longest common substring (contiguous)."""
        if not a or not b:
            return 0
        prev = [0] * (len(b) + 1)
        best = 0
        for i in range(1, len(a) + 1):
            curr = [0] * (len(b) + 1)
            for j in range(1, len(b) + 1):
                if a[i - 1] == b[j - 1]:
                    curr[j] = prev[j - 1] + 1
                    if curr[j] > best:
                        best = curr[j]
                else:
                    curr[j] = 0
            prev = curr
        return best

    @staticmethod
    def _explicit_matches(
        refs: list[str],
        goals: list[ConversationGoal],
    ) -> list[ConversationGoal]:
        requested = {value.lower() for value in refs}
        if not requested:
            return []
        return [
            goal
            for goal in goals
            if requested
            & {
                goal.goal_id.lower(),
                f"goal:{goal.goal_id}".lower(),
                *(value.lower() for value in goal.explicit_refs),
            }
        ]

    @staticmethod
    def _match(goal: ConversationGoal, score: float, method: str) -> GoalMatch:
        return GoalMatch(
            goal_id=goal.goal_id,
            label=GoalResolver.label_for_goal(goal),
            score=max(0.0, min(1.0, score)),
            resolution_method=method,  # type: ignore[arg-type]
        )

    @staticmethod
    def label_for_goal(goal: ConversationGoal) -> str:
        """Build a disambiguation label that stays unique across same-title goals."""

        title = next(iter(goal.artifact_titles), None) or goal.summary or goal.intent
        title = str(title or "未命名任务").strip() or "未命名任务"
        phase = str(goal.phase or "").upper()
        status_zh = {
            "PUBLISHED": "已发布",
            "SCHEDULED": "已排定",
            "READY": "草稿就绪",
            "DRAFTING": "创作中",
            "FAILED": "失败",
        }.get(phase, phase or goal.status or "未知")
        content = goal.target_context.content_target if goal.target_context else None
        schedule = goal.target_context.schedule_target if goal.target_context else None
        publication = (
            goal.target_context.publication_target if goal.target_context else None
        )
        target_id = None
        if publication is not None and publication.target_id:
            target_id = publication.target_id
        elif content is not None and content.target_id:
            target_id = content.target_id
        elif schedule is not None and schedule.target_id:
            target_id = schedule.target_id
        parts = [title, status_zh]
        if target_id:
            parts.append(f"号 {target_id}")
        return " · ".join(parts)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^0-9a-z一-鿿]+", "", value.lower())

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left in right or right in left:
            return min(len(left), len(right)) / max(len(left), len(right))
        left_set = cls._word_tokens(left) | cls._ngrams(left)
        right_set = cls._word_tokens(right) | cls._ngrams(right)
        union = left_set | right_set
        overlap = left_set & right_set
        word_overlap = cls._word_tokens(left) & cls._word_tokens(right)
        word_boost = len(word_overlap) * 0.05
        base = len(overlap) / len(union) if union else 0.0
        return min(1.0, base + word_boost)

    @staticmethod
    def _word_tokens(value: str) -> set[str]:
        tokens: set[str] = set()
        tokens.update(re.findall(r"[a-z0-9]{2,}", value))
        cjk_runs = re.findall(r"[一-鿿]{2,}", value)
        for run in cjk_runs:
            for length in (2, 3, 4):
                for index in range(len(run) - length + 1):
                    tokens.add(run[index:index + length])
        return tokens

    @staticmethod
    def _ngrams(value: str) -> set[str]:
        if len(value) < 2:
            return {value}
        return {value[index : index + 2] for index in range(len(value) - 1)}

    @staticmethod
    def _focus_goal_ids(focus_goal_refs: list[str] | None) -> list[str]:
        ids: list[str] = []
        for ref in focus_goal_refs or []:
            text = str(ref or "").strip()
            if not text:
                continue
            if text.lower().startswith("goal:"):
                text = text.split(":", 1)[1]
            if text and text not in ids:
                ids.append(text)
        return ids

    @classmethod
    def _focus_ordinal_match(
        cls,
        message: str,
        candidates: list[ConversationGoal],
        focus_ids: list[str],
    ) -> ConversationGoal | None:
        """Resolve '第一个/刚才那个/上一个任务' against the focus stack."""

        text = (message or "").strip()
        if not text or not focus_ids:
            return None
        by_id = {goal.goal_id: goal for goal in candidates}
        focused = [by_id[goal_id] for goal_id in focus_ids if goal_id in by_id]
        if not focused:
            return None
        lowered = text.lower()
        # Pure switch / resume cues without a new content subject.
        switch_cues = (
            "切换到",
            "回到",
            "改回",
            "先处理",
            "继续刚才",
            "刚才那个",
            "上一个任务",
            "第一个任务",
            "第二个任务",
            "第1个",
            "第2个",
        )
        if not any(cue in lowered for cue in switch_cues):
            return None
        if any(token in lowered for token in ("第一个", "第1个", "刚才那个", "上一个")):
            return focused[0]
        if any(token in lowered for token in ("第二个", "第2个")) and len(focused) > 1:
            return focused[1]
        # "切换到 MySQL 那个" — fall through to title match among focused only.
        normalized = cls._normalize(text)
        for goal in focused:
            titles = [cls._normalize(value) for value in goal.artifact_titles if value]
            if any(title and (title in normalized or normalized in title) for title in titles):
                return goal
            summary = cls._normalize(goal.summary or "")
            if summary and (summary in normalized or normalized in summary):
                return goal
        return None


__all__ = ["GoalResolver"]
