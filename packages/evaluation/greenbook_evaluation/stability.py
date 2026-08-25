"""Lightweight repeated-run evaluation for production semantic stability.

The evaluator deliberately sits outside the Agent Runtime.  It calls the
existing :class:`ProductionSemanticAdapter` repeatedly and records only
structured semantic facts plus bounded fingerprints.  It does not change the
Interpreter prompt, model settings, resolver, comparator, or Golden cases.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .canonical import semantic_mapping_matches
from .models import EvalCase


class StabilityClassification(StrEnum):
    """Case-level classification for repeated semantic observations."""

    STABLE_CORRECT = "STABLE_CORRECT"
    STABLE_WRONG = "STABLE_WRONG"
    UNSTABLE = "UNSTABLE"
    INVALID_EVAL = "INVALID_EVAL"


class SemanticStabilityRun(BaseModel):
    """One bounded observation of one production semantic invocation."""

    case_id: str
    run_index: int
    action_family: str = ""
    publication_mode: str = ""
    temporal_kind: str = ""
    temporal_resolved: bool = False
    target_state: str = ""
    clarification_required: bool = False
    objective_count: int | None = None
    operation: str = ""
    raw_item_count: int = 0
    item_publication_intents: list[str] = Field(default_factory=list)
    item_temporal_kinds: list[str] = Field(default_factory=list)
    item_temporal_resolved: list[bool] = Field(default_factory=list)
    item_run_at: list[str | None] = Field(default_factory=list)
    run_at: str | None = None
    canonical: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""
    matches_expected: bool = False
    # This is a normalized post-Interpreter Command hash.  The raw LLM
    # response is intentionally not persisted and no chain-of-thought is
    # captured.
    interpreter_fingerprint: str = ""
    target_fingerprint: str = ""
    temporal_fingerprint: str = ""
    state_fingerprint: str = ""
    error: str = ""


class SemanticStabilityCaseResult(BaseModel):
    """Aggregated repeated-run result for one EvalCase."""

    case_id: str
    category: str = ""
    historical_status: str = ""
    expected: dict[str, Any] = Field(default_factory=dict)
    run_count: int = 0
    correct_count: int = 0
    correctness_rate: float = 0.0
    consistent: bool = False
    consistency_rate: float = 0.0
    classification: StabilityClassification = StabilityClassification.UNSTABLE
    earliest_variation_layer: str = ""
    fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    canonical_distribution: dict[str, int] = Field(default_factory=dict)
    raw_item_count_distribution: dict[str, int] = Field(default_factory=dict)
    runs: list[SemanticStabilityRun] = Field(default_factory=list)


class SemanticStabilityReport(BaseModel):
    """Case-level and run-level stability metrics."""

    representative_case_count: int = 0
    total_runs: int = 0
    stable_correct_count: int = 0
    stable_wrong_count: int = 0
    unstable_count: int = 0
    invalid_eval_count: int = 0
    mean_correctness: float = 0.0
    mean_correctness_all_cases: float = 0.0
    consistency_rate: float = 0.0
    consistency_rate_all_cases: float = 0.0
    stable_correct_cases: list[str] = Field(default_factory=list)
    stable_wrong_cases: list[str] = Field(default_factory=list)
    unstable_cases: list[str] = Field(default_factory=list)
    invalid_eval_cases: list[str] = Field(default_factory=list)
    case_results: list[SemanticStabilityCaseResult] = Field(default_factory=list)


class ProductionSemanticStabilityEvaluator:
    """Run the existing production semantic adapter repeatedly.

    ``historical_statuses`` is supplied by the caller from the existing
    bookkeeping ledger.  It is only used to keep ``INVALID_EVAL`` cases out
    of Agent-quality aggregates; it never changes a semantic result.
    """

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def evaluate_case(
        self,
        case: EvalCase,
        *,
        repeats: int = 5,
        historical_status: str = "",
    ) -> SemanticStabilityCaseResult:
        if repeats < 1:
            raise ValueError("repeats must be positive")

        observations: list[SemanticStabilityRun] = []
        expected = dict(case.expected_semantic_state or {})
        for run_index in range(1, repeats + 1):
            actual = await self.adapter.run_case(case)
            observations.append(
                _observation(
                    case,
                    actual,
                    run_index=run_index,
                    expected=expected,
                )
            )
        return aggregate_case_stability(
            case,
            observations,
            historical_status=historical_status,
        )

    async def evaluate_cases(
        self,
        cases: Sequence[EvalCase],
        *,
        repeats: int = 5,
        repeat_counts: Mapping[str, int] | None = None,
        historical_statuses: Mapping[str, str] | None = None,
    ) -> SemanticStabilityReport:
        results: list[SemanticStabilityCaseResult] = []
        counts = repeat_counts or {}
        statuses = historical_statuses or {}
        for case in cases:
            results.append(
                await self.evaluate_case(
                    case,
                    repeats=int(counts.get(case.case_id, repeats)),
                    historical_status=str(statuses.get(case.case_id, "")),
                )
            )
        return aggregate_stability_report(results)


def aggregate_case_stability(
    case: EvalCase,
    runs: Sequence[SemanticStabilityRun],
    *,
    historical_status: str = "",
) -> SemanticStabilityCaseResult:
    """Aggregate observations without changing the Golden expectation."""

    expected = dict(case.expected_semantic_state or {})
    fingerprints = [run.fingerprint for run in runs]
    canonical_values = [
        json.dumps(run.canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for run in runs
    ]
    raw_item_counts = [str(run.raw_item_count) for run in runs]
    run_count = len(runs)
    correct_count = sum(1 for run in runs if run.matches_expected)
    consistent = len(set(fingerprints)) == 1 if runs else False
    normalized_status = str(historical_status or "").upper()
    if normalized_status == StabilityClassification.INVALID_EVAL.value:
        classification = StabilityClassification.INVALID_EVAL
    elif not consistent:
        classification = StabilityClassification.UNSTABLE
    elif correct_count == run_count:
        classification = StabilityClassification.STABLE_CORRECT
    else:
        classification = StabilityClassification.STABLE_WRONG

    return SemanticStabilityCaseResult(
        case_id=case.case_id,
        category=case.category,
        historical_status=normalized_status,
        expected=expected,
        run_count=run_count,
        correct_count=correct_count,
        correctness_rate=(correct_count / run_count) if run_count else 0.0,
        consistent=consistent,
        consistency_rate=1.0 if consistent else 0.0,
        classification=classification,
        earliest_variation_layer=_earliest_variation_layer(runs),
        fingerprint_counts=dict(Counter(fingerprints)),
        canonical_distribution=dict(Counter(canonical_values)),
        raw_item_count_distribution=dict(Counter(raw_item_counts)),
        runs=list(runs),
    )


def aggregate_stability_report(
    results: Sequence[SemanticStabilityCaseResult],
) -> SemanticStabilityReport:
    """Build the light report; invalid evaluation cases stay visible but are
    excluded from the Agent-quality mean and consistency rate.
    """

    stable_correct = [
        result.case_id
        for result in results
        if result.classification == StabilityClassification.STABLE_CORRECT
    ]
    stable_wrong = [
        result.case_id
        for result in results
        if result.classification == StabilityClassification.STABLE_WRONG
    ]
    unstable = [
        result.case_id
        for result in results
        if result.classification == StabilityClassification.UNSTABLE
    ]
    invalid = [
        result.case_id
        for result in results
        if result.classification == StabilityClassification.INVALID_EVAL
    ]
    valid_results = [
        result
        for result in results
        if result.classification != StabilityClassification.INVALID_EVAL
    ]
    total_runs = sum(result.run_count for result in results)
    valid_count = len(valid_results)
    all_count = len(results)
    return SemanticStabilityReport(
        representative_case_count=all_count,
        total_runs=total_runs,
        stable_correct_count=len(stable_correct),
        stable_wrong_count=len(stable_wrong),
        unstable_count=len(unstable),
        invalid_eval_count=len(invalid),
        mean_correctness=(
            sum(result.correctness_rate for result in valid_results) / valid_count
            if valid_count
            else 0.0
        ),
        mean_correctness_all_cases=(
            sum(result.correctness_rate for result in results) / all_count
            if all_count
            else 0.0
        ),
        consistency_rate=(
            sum(result.consistent for result in valid_results) / valid_count
            if valid_count
            else 0.0
        ),
        consistency_rate_all_cases=(
            sum(result.consistent for result in results) / all_count
            if all_count
            else 0.0
        ),
        stable_correct_cases=stable_correct,
        stable_wrong_cases=stable_wrong,
        unstable_cases=unstable,
        invalid_eval_cases=invalid,
        case_results=list(results),
    )


def _observation(
    case: EvalCase,
    actual: Mapping[str, Any],
    *,
    run_index: int,
    expected: Mapping[str, Any],
) -> SemanticStabilityRun:
    canonical = dict(actual.get("semantic_state") or {})
    command = actual.get("command") or {}
    raw_state = actual.get("raw_semantic_state") or actual.get("resolved_semantics") or {}
    command_signature = _interpreter_signature(command)
    target_signature = _target_signature(command, actual)
    temporal_signature = _temporal_signature(raw_state)
    state_signature = _semantic_state_signature(raw_state)
    state_items = [item for item in (raw_state.get("items") or ()) if isinstance(item, Mapping)]
    command_items = [item for item in (command.get("items") or ()) if isinstance(item, Mapping)]
    item_publication_intents = [
        _item_publication_intent(item)
        for item in command_items
    ]
    item_temporal_kinds = [str(item.get("temporal_kind") or "") for item in state_items]
    item_temporal_resolved = [bool(item.get("temporal_resolved")) for item in state_items]
    item_run_at = [item.get("run_at") for item in state_items]
    operation = str(
        raw_state.get("semantic_operation")
        or command.get("semantic_operation")
        or command.get("type")
        or ""
    )
    return SemanticStabilityRun(
        case_id=case.case_id,
        run_index=run_index,
        action_family=str(canonical.get("action_family") or ""),
        publication_mode=str(canonical.get("publication_mode") or ""),
        temporal_kind=str(canonical.get("temporal_kind") or ""),
        temporal_resolved=bool(canonical.get("temporal_resolved")),
        target_state=str(canonical.get("target_state") or ""),
        clarification_required=bool(canonical.get("clarification_required")),
        objective_count=_optional_int(canonical.get("objective_count")),
        operation=operation,
        raw_item_count=len(command_items),
        item_publication_intents=item_publication_intents,
        item_temporal_kinds=item_temporal_kinds,
        item_temporal_resolved=item_temporal_resolved,
        item_run_at=item_run_at,
        run_at=raw_state.get("run_at"),
        canonical=canonical,
        fingerprint=stable_fingerprint(canonical),
        matches_expected=semantic_mapping_matches(expected, canonical),
        interpreter_fingerprint=stable_fingerprint(command_signature),
        target_fingerprint=stable_fingerprint(target_signature),
        temporal_fingerprint=stable_fingerprint(temporal_signature),
        state_fingerprint=stable_fingerprint(state_signature),
        error=str(actual.get("error") or ""),
    )


def stable_fingerprint(value: Any) -> str:
    """Return a deterministic hash for a JSON-compatible structured value."""

    payload = json.dumps(
        _normalize_for_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _earliest_variation_layer(runs: Sequence[SemanticStabilityRun]) -> str:
    if not runs:
        return "NO_RUNS"
    if len({run.interpreter_fingerprint for run in runs}) > 1:
        return "INTERPRETER_NONDETERMINISM"
    if len({run.target_fingerprint for run in runs}) > 1:
        return "TARGET_RESOLVER"
    if len({run.temporal_fingerprint for run in runs}) > 1:
        return "TEMPORAL_RESOLVER"
    if len({run.state_fingerprint for run in runs}) > 1:
        return "RESOLVED_SEMANTIC_PROJECTION"
    if len({run.fingerprint for run in runs}) > 1:
        return "CANONICAL_PROJECTION"
    return "STABLE"


def _interpreter_signature(command: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only semantic Interpreter output, before resolver mutations."""

    items = []
    for item in command.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        items.append({
            "title": item.get("title"),
            "topic": item.get("topic"),
            "requirements": list(item.get("requirements") or ()),
            "operation": item.get("operation"),
            "capabilities": sorted(str(value) for value in (item.get("capabilities") or ())),
            "temporal_text": item.get("temporal_text"),
            "constraints": item.get("constraints") or {},
        })
    return {
        "type": command.get("type"),
        "task_changes": [
            {
                "operation": change.get("operation"),
                "target_reference": change.get("target_reference") or {},
                "desired_changes": change.get("desired_changes") or {},
                "dependency_reference": change.get("dependency_reference") or [],
                "source_reference": change.get("source_reference") or {},
                "needs_target_resolution": change.get("needs_target_resolution"),
            }
            for change in (command.get("task_changes") or ())
            if isinstance(change, Mapping)
        ],
        "target": command.get("target") or {},
        "parameters": command.get("parameters") or {},
        "entities": command.get("entities") or {},
        "constraints": command.get("constraints") or {},
        "semantic_operation": command.get("semantic_operation"),
        "scope": command.get("scope"),
        "risk": command.get("risk"),
        "references": command.get("references") or [],
        "ambiguity": command.get("ambiguity"),
        "needs_clarification": command.get("needs_clarification"),
        "required_capabilities": sorted(
            str(value) for value in (command.get("required_capabilities") or ())
        ),
        "items": items,
    }


