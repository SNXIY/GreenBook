"""Conservative Semantic Memory V1 contracts and admission boundary.

Semantic V1 stores only explicit, stable user facts.  It deliberately does
not infer a profile from a task, tool result, episode, or model-generated
summary.  The module owns the small source parser, admission policy, and
canonical MemoryManager adapter; it does not create a second repository or
retriever.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .manager import MemoryManager
from .models import MemoryRecord, MemoryStatus, MemoryType

SEMANTIC_MEMORY_CONTRACT = "SEMANTIC_V1"
SEMANTIC_MEMORY_VERSION = 1
SEMANTIC_MEMORY_ROLE = "stable_fact"
SEMANTIC_SOURCE_TYPE = "EXPLICIT_USER_STATEMENT"

SEMANTIC_PREDICATES = frozenset({
    "occupation_domain",
    "learning_focus",
})

_OCCUPATION_PATTERNS = (
    re.compile(
        r"(?i)\b(?:i\s+am|i'm|i\s+mainly\s+(?:do|work\s+in))\b"
        r".{0,40}\bjava\b.{0,20}\bbackend\b"
    ),
    re.compile(
        r"(?i)\b(?:java\s+backend|backend\s+java)\s+"
        r"(?:developer|engineer)\b"
    ),
    re.compile(
        r"(?i)\u6211(?:\u662f|\u4e3b\u8981\u505a|\u4e3b\u8981\u4ece\u4e8b)"
        r".{0,20}java.{0,12}\u540e\u7aef"
    ),
)
_LEARNING_PATTERNS = (
    re.compile(
        r"(?i)\b(?:i\s+am|i'm|i\s+currently|i\s+am\s+currently)\s+"
        r"(?:learning|studying|exploring)\s+"
        r"(?:ai\s+)?(?:agent|java)\b"
    ),
    re.compile(
        r"(?i)\b(?:learning|studying|exploring)\s+"
        r"(?:ai\s+)?(?:agent|java)\b"
    ),
    re.compile(
        r"(?i)\u6211(?:\u73b0\u5728|\u76ee\u524d)?(?:\u5728)?"
        r"(?:\u4e3b\u8981)?(?:\u5b66\u4e60|\u5b66).{0,8}(?:agent|java)"
    ),
    re.compile(
        r"(?i)(?:\u73b0\u5728|\u76ee\u524d)(?:\u5728)?"
        r"(?:\u4e3b\u8981)?(?:\u5b66\u4e60|\u5b66).{0,8}(?:agent|java)"
    ),
)
_TRANSIENT_MARKERS = (
    "today",
    "yesterday",
    "for this task",
    "for this project",
    "this task",
    "this project",
    "\u4eca\u5929",
    "\u6628\u5929",
    "\u8fd9\u6b21",
    "\u8fd9\u4e2a\u4efb\u52a1",
    "\u8fd9\u4e2a\u9879\u76ee",
)
_INFERENCE_MARKERS = (
    "therefore",
    "probably",
    "maybe",
    "i guess",
    "so i am",
    "\u6240\u4ee5",
    "\u53ef\u80fd",
    "\u5927\u6982",
    "\u5e94\u8be5",
)
_FORBIDDEN_ID_RE = re.compile(
    r"(?i)\b(?:run|execution|operation|task|objective|draft|schedule|"
    r"post|resource|approval)[_-]?id\b"
)
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_SENSITIVE_MARKERS = (
    "password",
    "secret",
    "token",
    "credit card",
    "medical",
    "diagnosis",
    "\u5bc6\u7801",
    "\u4ee4\u724c",
    "\u75c5\u5386",
)


class SemanticCandidate(BaseModel):
    """One explicit, normalized, cross-conversation fact candidate."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    subject: str
    predicate: str
    object: str
    normalized_fact: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    source_type: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    observed_at: str

    @field_validator(
        "user_id",
        "tenant_id",
        "subject",
        "predicate",
        "object",
        "source_type",
        "observed_at",
        mode="before",
    )
    @classmethod
    def _required_text(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if not rendered:
            raise ValueError("semantic candidate requires a non-empty field")
        return rendered

    @field_validator("observed_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("semantic observed_at must be an ISO timestamp") from exc
        return value


class SemanticAdmissionDecision(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    UNKNOWN = "UNKNOWN"


class SemanticAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SemanticAdmissionDecision
    reason: str = ""

    @property
    def should_write(self) -> bool:
        return self.decision == SemanticAdmissionDecision.KEEP

    @property
    def effective_decision(self) -> SemanticAdmissionDecision:
        return (
            SemanticAdmissionDecision.DROP
            if self.decision == SemanticAdmissionDecision.UNKNOWN
            else self.decision
        )


class SemanticCandidateBuilder:
    """Parse only explicit V1 self-statements into semantic candidates."""

    def build(
        self,
        user_text: str,
        *,
        user_id: str,
        tenant_id: str,
        observed_at: str | None = None,
        source_id: str | None = None,
    ) -> list[SemanticCandidate]:
        scope_user = str(user_id or "").strip()
        scope_tenant = str(tenant_id or "").strip()
        text = " ".join(str(user_text or "").split()).strip()
        if not scope_user or not scope_tenant or not text:
            return []
        folded = text.casefold()
        if any(marker in folded for marker in _TRANSIENT_MARKERS):
            return []
        if any(marker in folded for marker in _INFERENCE_MARKERS):
            return []
        if _FORBIDDEN_ID_RE.search(text) or _UUID_RE.search(text):
            return []
        if any(marker in folded for marker in _SENSITIVE_MARKERS):
            return []

        timestamp = str(observed_at or datetime.now(UTC).isoformat()).strip()
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return []
        source_key = str(source_id or "").strip()
        if not source_key:
            digest = hashlib.sha256(text.casefold().encode()).hexdigest()[:32]
            source_key = f"message:{digest}"
        source_hash = hashlib.sha256(text.encode()).hexdigest()[:32]

        facts: list[tuple[str, str, str]] = []
        if any(pattern.search(text) for pattern in _OCCUPATION_PATTERNS):
            facts.append((
                "occupation_domain",
                "java_backend",
                "The user's primary work domain is Java backend development.",
            ))
        if any(pattern.search(text) for pattern in _LEARNING_PATTERNS):
            learning_object = "ai_agent" if re.search(
                r"(?i)agent|\u667a\u80fd\u4f53",
                text,
            ) else "java"
            fact_text = (
                "The user is learning AI Agent development."
                if learning_object == "ai_agent"
                else "The user is learning Java."
            )
            facts.append(("learning_focus", learning_object, fact_text))

        candidates: list[SemanticCandidate] = []
        for index, (predicate, object_value, normalized_fact) in enumerate(facts):
            candidates.append(SemanticCandidate(
                user_id=scope_user,
                tenant_id=scope_tenant,
                subject="user",
                predicate=predicate,
                object=object_value,
                normalized_fact=normalized_fact,
                confidence=0.98,
                source_type=SEMANTIC_SOURCE_TYPE,
                provenance={
                    "semantic_contract": SEMANTIC_MEMORY_CONTRACT,
                    "memory_version": SEMANTIC_MEMORY_VERSION,
                    "source": "explicit_user_statement",
                    "source_type": SEMANTIC_SOURCE_TYPE,
                    "author_role": "user",
                    "source_id": source_key,
                    "source_hash": source_hash,
                    "claim_index": index,
                },
                observed_at=timestamp,
            ))
        return candidates


class SemanticAdmissionPolicy:
    """Conservative admission policy; UNKNOWN is always write-disabled."""

    def evaluate(
        self,
        candidate: SemanticCandidate | None,
    ) -> SemanticAdmissionResult:
        if candidate is None:
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.DROP,
                reason="no_candidate",
            )
        if candidate.subject != "user":
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.DROP,
                reason="unsupported_subject",
            )
        if candidate.predicate not in SEMANTIC_PREDICATES:
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.DROP,
                reason="unsupported_predicate",
            )
        if candidate.source_type != SEMANTIC_SOURCE_TYPE:
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.UNKNOWN,
                reason="source_is_not_explicit_user_statement",
            )
        provenance = candidate.provenance
        if (
            provenance.get("source") != "explicit_user_statement"
            or provenance.get("author_role") != "user"
            or provenance.get("semantic_contract") != SEMANTIC_MEMORY_CONTRACT
        ):
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.UNKNOWN,
                reason="explicit_provenance_incomplete",
            )
        if candidate.confidence < 0.85:
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.UNKNOWN,
                reason="confidence_below_conservative_threshold",
            )
        folded = f"{candidate.object} {candidate.normalized_fact}".casefold()
        if _FORBIDDEN_ID_RE.search(folded) or _UUID_RE.search(folded):
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.DROP,
                reason="runtime_identity_in_fact",
            )
        if any(marker in folded for marker in _SENSITIVE_MARKERS):
            return SemanticAdmissionResult(
                decision=SemanticAdmissionDecision.DROP,
                reason="sensitive_fact_not_admitted",
            )
        return SemanticAdmissionResult(
            decision=SemanticAdmissionDecision.KEEP,
            reason="explicit_stable_user_fact",
        )


