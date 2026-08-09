from __future__ import annotations

import re

from app.creator.evaluation.models import (
    ClaimAssessment,
    ClaimVerdict,
    CreatorEvaluationObservation,
    EvaluationCase,
    GenerationJudgeAssessment,
    JudgeMetricScore,
)


_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_+#.-]*", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_SENTENCE_BOUNDARY = re.compile(r"[。！？.!?；;\n]+")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$")
_STOP_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


class DeterministicGenerationJudge:
    """Auditable local judge used when no external model evaluator is configured."""

    name = "mindflow.creator.generation-rules"
    version = "1.0.0"

    def __init__(self, *, claim_overlap_threshold: float = 0.42) -> None:
        if claim_overlap_threshold <= 0.0 or claim_overlap_threshold > 1.0:
            raise ValueError("claim_overlap_threshold must be between zero and one")
        self._claim_overlap_threshold = claim_overlap_threshold

    async def assess(
        self,
        case: EvaluationCase,
        observation: CreatorEvaluationObservation,
    ) -> GenerationJudgeAssessment:
        generation = observation.generation
        if generation is None or not generation.body_markdown.strip():
            return GenerationJudgeAssessment(
                judge_name=self.name,
                judge_version=self.version,
                limitations=("No generated body was present.",),
            )
        limitations: list[str] = []
        claims = generation.claim_assessments or self._assess_claims(observation)
        faithfulness = self._faithfulness(observation, claims)
        relevance = self._relevance(case, generation.title, generation.body_markdown)
        style, style_limitations = self._style(
            case,
            generation.body_markdown,
        )
        limitations.extend(style_limitations)
        if not generation.claim_assessments:
            limitations.append(
                "Claim support used deterministic lexical overlap; configure a "
                "calibrated model or human judge for semantic entailment."
            )
        return GenerationJudgeAssessment(
            judge_name=self.name,
            judge_version=self.version,
            faithfulness=faithfulness,
            relevance=relevance,
            style_consistency=style,
            claims=claims,
            limitations=tuple(limitations),
        )

    def _assess_claims(
        self,
        observation: CreatorEvaluationObservation,
    ) -> tuple[ClaimAssessment, ...]:
        generation = observation.generation
        assert generation is not None
        evidence = {
            item.evidence_id: item
            for item in observation.evidence
            if item.authority_verified
        }
        cited = tuple(
            evidence_id
            for evidence_id in generation.cited_evidence_ids
            if evidence_id in evidence
        )
        declared_unsupported = {
            _normal_text(value) for value in generation.declared_unsupported_claims
        }
        assessments: list[ClaimAssessment] = []
        for raw in _SENTENCE_BOUNDARY.split(generation.body_markdown):
            claim = raw.strip().lstrip("#-*0123456789.、) ").strip()
            if len(claim) < 8:
                continue
            normalized = _normal_text(claim)
            explicitly_unsupported = any(
                value and (value in normalized or normalized in value)
                for value in declared_unsupported
            )
            supporting = tuple(
                evidence_id
                for evidence_id in cited
                if _claim_supported(
                    claim,
                    evidence[evidence_id].text,
                    threshold=self._claim_overlap_threshold,
                )
            )
            if supporting and not explicitly_unsupported:
                verdict = ClaimVerdict.SUPPORTED
                reason = "Authorized cited evidence had sufficient textual overlap."
            elif cited or explicitly_unsupported:
                verdict = ClaimVerdict.UNSUPPORTED
                reason = (
                    "The claim lacked support in authorized cited evidence or was "
                    "declared unsupported by the Writer."
                )
            else:
                verdict = ClaimVerdict.NOT_ASSESSABLE
                reason = "No authorized evidence citation was available for this claim."
            assessments.append(
                ClaimAssessment(
                    claim=claim,
                    verdict=verdict,
                    supporting_evidence_ids=supporting,
                    reason=reason,
                )
            )
            if len(assessments) >= 200:
                break
        return tuple(assessments)

    def _faithfulness(
        self,
        observation: CreatorEvaluationObservation,
        claims: tuple[ClaimAssessment, ...],
    ) -> JudgeMetricScore | None:
        authorized_ids = {
            item.evidence_id for item in observation.evidence if item.authority_verified
        }
        assessable = [
            claim for claim in claims if claim.verdict != ClaimVerdict.NOT_ASSESSABLE
        ]
        if not assessable:
            return None
        supported = [
            claim
            for claim in assessable
            if claim.verdict == ClaimVerdict.SUPPORTED
            and set(claim.supporting_evidence_ids) <= authorized_ids
        ]
        return JudgeMetricScore(
            score=len(supported) / len(assessable),
            reason=(
                f"{len(supported)} of {len(assessable)} assessable claims were "
                "supported by authorized evidence."
            ),
        )

    def _relevance(
        self,
        case: EvaluationCase,
        title: str,
        body: str,
    ) -> JudgeMetricScore | None:
        output = f"{title}\n{body}"
        scores: list[float] = []
        for concept in case.criteria.required_concepts:
            scores.append(_coverage(concept, output))
        if case.criteria.reference_answer:
            reference_terms = _terms(case.criteria.reference_answer)
            output_terms = _terms(output)
            scores.append(
                len(reference_terms & output_terms) / len(reference_terms)
                if reference_terms
                else 0.0
            )
        if not scores:
            return None
        score = sum(scores) / len(scores)
        return JudgeMetricScore(
            score=score,
            reason=(
                "Relevance is deterministic coverage of labeled concepts and "
                "reference-answer terms."
            ),
        )

    def _style(
        self,
        case: EvaluationCase,
        body: str,
    ) -> tuple[JudgeMetricScore | None, tuple[str, ...]]:
        style = case.criteria.style
        checks: dict[str, bool] = {}
        lowered = body.casefold()
        for term in style.required_terms:
            checks[f"required_term:{term}"] = term.casefold() in lowered
        for term in style.forbidden_terms:
            checks[f"forbidden_term:{term}"] = term.casefold() not in lowered
        headings = [_normal_text(value) for value in _HEADING.findall(body)]
        for heading in style.required_headings:
            normalized = _normal_text(heading)
            checks[f"required_heading:{heading}"] = any(
                normalized in actual for actual in headings
            )
        character_count = len(body.strip())
        if style.min_chars is not None:
            checks["minimum_characters"] = character_count >= style.min_chars
        if style.max_chars is not None:
            checks["maximum_characters"] = character_count <= style.max_chars
        for index, exemplar in enumerate(style.exemplar_texts):
            checks[f"exemplar_overlap:{index}"] = _jaccard(exemplar, body) >= 0.15

        limitations: list[str] = []
        if style.instructions:
            limitations.append(
                "Free-form style instructions were not scored by the deterministic "
                "judge; only explicit terms, headings, length, and exemplars were used."
            )
        if not checks:
            return None, tuple(limitations)
        score = sum(1.0 for passed in checks.values() if passed) / len(checks)
        return (
            JudgeMetricScore(
                score=score,
                reason=f"{sum(checks.values())} of {len(checks)} explicit style checks passed.",
            ),
            tuple(limitations),
        )


def _coverage(expected: str, actual: str) -> float:
    normalized_expected = _normal_text(expected)
    normalized_actual = _normal_text(actual)
    if normalized_expected and normalized_expected in normalized_actual:
        return 1.0
    expected_terms = _terms(expected)
    if not expected_terms:
        return 0.0
    return len(expected_terms & _terms(actual)) / len(expected_terms)


def _claim_supported(claim: str, evidence: str, *, threshold: float) -> bool:
    normalized_claim = _normal_text(claim)
    normalized_evidence = _normal_text(evidence)
    if normalized_claim and normalized_claim in normalized_evidence:
        return True
    claim_terms = _terms(claim)
    return bool(claim_terms) and (
        len(claim_terms & _terms(evidence)) / len(claim_terms) >= threshold
    )


def _jaccard(left: str, right: str) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    union = left_terms | right_terms
    return len(left_terms & right_terms) / len(union) if union else 0.0


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = {term for term in _ASCII_TERM.findall(lowered) if term not in _STOP_TERMS}
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _normal_text(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())
