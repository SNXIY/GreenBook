from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from app.creator.memory.models import CreatorEngagementMetrics
from app.creator.retrieval.models import CreatorCorpusDocument


_TOKEN_PATTERN = re.compile(
    r"[a-zA-Z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]",
    re.IGNORECASE,
)


def query_sha256(query: str) -> str:
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


def evidence_id(tenant_id: str, document_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{document_id}".encode("utf-8")).hexdigest()
    return f"evidence-{digest[:24]}"


def searchable_text(document: CreatorCorpusDocument) -> str:
    return "\n".join(
        value
        for value in (
            document.title,
            document.description,
            document.body,
            " ".join(document.tags),
        )
        if value
    )


def tokenize(text: str) -> tuple[str, ...]:
    normalized = text.lower()
    base = _TOKEN_PATTERN.findall(normalized)
    cjk = "".join(
        character for character in normalized if "\u3400" <= character <= "\u9fff"
    )
    bigrams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return tuple(item for item in (*base, *bigrams) if item.strip())


def bm25_scores(
    query: str,
    documents: Mapping[str, str],
) -> dict[str, float]:
    query_counts = _counts(tokenize(query))
    if not query_counts or not documents:
        return {}

    rows: list[tuple[str, dict[str, int], int]] = []
    document_frequency: dict[str, int] = {}
    for document_id, content in documents.items():
        token_counts = _counts(tokenize(content))
        rows.append((document_id, token_counts, sum(token_counts.values())))
        for term in token_counts:
            document_frequency[term] = document_frequency.get(term, 0) + 1

    total_documents = len(rows)
    average_length = (
        sum(length for _, _, length in rows) / total_documents
        if total_documents
        else 1.0
    ) or 1.0
    k1 = 1.5
    b = 0.75
    scores: dict[str, float] = {}
    for document_id, token_counts, document_length in rows:
        score = 0.0
        length_norm = k1 * (1.0 - b + b * document_length / average_length)
        for term, query_frequency in query_counts.items():
            term_frequency = token_counts.get(term, 0)
            if term_frequency == 0:
                continue
            frequency = document_frequency.get(term, 0)
            inverse_frequency = math.log(
                1.0 + (total_documents - frequency + 0.5) / (frequency + 0.5)
            )
            score += (
                inverse_frequency
                * (1.0 + math.log(query_frequency))
                * (term_frequency * (k1 + 1.0))
                / (term_frequency + length_norm)
            )
        if score > 0:
            scores[document_id] = score
    return scores


def normalize_scores(scores: Mapping[str, float]) -> dict[str, float]:
    positive = [score for score in scores.values() if score > 0]
    if not positive:
        return {key: 0.0 for key in scores}
    lowest = min(positive)
    highest = max(positive)
    if math.isclose(lowest, highest):
        return {key: 1.0 if score > 0 else 0.0 for key, score in scores.items()}
    return {
        key: (score - lowest) / (highest - lowest) if score > 0 else 0.0
        for key, score in scores.items()
    }


def lexical_relevance(query: str, content: str) -> float:
    query_tokens = _counts(tokenize(query))
    content_tokens = _counts(tokenize(content))
    if not query_tokens or not content_tokens:
        return 0.0
    dot = sum(value * content_tokens.get(key, 0) for key, value in query_tokens.items())
    query_norm = math.sqrt(sum(value * value for value in query_tokens.values()))
    content_norm = math.sqrt(sum(value * value for value in content_tokens.values()))
    cosine = dot / (query_norm * content_norm) if query_norm and content_norm else 0.0
    coverage = len(set(query_tokens) & set(content_tokens)) / len(query_tokens)
    compact_query = _compact(query)
    phrase = 1.0 if compact_query and compact_query in _compact(content) else 0.0
    return _bounded(cosine * 0.55 + coverage * 0.35 + phrase * 0.10)


def raw_business_score(metrics: CreatorEngagementMetrics) -> float:
    if metrics.heat_score > 0:
        return metrics.heat_score
    weighted = (
        metrics.views * 0.02
        + metrics.likes * 1.0
        + metrics.favorites * 1.4
        + metrics.comments * 1.6
        + metrics.shares * 2.0
    )
    return math.log1p(weighted)


def freshness_score(
    published_at: datetime | None,
    *,
    half_life_days: float = 180.0,
) -> float:
    if published_at is None:
        return 0.2
    moment = published_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age_seconds = max(
        0.0,
        (datetime.now(timezone.utc) - moment).total_seconds(),
    )
    age_days = age_seconds / 86_400
    return _bounded(math.pow(0.5, age_days / half_life_days))


def best_excerpt(
    query: str,
    document: CreatorCorpusDocument,
    *,
    max_chars: int,
) -> str:
    candidates = [
        segment.strip()
        for segment in re.split(r"(?:\r?\n){2,}|(?<=[。！？.!?])\s+", document.body)
        if segment.strip()
    ]
    if document.description.strip():
        candidates.insert(0, document.description.strip())
    if not candidates:
        candidates = [document.title]
    best = max(
        candidates,
        key=lambda segment: (
            lexical_relevance(query, f"{document.title}\n{segment}"),
            -len(segment),
        ),
    )
    return _clip(best, max_chars)


def reciprocal_rank_score(
    ranks: Sequence[int],
    *,
    constant: int = 60,
) -> float:
    if not ranks:
        return 0.0
    return sum(1.0 / (constant + rank) for rank in ranks)


def _counts(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _clip(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def bounded_score(value: float) -> float:
    return _bounded(value)


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