class SemanticMemoryService:
    """Canonical Semantic writer over the existing MemoryManager."""

    def __init__(
        self,
        memory_manager: MemoryManager,
        *,
        builder: SemanticCandidateBuilder | None = None,
        policy: SemanticAdmissionPolicy | None = None,
        enabled: bool = True,
    ) -> None:
        self._memory = memory_manager
        self._builder = builder or SemanticCandidateBuilder()
        self._policy = policy or SemanticAdmissionPolicy()
        self._enabled = bool(enabled)

    def build_candidates(
        self,
        user_text: str,
        *,
        user_id: str,
        tenant_id: str,
        observed_at: str | None = None,
        source_id: str | None = None,
    ) -> list[SemanticCandidate]:
        if not self._enabled:
            return []
        return self._builder.build(
            user_text,
            user_id=user_id,
            tenant_id=tenant_id,
            observed_at=observed_at,
            source_id=source_id,
        )

    def evaluate(
        self,
        candidate: SemanticCandidate | None,
    ) -> SemanticAdmissionResult:
        return self._policy.evaluate(candidate)

    def write(self, candidate: SemanticCandidate | None) -> MemoryRecord | None:
        if not self._enabled or candidate is None:
            return None
        decision = self._policy.evaluate(candidate)
        if not decision.should_write:
            return None
        record = self._record(candidate)
        return self._memory.remember_semantic(
            record,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object_value=candidate.object,
        )

    def process_user_statement(
        self,
        user_text: str,
        *,
        user_id: str,
        tenant_id: str,
        observed_at: str | None = None,
        source_id: str | None = None,
    ) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for candidate in self.build_candidates(
            user_text,
            user_id=user_id,
            tenant_id=tenant_id,
            observed_at=observed_at,
            source_id=source_id,
        ):
            record = self.write(candidate)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _memory_id(candidate: SemanticCandidate) -> str:
        identity = "|".join((
            SEMANTIC_MEMORY_CONTRACT,
            candidate.user_id,
            candidate.tenant_id,
            candidate.subject,
            candidate.predicate,
            candidate.object,
        ))
        return f"semv1-{hashlib.sha256(identity.encode()).hexdigest()}"

    def _record(self, candidate: SemanticCandidate) -> MemoryRecord:
        return MemoryRecord(
            memory_id=self._memory_id(candidate),
            user_id=candidate.user_id,
            tenant_id=candidate.tenant_id,
            status=MemoryStatus.ACTIVE,
            memory_type=MemoryType.SEMANTIC,
            content=candidate.normalized_fact,
            structured_metadata={
                "memory_contract": SEMANTIC_MEMORY_CONTRACT,
                "memory_version": SEMANTIC_MEMORY_VERSION,
                "memory_role": SEMANTIC_MEMORY_ROLE,
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "object": candidate.object,
                "normalized_fact": candidate.normalized_fact,
                "observed_at": candidate.observed_at,
                "provenance": dict(candidate.provenance),
            },
            importance=0.7,
            confidence=candidate.confidence,
            source_type=SEMANTIC_SOURCE_TYPE,
            source_id=str(candidate.provenance.get("source_id") or "") or None,
        )


__all__ = [
    "SEMANTIC_MEMORY_CONTRACT",
    "SEMANTIC_MEMORY_ROLE",
    "SEMANTIC_MEMORY_VERSION",
    "SEMANTIC_PREDICATES",
    "SEMANTIC_SOURCE_TYPE",
    "SemanticAdmissionDecision",
    "SemanticAdmissionPolicy",
    "SemanticAdmissionResult",
    "SemanticCandidate",
    "SemanticCandidateBuilder",
    "SemanticMemoryService",
]
