"""Generic result production for the ActionLoop.

Not every Task is satisfied by a tool/resource.  The user intent falls into one
of three completion requirements:

- DIRECT_RESULT:     the tool/resource itself IS the answer (query a status).
- RESOURCE_MUTATION: the goal is that a business resource changed; a verified
                     postcondition completes it (edit a draft, cancel publish).
- GROUNDED_SYNTHESIS: a NEW natural-language answer must be composed from real
                     current-Task evidence (search-then-summarize, compare,
                     analyze, explain).  A resource alone is NOT completion.

The ResultComposer is a thin facts->user-facing-result layer.  It does NOT plan,
run tools, manage the Task, or own the Worker.  It only turns verified evidence
into a FinalResult with source_refs and coverage.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


class ResultRequirement:
    """Canonical completion kinds for an Objective."""

    DIRECT_RESULT = "DIRECT_RESULT"
    RESOURCE_MUTATION = "RESOURCE_MUTATION"
    GROUNDED_SYNTHESIS = "GROUNDED_SYNTHESIS"

    ALL = {DIRECT_RESULT, RESOURCE_MUTATION, GROUNDED_SYNTHESIS}


def classify_result_requirement(capability: Any) -> str:
    """Metadata-driven result kind for a capability (no keyword rules).

    A reasoning-backed capability (is_llm_step, no tool) produces a new answer
    from evidence -> GROUNDED_SYNTHESIS.  Everything with a real tool produces
    the answer (or a resource change) directly -> DIRECT_RESULT.  RESOURCE_MUTATION
    is assigned by the loop when a write submission carries a verified postcondition.
    """
    if capability is None:
        return ResultRequirement.DIRECT_RESULT
    # Explicit metadata wins (authoritative source of truth for result kind).
    explicit = str(getattr(capability, "result_requirement", "") or "").upper()
    if explicit in ResultRequirement.ALL:
        return explicit
    # Fallback heuristic: a reasoning-backed capability produces a new answer.
    if bool(getattr(capability, "is_llm_step", False)):
        return ResultRequirement.GROUNDED_SYNTHESIS
    return ResultRequirement.DIRECT_RESULT


class FinalResult(BaseModel):
    """The user-facing result for one completed Objective."""

    model_config = ConfigDict(extra="forbid")

    requirement: str = ResultRequirement.DIRECT_RESULT
    content: str = ""
    source_refs: list[str] = Field(default_factory=list)
    coverage: float = 0.0          # fraction of required evidence gathered (0..1)
    ready: bool = False            # enough evidence to state a reliable answer
    result_artifact_id: str = ""


class ResultComposer:
    """facts/evidence -> user-facing FinalResult.

    Deterministic scaffold: it gathers verified evidence, computes coverage, and
    delegates natural-language composition to an injected generator (the loop's
    LLM in production, a stub in tests).  It never re-plans or runs tools.
    """

    def __init__(self, generator: Callable[[str, Sequence[Mapping[str, Any]]], Any] | None = None) -> None:
        self._generator = generator

    # Candidate-set resources are NOT strong evidence: a SEARCH_RESULT is a list
    # of candidates, not read detail.  Only detail/artifact resources (POST,
    # ANALYSIS_REPORT, ...) ground a synthesis conclusion.
    _CANDIDATE_KINDS = {"SEARCH_RESULT", "CANDIDATE_SET", "SEARCH"}

    def evidence_from_task(
        self,
        task: Any,
        requirement: str = ResultRequirement.GROUNDED_SYNTHESIS,
        *,
        objective: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Collect verified, current-Task-only evidence facts.

        Only real detail/artifact resources bound to THIS task count; candidate
        sets (SEARCH_RESULT) are excluded so a search alone never reads as
        "evidence ready".  Historical/other-task facts must not satisfy a
        current synthesis objective.
        """
        objective_id = str(getattr(objective, "objective_id", "") or "")
        owned_ids = set(getattr(objective, "related_resource_ids", ()) or ()) if objective_id else set()
        resource_rows = list(getattr(task, "resource_index", ()) or ())
        has_owned_resource_rows = any(
            bool(
                item.get("objective_id")
                if isinstance(item, Mapping)
                else getattr(item, "objective_id", None)
            )
            for item in resource_rows
        )
        # Old rows did not record ownership.  They remain readable only when
        # the whole Task has no ownership data; once any binding exists, every
        # Objective must use its own resources.
        enforce_owner = bool(objective_id and (owned_ids or has_owned_resource_rows))
        facts: list[dict[str, Any]] = []
        for resource in resource_rows:
            if isinstance(resource, Mapping):
                rid = str(resource.get("resource_id") or "")
                kind = str(resource.get("resource_kind") or "").upper()
                owner_id = str(resource.get("objective_id") or "")
                title = str(resource.get("title") or "")
                content = str(resource.get("content") or resource.get("summary") or "")
            else:
                rid = str(getattr(resource, "resource_id", "") or "")
                kind = str(getattr(resource, "resource_kind", "") or "").upper()
                owner_id = str(getattr(resource, "objective_id", "") or "")
                title = str(getattr(resource, "title", "") or "")
                content = str(getattr(resource, "content", "") or getattr(resource, "summary", "") or "")
            if not rid or kind in self._CANDIDATE_KINDS:
                continue
            # Current Task alone is insufficient for multi-objective evidence:
            # a synthesis can use only its own immutable ResourceBindings.
            if enforce_owner and owner_id != objective_id and rid not in owned_ids:
                continue
            facts.append({"source_ref": rid, "kind": kind, "title": title, "content": content})
        # Artifacts bound to this task are also legitimate evidence.
        for artifact in getattr(task, "artifacts", ()) or ():
            aid = str(getattr(artifact, "artifact_id", "") or getattr(artifact, "resource_id", "") or "")
            if aid:
                facts.append({"source_ref": aid, "kind": "ARTIFACT",
                              "title": str(getattr(artifact, "title", "") or "")})
        return facts

    async def compose(
        self,
        *,
        objective: Any,
        intent: str,
        task: Any,
        required_coverage: float = 0.6,
        required_evidence_count: int = 1,
        generator: Callable[[str, Sequence[Mapping[str, Any]]], Any] | None = None,
    ) -> FinalResult:
        requirement = str(getattr(objective, "result_requirement", "") or ResultRequirement.DIRECT_RESULT)
        if requirement != ResultRequirement.GROUNDED_SYNTHESIS:
            return FinalResult(requirement=requirement, ready=True)
        evidence = self.evidence_from_task(task, requirement, objective=objective)
        source_refs = [e["source_ref"] for e in evidence]
        # The Objective's structured min_sources is authoritative; a caller
        # override (required_evidence_count) only tightens it further.
        min_sources = int(getattr(objective, "min_sources", 1) or 1)
        needed = max(min_sources, int(required_evidence_count or 1))
        coverage = min(1.0, len(evidence) / needed)
        ready = coverage >= required_coverage and bool(evidence)
        if not ready:
            return FinalResult(
                requirement=requirement,
                content="",
                source_refs=source_refs,
                coverage=coverage,
                ready=False,
            )
        content = await self._compose_content(intent, evidence, generator=generator)
        return FinalResult(
            requirement=requirement,
            content=content,
            source_refs=source_refs,
            coverage=coverage,
            ready=True,
        )

    async def _compose_content(
        self,
        intent: str,
        evidence: Sequence[Mapping[str, Any]],
        *,
        generator: Callable[[str, Sequence[Mapping[str, Any]]], Any] | None = None,
    ) -> str:
        active = generator or self._generator
        if active is None:
            titles = "; ".join(str(e.get("title") or e.get("source_ref") or "") for e in evidence[:5])
            return f"综合 {len(evidence)} 个来源：{titles}" if titles else ""
        value = active(intent, list(evidence))
        if isinstance(value, Awaitable):
            value = await value
        return str(value or "")


__all__ = [
    "FinalResult",
    "ResultComposer",
    "ResultRequirement",
    "classify_result_requirement",
]
