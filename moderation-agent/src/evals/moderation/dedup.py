import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from evals.moderation.io import DatasetValidationError
from evals.moderation.schemas import ModerationEvalCase

_WHITESPACE = re.compile(r"\s+")


class DuplicateKind(StrEnum):
    EXACT = "EXACT"
    NEAR = "NEAR"


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    kind: DuplicateKind
    left_case_id: str
    right_case_id: str
    similarity: float
    same_scenario_group: bool
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    exact: tuple[DuplicateMatch, ...]
    near: tuple[DuplicateMatch, ...]
    intentional_variants: tuple[DuplicateMatch, ...]

    @property
    def blocking(self) -> tuple[DuplicateMatch, ...]:
        return (*self.exact, *self.near)


class DuplicateValidationError(DatasetValidationError):
    """Raised when exact or cross-scenario near duplicates are found."""


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def canonical_input(case: ModerationEvalCase) -> str:
    context = case.input.context
    parts = [
        f"platform:{normalize_text(case.input.platform)}",
        f"content-type:{case.input.content_type.value.casefold()}",
        f"content:{normalize_text(case.input.content)}",
        f"parent:{normalize_text(context.parent_comment or '')}",
        f"context-complete:{str(context.complete).casefold()}",
    ]
    parts.extend(
        f"conversation:{index}:{normalize_text(value)}"
        for index, value in enumerate(context.conversation_context)
    )
    parts.extend(
        f"recent:{index}:{normalize_text(value)}"
        for index, value in enumerate(context.author_recent_contents)
    )
    parts.extend(
        f"report:{index}:{normalize_text(value)}"
        for index, value in enumerate(context.report_reasons)
    )
    parts.extend(
        f"metadata:{normalize_text(key)}:{normalize_text(value)}"
        for key, value in sorted(case.input.metadata.items())
    )
    return "\n".join(parts)


def exact_fingerprint(case: ModerationEvalCase) -> str:
    return hashlib.sha256(canonical_input(case).encode("utf-8")).hexdigest()


def inspect_duplicates(
    cases: Sequence[ModerationEvalCase],
    *,
    near_threshold: float = 0.88,
    ngram_size: int = 3,
) -> DuplicateReport:
    if not 0.0 < near_threshold <= 1.0:
        raise ValueError("near_threshold must be in (0, 1]")
    if ngram_size < 1:
        raise ValueError("ngram_size must be at least 1")

    canonical = [canonical_input(case) for case in cases]
    fingerprints = [
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in canonical
    ]
    grams = [_character_ngrams(value, ngram_size) for value in canonical]
    exact: list[DuplicateMatch] = []
    near: list[DuplicateMatch] = []
    intentional: list[DuplicateMatch] = []

    for left_index, left in enumerate(cases):
        for right_index in range(left_index + 1, len(cases)):
            right = cases[right_index]
            same_group = left.scenario_group_id == right.scenario_group_id
            if fingerprints[left_index] == fingerprints[right_index]:
                exact.append(
                    DuplicateMatch(
                        kind=DuplicateKind.EXACT,
                        left_case_id=left.case_id,
                        right_case_id=right.case_id,
                        similarity=1.0,
                        same_scenario_group=same_group,
                        fingerprint=fingerprints[left_index],
                    )
                )
                continue

            similarity = _jaccard(grams[left_index], grams[right_index])
            if similarity < near_threshold:
                continue
            match = DuplicateMatch(
                kind=DuplicateKind.NEAR,
                left_case_id=left.case_id,
                right_case_id=right.case_id,
                similarity=similarity,
                same_scenario_group=same_group,
            )
            if same_group:
                intentional.append(match)
            else:
                near.append(match)

    return DuplicateReport(
        exact=tuple(exact),
        near=tuple(near),
        intentional_variants=tuple(intentional),
    )


def validate_duplicates(
    cases: Sequence[ModerationEvalCase],
    *,
    near_threshold: float = 0.88,
    ngram_size: int = 3,
) -> DuplicateReport:
    report = inspect_duplicates(
        cases,
        near_threshold=near_threshold,
        ngram_size=ngram_size,
    )
    if report.blocking:
        details = "\n".join(
            f"{match.kind}: {match.left_case_id} <-> {match.right_case_id} "
            f"(similarity={match.similarity:.3f})"
            for match in report.blocking
        )
        raise DuplicateValidationError(details)
    return report


def _character_ngrams(value: str, size: int) -> frozenset[str]:
    compact = _WHITESPACE.sub(" ", value)
    if len(compact) <= size:
        return frozenset({compact})
    return frozenset(compact[index : index + size] for index in range(len(compact) - size + 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union)
