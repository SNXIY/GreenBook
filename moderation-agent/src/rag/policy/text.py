import re

from moderation.models import ModerationPolicy

_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]")
_WHITESPACE = re.compile(r"\s+")


def normalize_policy_query(query: str) -> str:
    return _WHITESPACE.sub(" ", query.strip().lower())


def policy_search_text(policy: ModerationPolicy) -> str:
    values = [
        policy.code,
        policy.title,
        policy.description,
        policy.risk_type.value,
        (policy.severity.value if policy.severity is not None else ""),
        *(policy.applicability_conditions or []),
        *(policy.exclusion_conditions or []),
        *(policy.violation_examples or []),
        *(policy.safe_examples or []),
        *(policy.tags or []),
    ]
    return " ".join(str(value) for value in values if value)


def keyword_relevance(query: str, document: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    document_tokens = _tokens(document)
    overlap = query_tokens.intersection(document_tokens)
    if not overlap:
        return 0.0

    coverage = len(overlap) / len(query_tokens)
    specificity = len(overlap) / min(max(len(document_tokens), 1), 20)
    normalized_query = normalize_policy_query(query)
    phrase_bonus = (
        0.15 if normalized_query and normalized_query in normalize_policy_query(document) else 0
    )
    return min(1.0, coverage * 0.85 + specificity * 0.15 + phrase_bonus)


def _tokens(value: str) -> set[str]:
    lowered = value.lower()
    tokens = set(_WORD.findall(lowered))
    cjk = _CJK.findall(lowered)
    tokens.update(cjk)
    tokens.update("".join(pair) for pair in zip(cjk, cjk[1:], strict=False))
    return tokens