def _target_signature(command: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    candidates = command.get("target_candidates") or []
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        candidates = sorted(
            (_normalize_for_json(item) for item in candidates),
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    return {
        "target_resolution": command.get("target_resolution"),
        "resolved_target": command.get("resolved_target") or {},
        "target_candidates": candidates,
        "target": actual.get("target") or {},
    }


def _temporal_signature(state: Mapping[str, Any]) -> dict[str, Any]:
    items = []
    for item in state.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        constraints = item.get("constraints") or {}
        items.append({
            "temporal_text": item.get("temporal_text"),
            "temporal_kind": item.get("temporal_kind"),
            "temporal_resolved": item.get("temporal_resolved"),
            "run_at": item.get("run_at"),
            "temporal_constraints": {
                key: constraints.get(key)
                for key in ("run_at", "publish_at", "scheduled_at", "temporal_kind")
                if key in constraints
            },
        })
    constraints = state.get("constraints") or {}
    return {
        "temporal_kind": state.get("temporal_kind"),
        "temporal_resolved": state.get("temporal_resolved"),
        "run_at": state.get("run_at"),
        "temporal_constraints": {
            key: constraints.get(key)
            for key in ("run_at", "publish_at", "scheduled_at", "temporal_kind")
            if key in constraints
        },
        "items": items,
    }


def _item_publication_intent(item: Mapping[str, Any]) -> str:
    constraints = item.get("constraints") or {}
    value = (
        constraints.get("publication_intent")
        or constraints.get("publication")
        or constraints.get("publish_intent")
    )
    if value:
        return str(value)
    capabilities = {str(value).upper() for value in (item.get("capabilities") or ())}
    if "PUBLISH_NOW" in capabilities:
        return "IMMEDIATE_PUBLISH"
    if capabilities.intersection({"SCHEDULE_PUBLISH", "CREATE_SCHEDULE"}):
        return "SCHEDULED_PUBLISH"
    return ""


def _semantic_state_signature(state: Mapping[str, Any]) -> Any:
    """Normalize resolved facts while excluding per-invocation identity."""

    normalized = _normalize_for_json(state)
    if isinstance(normalized, dict):
        normalized.pop("source_command_id", None)
    return normalized


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_normalize_for_json(item) for item in value)
    if hasattr(value, "model_dump"):
        return _normalize_for_json(value.model_dump(mode="json"))
    if hasattr(value, "value"):
        return value.value
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ProductionSemanticStabilityEvaluator",
    "SemanticStabilityCaseResult",
    "SemanticStabilityReport",
    "SemanticStabilityRun",
    "StabilityClassification",
    "aggregate_case_stability",
    "aggregate_stability_report",
        "stable_fingerprint",
]
