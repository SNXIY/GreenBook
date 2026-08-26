"""Conservative Episodic Memory V1 contracts and projection boundary.

An Episode is a derived, verified history record.  It is not a copy of a
Task, Execution, Run, or Resource projection.  This module keeps the source
adapter, candidate builder, worth-remembering policy, and canonical writer in
one small boundary so future memory types cannot grow a parallel write path.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from greenbook_contracts.identity import AuthContext
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..execution.action_observation import ActionObservation
from ..execution.execution_queue import ExecutionQueueMessage
from ..task.models import Objective, ObjectiveStatus
from ..task.provider import TaskScope
from .manager import MemoryManager
from .models import MemoryRecord, MemoryStatus, MemoryType

EPISODIC_MEMORY_CONTRACT = "EPISODIC_V1"
EPISODIC_MEMORY_VERSION = 1
EPISODIC_SOURCE_TYPE = "VERIFIED_ACTION_OBSERVATION"

CONTENT_PUBLICATION_CATEGORY = "CONTENT_PUBLICATION_WORKFLOW"
CONTENT_PUBLICATION_OUTCOME = "VERIFIED_PUBLICATION_AFTER_USER_REVISION"
DEFAULT_EPISODE_SUMMARY = (
    "用户在一次内容发布流程中主动调整标题和发布时间后，经验证成功发布。 "
    "In a verified content publication workflow, the user actively revised "
    "the title and publication time before the content was successfully published."
)

_VERIFIED_SOURCE_TYPES = frozenset({
    "JAVA_READ_BACK",
    "VERIFIED_BUSINESS_OUTCOME",
    "VERIFIED_COMPLETION_PROJECTION",
    "VERIFIED_ACTION_OBSERVATION",
})
_FORBIDDEN_SOURCE_MARKERS = frozenset({
    "CONVERSATION_SUMMARY",
    "LLM",
    "LLM_INTERMEDIATE",
    "RUNTIME_RESULT",
    "TOOL_RESULT",
    "UNVERIFIED_TOOL_RESULT",
})
_IDENTITY_KEY_RE = re.compile(
    r"(?i)\b(?:execution|operation|run|schedule|draft|task|objective|approval)"
    r"(?:[_ -]?id)\b"
)
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


class EpisodeCandidate(BaseModel):
    """Semantic candidate derived from verified runtime evidence."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    category: str
    summary: str = Field(min_length=1, max_length=1000)
    outcome: str
    occurred_at: str
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_type: str

    @field_validator(
        "user_id",
        "tenant_id",
        "category",
        "outcome",
        "occurred_at",
        "source_type",
        mode="before",
    )
    @classmethod
    def _required_text(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if not rendered:
            raise ValueError("episode candidate requires a non-empty field")
        return rendered

    @field_validator("occurred_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("episode occurred_at must be an ISO timestamp") from exc
        return value


class VerifiedBusinessOutcome(BaseModel):
    """Trusted adapter output used before candidate construction.

    This is deliberately not a MemoryRecord.  The adapter must supply the
    exact Task/Objective join and a verified business source; the builder will
    reject missing or mismatched identities rather than selecting an active or
    recent Objective.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = ""
    objective_id: str = ""
    category: str = CONTENT_PUBLICATION_CATEGORY
    summary: str = DEFAULT_EPISODE_SUMMARY
    outcome: str = CONTENT_PUBLICATION_OUTCOME
    occurred_at: str = ""
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    verified: bool = False
    source_type: str = "VERIFIED_BUSINESS_OUTCOME"
    provenance: dict[str, Any] = Field(default_factory=dict)
    revision_fields: list[str] = Field(default_factory=list)
    user_initiated_revision: bool = False
    verified_resource_kinds: list[str] = Field(default_factory=list)


class WorthRememberingDecision(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    UNKNOWN = "UNKNOWN"


class WorthRememberingResult(BaseModel):
    """Policy decision with an explicit conservative write boundary."""

    model_config = ConfigDict(extra="forbid")

    decision: WorthRememberingDecision
    reason: str = ""

    @property
    def should_write(self) -> bool:
        # UNKNOWN is intentionally equivalent to DROP for V1 writes.
        return self.decision == WorthRememberingDecision.KEEP

    @property
    def effective_decision(self) -> WorthRememberingDecision:
        return (
            WorthRememberingDecision.DROP
            if self.decision == WorthRememberingDecision.UNKNOWN
            else self.decision
        )


class EpisodeCandidateBuilder:
    """Build one Episode candidate from three explicit verified inputs."""

    def build(
        self,
        *,
        observation: ActionObservation | Mapping[str, Any],
        objective: Objective | Mapping[str, Any],
        verified_outcome: VerifiedBusinessOutcome | Mapping[str, Any],
        user_id: str,
        tenant_id: str,
    ) -> EpisodeCandidate | None:
        scope_user = str(user_id or "").strip()
        scope_tenant = str(tenant_id or "").strip()
        if not scope_user or not scope_tenant:
            return None
        observation_value = _coerce_model(observation, ActionObservation)
        objective_value = _coerce_model(objective, Objective)
        outcome_value = _coerce_model(verified_outcome, VerifiedBusinessOutcome)
        if observation_value is None or objective_value is None or outcome_value is None:
            return None

        if _status(observation_value) != "COMPLETED":
            return None
        if _status(objective_value) != ObjectiveStatus.COMPLETED.value:
            return None
        if not outcome_value.verified:
            return None

        observation_task_id = str(observation_value.task_id or "").strip()
        objective_task_id = str(objective_value.task_id or "").strip()
        outcome_task_id = str(outcome_value.task_id or "").strip()
        if not observation_task_id or not objective_task_id or not outcome_task_id:
            return None
        if not (
            observation_task_id == objective_task_id == outcome_task_id
        ):
            return None

        objective_id = str(objective_value.objective_id or "").strip()
        if not objective_id or str(outcome_value.objective_id or "").strip() != objective_id:
            return None

        source_type = str(outcome_value.source_type or "").strip().upper()
        if (
            source_type in _FORBIDDEN_SOURCE_MARKERS
            or source_type not in _VERIFIED_SOURCE_TYPES
        ):
            return None
        if str(outcome_value.category or "").strip() != CONTENT_PUBLICATION_CATEGORY:
            return None
        if str(outcome_value.outcome or "").strip() != CONTENT_PUBLICATION_OUTCOME:
            return None

        revision_fields = _normalise_revision_fields(outcome_value.revision_fields)
        if not outcome_value.user_initiated_revision:
            return None
        if not {"title", "publish_time"}.issubset(revision_fields):
            return None

        occurred_at = str(
            outcome_value.occurred_at
            or observation_value.observed_at
            or objective_value.completed_at
            or ""
        ).strip()
        if not occurred_at:
            return None
        try:
            datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        except ValueError:
            return None

        summary = str(outcome_value.summary or DEFAULT_EPISODE_SUMMARY).strip()
        if not summary or _contains_runtime_identity(summary):
            return None
        if len(summary) > 1000:
            summary = summary[:1000]

        provenance = {
            "memory_contract": EPISODIC_MEMORY_CONTRACT,
            "memory_version": EPISODIC_MEMORY_VERSION,
            "verified": True,
            "observation_id": str(observation_value.observation_id),
            "execution_id": str(observation_value.execution_id),
            "task_id": objective_task_id,
            "objective_id": objective_id,
            "conversation_id": str(observation_value.conversation_id or ""),
            "verification_source": source_type,
            "revision_fields": revision_fields,
        }
        if outcome_value.provenance:
            provenance["verification_refs"] = _bounded_provenance(
                outcome_value.provenance
            )
        return EpisodeCandidate(
            user_id=scope_user,
            tenant_id=scope_tenant,
            category=CONTENT_PUBLICATION_CATEGORY,
            summary=summary,
            outcome=CONTENT_PUBLICATION_OUTCOME,
            occurred_at=occurred_at,
            confidence=outcome_value.confidence,
            provenance=provenance,
            source_type=EPISODIC_SOURCE_TYPE,
        )


class WorthRememberingPolicy:
    """Conservative policy for the single Episodic V1 vertical slice."""

    def evaluate(self, candidate: EpisodeCandidate | None) -> WorthRememberingResult:
        if candidate is None:
            return WorthRememberingResult(
                decision=WorthRememberingDecision.DROP,
                reason="no_candidate",
            )
        if candidate.category != CONTENT_PUBLICATION_CATEGORY:
            return WorthRememberingResult(
                decision=WorthRememberingDecision.DROP,
                reason="outside_v1_vertical_slice",
            )
        if candidate.outcome != CONTENT_PUBLICATION_OUTCOME:
            return WorthRememberingResult(
                decision=WorthRememberingDecision.DROP,
                reason="ordinary_or_unverified_outcome",
            )
        if candidate.source_type != EPISODIC_SOURCE_TYPE:
            return WorthRememberingResult(
                decision=WorthRememberingDecision.UNKNOWN,
                reason="unsupported_candidate_source",
            )
        if candidate.confidence < 0.8:
            return WorthRememberingResult(
                decision=WorthRememberingDecision.UNKNOWN,
                reason="confidence_below_conservative_threshold",
            )
        provenance = candidate.provenance
        fields = set(_normalise_revision_fields(provenance.get("revision_fields", [])))
        if (
            provenance.get("memory_contract") != EPISODIC_MEMORY_CONTRACT
            or provenance.get("verified") is not True
            or not {"title", "publish_time"}.issubset(fields)
        ):
            return WorthRememberingResult(
                decision=WorthRememberingDecision.UNKNOWN,
                reason="verified_revision_evidence_incomplete",
            )
        return WorthRememberingResult(
            decision=WorthRememberingDecision.KEEP,
            reason="verified_reusable_publication_history",
        )


class EpisodicMemoryService:
    """Canonical Episodic writer over the existing MemoryManager."""

    def __init__(
        self,
        memory_manager: MemoryManager,
        *,
        builder: EpisodeCandidateBuilder | None = None,
        policy: WorthRememberingPolicy | None = None,
        enabled: bool = True,
    ) -> None:
        self._memory = memory_manager
        self._builder = builder or EpisodeCandidateBuilder()
        self._policy = policy or WorthRememberingPolicy()
        self._enabled = bool(enabled)

    def evaluate(self, candidate: EpisodeCandidate | None) -> WorthRememberingResult:
        return self._policy.evaluate(candidate)

    def write(self, candidate: EpisodeCandidate | None) -> MemoryRecord | None:
        decision = self._policy.evaluate(candidate)
        if not self._enabled or candidate is None or not decision.should_write:
            return None
        observation_id = str(candidate.provenance.get("observation_id") or "").strip()
        if not observation_id:
            return None
        return self._memory.remember(self._record(candidate, observation_id))

    def process(
        self,
        *,
        observation: ActionObservation | Mapping[str, Any],
        objective: Objective | Mapping[str, Any],
        verified_outcome: VerifiedBusinessOutcome | Mapping[str, Any],
        user_id: str,
        tenant_id: str,
    ) -> MemoryRecord | None:
        candidate = self._builder.build(
            observation=observation,
            objective=objective,
            verified_outcome=verified_outcome,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        return self.write(candidate)

    @staticmethod
    def _memory_id(candidate: EpisodeCandidate, observation_id: str) -> str:
        identity = "|".join((
            EPISODIC_MEMORY_CONTRACT,
            candidate.user_id,
            candidate.tenant_id,
            candidate.source_type,
            observation_id,
        ))
        return f"epv1-{hashlib.sha256(identity.encode()).hexdigest()}"

    def _record(self, candidate: EpisodeCandidate, observation_id: str) -> MemoryRecord:
        return MemoryRecord(
            memory_id=self._memory_id(candidate, observation_id),
            user_id=candidate.user_id,
            tenant_id=candidate.tenant_id,
            status=MemoryStatus.ACTIVE,
            memory_type=MemoryType.EPISODIC,
            content=candidate.summary,
            structured_metadata={
                "memory_contract": EPISODIC_MEMORY_CONTRACT,
                "memory_version": EPISODIC_MEMORY_VERSION,
                "memory_role": "relevant_past_experience",
                "category": candidate.category,
                "outcome": candidate.outcome,
                "occurred_at": candidate.occurred_at,
                "revision_fields": list(
                    _normalise_revision_fields(
                        candidate.provenance.get("revision_fields", [])
                    )
                ),
                "provenance": dict(candidate.provenance),
            },
            importance=0.75,
            confidence=candidate.confidence,
            source_type=candidate.source_type,
            source_id=observation_id,
        )


class EpisodicMemoryProjector:
    """Post-observation adapter that supplies only verified source facts.

    The queue/runtime owns execution.  This adapter runs after completion and
    observation persistence, reads the exact persisted Execution/Objectives,
    and silently declines unless the verified post and explicit user revision
    evidence both exist.  It never uses an active/current/recent fallback.
    """

    def __init__(
        self,
        *,
        service: EpisodicMemoryService,
        execution_repository: Any,
        task_provider: Any,
    ) -> None:
        self._service = service
        self._execution_repository = execution_repository
        self._task_provider = task_provider

    async def __call__(
        self,
        *,
        observation: ActionObservation,
        message: ExecutionQueueMessage,
        result: Any,
        auth: AuthContext,
        execution: Any | None = None,
    ) -> MemoryRecord | None:
        del message, result
        if _status(observation) != "COMPLETED":
            return None
        if not auth.user_id or not auth.tenant_id:
            return None
        execution_value = execution
        if execution_value is None:
            finder = getattr(self._execution_repository, "find_by_id", None)
            if not callable(finder):
                return None
            execution_value = finder(observation.execution_id)
            if inspect.isawaitable(execution_value):
                execution_value = await execution_value
        if execution_value is None:
            return None
        if str(_value(execution_value, "execution_id") or "") != observation.execution_id:
            return None
        if _status(execution_value) != "COMPLETED":
            return None
        if str(_value(execution_value, "task_id") or "") != observation.task_id:
            return None
        objective_id = str(_value(execution_value, "objective_id") or "").strip()
        if not objective_id:
            return None
        task_id = str(observation.task_id or "").strip()
        conversation_id = str(observation.conversation_id or "").strip()
        if not task_id or not conversation_id:
            return None

        task = await self._load_task(
            task_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
        )
        if task is None:
            return None
        if (
            str(_value(task, "task_id") or "") != task_id
            or str(_value(task, "user_id") or "") != auth.user_id
            or str(_value(task, "tenant_id") or "") != auth.tenant_id
        ):
            return None
        objectives = [
            item
            for item in (_value(task, "objectives") or ())
            if str(_value(item, "objective_id") or "") == objective_id
        ]
        if len(objectives) != 1:
            return None
        objective = objectives[0]
        if _status(objective) != ObjectiveStatus.COMPLETED.value:
            return None

        revision_fields, revision_refs = _revision_evidence(task, objective_id)
        if not {"title", "publish_time"}.issubset(revision_fields):
            return None
        if not _verified_post(observation, task, objective_id):
            return None

        verified = VerifiedBusinessOutcome(
            task_id=task_id,
            objective_id=objective_id,
            category=CONTENT_PUBLICATION_CATEGORY,
            summary=DEFAULT_EPISODE_SUMMARY,
            outcome=CONTENT_PUBLICATION_OUTCOME,
            occurred_at=str(observation.observed_at or ""),
            confidence=0.95,
            verified=True,
            source_type="VERIFIED_COMPLETION_PROJECTION",
            provenance={
                "revision_refs": revision_refs,
                "verified_resource_kind": "POST",
            },
            revision_fields=sorted(revision_fields),
            user_initiated_revision=True,
            verified_resource_kinds=["POST"],
        )
        return self._service.process(
            observation=observation,
            objective=objective,
            verified_outcome=verified,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )

    async def _load_task(
        self,
        task_id: str,
        *,
        user_id: str,
        tenant_id: str,
        conversation_id: str,
    ) -> Any | None:
        getter = getattr(self._task_provider, "get_task", None)
        if not callable(getter):
            return None
        scope = TaskScope(
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        try:
            value = getter(scope, task_id)
        except TypeError:
            # Compatibility adapters may expose the older keyword shape.  It
            # remains explicitly scoped; no active/recent lookup is allowed.
            value = getter(
                task_id,
                user_id=user_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
        return await value if inspect.isawaitable(value) else value


def _coerce_model(value: Any, model: type[BaseModel]) -> BaseModel | None:
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except Exception:
        return None


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _status(value: Any) -> str:
    status = _value(value, "status")
    return str(getattr(status, "value", status) or "").upper()


def _normalise_revision_fields(values: Any) -> list[str]:
    aliases = {
        "title": "title",
        "title_text": "title",
        "publish_time": "publish_time",
        "publication_time": "publish_time",
        "scheduled_at": "publish_time",
        "run_at": "publish_time",
    }
    result: list[str] = []
    for value in values or ():
        key = aliases.get(str(value or "").strip().casefold())
        if key and key not in result:
            result.append(key)
    return result


def _contains_runtime_identity(value: str) -> bool:
    return bool(_IDENTITY_KEY_RE.search(value) or _UUID_RE.search(value))


def _bounded_provenance(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return str(value)[:240]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_provenance(item, depth=depth + 1)
            for key, item in list(value.items())[:12]
            if str(key).casefold() not in {"body", "content", "raw_result", "secret"}
        }
    if isinstance(value, list):
        return [_bounded_provenance(item, depth=depth + 1) for item in value[:12]]
    return str(value)[:240] if isinstance(value, str) else value


def _revision_evidence(task: Any, objective_id: str) -> tuple[set[str], list[str]]:
    fields: set[str] = set()
    refs: list[str] = []
    for revision in _value(task, "revisions") or ():
        payload = _value(revision, "payload") or {}
        if not isinstance(payload, Mapping) or payload.get("kind") != "ACTION_LOOP_MUTATION_PLAN":
            continue
        for change in payload.get("task_changes") or ():
            if not isinstance(change, Mapping):
                continue
            if str(change.get("operation") or "").upper() != "UPDATE_GOAL":
                continue
            desired = change.get("desired_changes") or {}
            if not isinstance(desired, Mapping):
                continue
            target_reference = change.get("target_reference") or {}
            if not isinstance(target_reference, Mapping):
                target_reference = {}
            target = str(
                desired.get("objective_id")
                or desired.get("mutation_objective_id")
                or target_reference.get("objective_id")
                or ""
            )
            if target != objective_id:
                continue
            if desired.get("title") or desired.get("title_text"):
                fields.add("title")
            if any(
                desired.get(key)
                for key in ("run_at", "publish_time", "publication_time", "scheduled_at")
            ):
                fields.add("publish_time")
            revision_id = str(_value(revision, "revision_id") or "")
            if revision_id and revision_id not in refs:
                refs.append(revision_id)
    return fields, refs[-20:]


def _verified_post(
    observation: ActionObservation,
    task: Any,
    objective_id: str,
) -> bool:
    observed_ids: set[str] = set()
    for ref in observation.resource_refs or ():
        if not isinstance(ref, Mapping):
            continue
        kind = str(ref.get("resource_type") or ref.get("resource_kind") or "").upper()
        resource_id = str(ref.get("resource_id") or "").strip()
        if kind == "POST" and resource_id:
            observed_ids.add(resource_id)
    if not observed_ids:
        return False
    verified_statuses = {"PUBLISHED", "COMPLETED", "ACTIVE"}
    for resource in _value(task, "resource_index") or ():
        if str(_value(resource, "resource_kind") or "").upper() != "POST":
            continue
        if str(_value(resource, "objective_id") or "") != objective_id:
            continue
        if str(_value(resource, "resource_id") or "") not in observed_ids:
            continue
        if str(_value(resource, "status") or "").upper() in verified_statuses:
            return True
    return False


__all__ = [
    "CONTENT_PUBLICATION_CATEGORY",
    "CONTENT_PUBLICATION_OUTCOME",
    "DEFAULT_EPISODE_SUMMARY",
    "EPISODIC_MEMORY_CONTRACT",
    "EPISODIC_MEMORY_VERSION",
    "EPISODIC_SOURCE_TYPE",
    "EpisodeCandidate",
    "EpisodeCandidateBuilder",
    "EpisodicMemoryProjector",
    "EpisodicMemoryService",
    "VerifiedBusinessOutcome",
    "WorthRememberingDecision",
    "WorthRememberingPolicy",
    "WorthRememberingResult",
]
