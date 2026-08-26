"""Small, storage-neutral relevance gate for memory candidates."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import MemoryRecord


_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class ScoredMemory:
    """One candidate and its normalized relevance score."""

    memory: MemoryRecord
    relevance_score: float


@dataclass(frozen=True)
class MemoryRelevanceResult:
    """The gate decision, including an explicit no-memory outcome."""

    selected: tuple[MemoryRecord, ...]
    scored: tuple[ScoredMemory, ...]
    relevance_threshold: float
    confidence_threshold: float

    @property
    def no_memory(self) -> bool:
        return not self.selected

    @property
    def relevance_scores(self) -> dict[str, float]:
        return {
            item.memory.memory_id: item.relevance_score
            for item in self.scored
        }


class MemoryRelevanceGate:
    """Select only candidates that clear relevance and confidence gates."""

    def __init__(
        self,
        *,
        relevance_threshold: float,
        confidence_threshold: float = 0.0,
    ) -> None:
        self.relevance_threshold = _bounded_threshold(relevance_threshold)
        self.confidence_threshold = _bounded_threshold(confidence_threshold)

    def evaluate(
        self,
        candidates: Iterable[MemoryRecord],
        *,
        score: Callable[[MemoryRecord], float],
        limit: int,
    ) -> MemoryRelevanceResult:
        scored = sorted(
            (
                ScoredMemory(
                    memory=item,
                    relevance_score=_bounded_score(score(item)),
                )
                for item in candidates
            ),
            key=lambda item: item.relevance_score,
            reverse=True,
        )
        selected = tuple(
            item.memory
            for item in scored
            if (
                item.relevance_score >= self.relevance_threshold
                and item.memory.confidence >= self.confidence_threshold
            )
        )[: max(0, int(limit))]
        return MemoryRelevanceResult(
            selected=selected,
            scored=tuple(scored),
            relevance_threshold=self.relevance_threshold,
            confidence_threshold=self.confidence_threshold,
        )


def lexical_relevance(candidate_text: str, query_terms: Iterable[str]) -> float:
    """Return candidate/query lexical coverage normalized to ``[0, 1]``.

    Both candidate coverage and query coverage are considered. This preserves
    short, focused preference matches while allowing longer episodic memory
    text to pass when it contains a clear query term. Matching is intentionally
    lexical and deterministic; no storage field or embedding is introduced.
    """

    candidate = _terms(candidate_text)
    query = _terms(" ".join(str(item) for item in query_terms))
    if not candidate or not query:
        return 0.0
    overlap = sum(
        1
        for candidate_term in candidate
        if any(_terms_match(candidate_term, query_term) for query_term in query)
    )
    if not overlap:
        return 0.0
    return max(overlap / len(candidate), overlap / len(query))


def _terms(value: str) -> list[str]:
    return list(dict.fromkeys(
        term.casefold()
        for term in _WORD_RE.findall(value.casefold())
        if len(term) > 1
    ))


def _terms_match(left: str, right: str) -> bool:
    left = left.rstrip("s")
    right = right.rstrip("s")
    return left == right or left in right or right in left


def _bounded_threshold(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("memory relevance thresholds must be within [0, 1]")
    return value


def _bounded_score(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


__all__ = [
    "MemoryRelevanceGate",
    "MemoryRelevanceResult",
    "ScoredMemory",
    "lexical_relevance",
]
