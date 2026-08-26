"""Conservative Procedural Memory V1 boundary.

Procedural V1 stores one explicit, reusable user workflow preference as
bounded soft guidance.  It is deliberately not a planner, workflow engine,
tool router, capability grant, or runtime invariant.  All persistence goes
through the existing MemoryManager and all reads go through MemoryRetriever.
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

PROCEDURAL_MEMORY_CONTRACT = "PROCEDURAL_V1"
PROCEDURAL_MEMORY_VERSION = 1
PROCEDURAL_MEMORY_ROLE = "relevant_procedure"
PROCEDURAL_SOURCE_TYPE = "EXPLICIT_USER_INSTRUCTION"

TECHNICAL_ARTICLE_PROCEDURE_KEY = "technical_article_creation"
TECHNICAL_ARTICLE_TRIGGER = "create_technical_article"
TECHNICAL_ARTICLE_GUIDANCE = (
    "\u5148\u751f\u6210\u5927\u7eb2\uff0c\u518d\u6839\u636e\u5927\u7eb2\u751f\u6210\u6b63\u6587"
)
TECHNICAL_ARTICLE_GUIDANCE_EN = (
    "Generate an outline first, then write the body from that outline."
)
TECHNICAL_ARTICLE_DIRECT_GUIDANCE = (
    "\u5148\u5199\u521d\u7a3f\uff0c\u4e0d\u7528\u5927\u7eb2"
)
TECHNICAL_ARTICLE_DIRECT_GUIDANCE_EN = (
    "Write a draft directly without an outline."
)

_EXPLICIT_MARKERS = (
    "\u4ee5\u540e",
    "\u4eca\u540e",
    "from now on",
    "going forward",
    "in future",
)
_PAST_MARKERS = (
    "\u4e0a\u6b21",
    "\u6628\u5929",
    "\u4ee5\u524d",
    "last time",
    "previously",
    "yesterday",
)
_PREFERENCE_MARKERS = (
    "\u559c\u6b22",
    "\u504f\u597d",
    "\u559c\u6b22\u5148",
    "i like",
    "i prefer",
    "prefer",
)
_SEMANTIC_MARKERS = (
    "\u6211\u662f",
    "\u6211\u4e3b\u8981\u505a",
    "i am",
    "i'm",
)
_CURRENT_STATE_MARKERS = (
    "\u8fd9\u6b21",
    "\u5f53\u524d",
    "\u73b0\u5728\u6b63\u5728",
    "this time",
    "currently",
    "right now",
)
_HARD_RULE_MARKERS = (
    "schedule",
    "version",
    "reconcile",
    "result_unknown",
    "hitl",
    "approval",
    "permission",
    "\u5b9a\u65f6",
    "\u7248\u672c",
    "\u91cd\u8bd5",
    "\u6062\u590d",
    "\u5bf9\u8d26",
    "\u5ba1\u6279",
    "\u6743\u9650",
)
_RUNTIME_ID_RE = re.compile(
    r"(?i)\b(?:run|execution|operation|task|objective|resource|draft|"
    r"schedule|approval)[_-]?id\b|\b[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_SUPPORTED_GUIDANCE = frozenset({
    TECHNICAL_ARTICLE_GUIDANCE,
    TECHNICAL_ARTICLE_GUIDANCE_EN,
    TECHNICAL_ARTICLE_DIRECT_GUIDANCE,
    TECHNICAL_ARTICLE_DIRECT_GUIDANCE_EN,
})


class ProceduralCandidate(BaseModel):
    """A short, user-authored, non-executable workflow guideline."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    procedure_key: str
    trigger: str
    guidance: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    source_type: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    observed_at: str

    @field_validator(
        "user_id",
        "tenant_id",
        "procedure_key",
        "trigger",
        "source_type",
        "observed_at",
        mode="before",
    )
    @classmethod
    def _required_text(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if not rendered:
            raise ValueError("procedural candidate requires a non-empty field")
        return rendered

    @field_validator("observed_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("procedural observed_at must be an ISO timestamp") from exc
        return value


class ProceduralAdmissionDecision(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    UNKNOWN = "UNKNOWN"


class ProceduralAdmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ProceduralAdmissionDecision
    reason: str = ""

    @property
    def should_write(self) -> bool:
        return self.decision == ProceduralAdmissionDecision.KEEP

    @property
    def effective_decision(self) -> ProceduralAdmissionDecision:
        return (
            ProceduralAdmissionDecision.DROP
            if self.decision == ProceduralAdmissionDecision.UNKNOWN
            else self.decision
        )


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _ordered_workflow(text: str) -> bool:
    folded = text.casefold()
    chinese_article = "\u6280\u672f\u6587\u7ae0" in text
    english_article = "technical article" in folded or "technical content" in folded
    if not (chinese_article or english_article):
        return False

    outline_positions = [
        position
        for marker in ("\u5927\u7eb2", "outline")
        for position in [folded.find(marker.casefold())]
        if position >= 0
    ]
    body_positions = [
        position
        for marker in ("\u6b63\u6587", "\u521d\u7a3f", "body", "draft")
        for position in [folded.find(marker.casefold())]
        if position >= 0
    ]
    if not outline_positions or not body_positions:
        return False
    outline_position = min(outline_positions)
    body_position = min(position for position in body_positions if position > outline_position) \
        if any(position > outline_position for position in body_positions) else -1
    if body_position < 0:
        return False

    first_markers = ("\u5148", "first", "initially")
    next_markers = ("\u518d", "\u7136\u540e", "then", "after")
    first_position = min(
        (position for marker in first_markers if (position := folded.find(marker.casefold())) >= 0),
        default=-1,
    )
    next_position = min(
        (
            position
            for marker in next_markers
            if (position := folded.find(marker.casefold(), outline_position)) >= 0
        ),
        default=-1,
    )
    return first_position >= 0 and first_position < outline_position and next_position >= 0


def _direct_first_workflow(text: str) -> bool:
    folded = text.casefold()
    chinese_article = "\u6280\u672f\u6587\u7ae0" in text
    english_article = "technical article" in folded or "technical content" in folded
    if not (chinese_article or english_article):
        return False
    chinese_override = (
        ("\u76f4\u63a5" in text and "\u521d\u7a3f" in text)
        and any(
            marker in text
            for marker in (
                "\u4e0d\u7528\u5927\u7eb2",
                "\u4e0d\u7528\u5148\u5217\u5927\u7eb2",
                "\u4e0d\u9700\u8981\u5927\u7eb2",
                "\u4e0d\u9700\u8981\u5148\u5217\u5927\u7eb2",
            )
        )
    )
    english_override = (
        ("write directly" in folded or "directly write" in folded)
        and ("without an outline" in folded or "no outline" in folded)
    )
    return chinese_override or english_override


class ProceduralCandidateBuilder:
    """Build only the single explicit technical-article procedure in V1."""

    def build(
        self,
        user_text: str,
        *,
        user_id: str,
        tenant_id: str,
        observed_at: str | None = None,
        source_id: str | None = None,
    ) -> list[ProceduralCandidate]:
        scope_user = str(user_id or "").strip()
        scope_tenant = str(tenant_id or "").strip()
        text = " ".join(str(user_text or "").split()).strip()
        if not scope_user or not scope_tenant or not text:
            return []
        folded = text.casefold()
        if not _contains_any(text, _EXPLICIT_MARKERS):
            return []
        if _contains_any(text, _PAST_MARKERS + _PREFERENCE_MARKERS + _SEMANTIC_MARKERS):
            return []
        if _contains_any(text, _CURRENT_STATE_MARKERS + _HARD_RULE_MARKERS):
            return []
        ordered_workflow = _ordered_workflow(text)
        direct_first_workflow = _direct_first_workflow(text)
        if _RUNTIME_ID_RE.search(text) or not (ordered_workflow or direct_first_workflow):
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
        english = "technical article" in folded or "technical content" in folded
        if direct_first_workflow:
            guidance = (
                TECHNICAL_ARTICLE_DIRECT_GUIDANCE_EN
                if english
                else TECHNICAL_ARTICLE_DIRECT_GUIDANCE
            )
        else:
            guidance = TECHNICAL_ARTICLE_GUIDANCE_EN if english else TECHNICAL_ARTICLE_GUIDANCE
        return [ProceduralCandidate(
            user_id=scope_user,
            tenant_id=scope_tenant,
            procedure_key=TECHNICAL_ARTICLE_PROCEDURE_KEY,
            trigger=TECHNICAL_ARTICLE_TRIGGER,
            guidance=guidance,
            confidence=0.98,
            source_type=PROCEDURAL_SOURCE_TYPE,
            provenance={
                "procedural_contract": PROCEDURAL_MEMORY_CONTRACT,
                "memory_version": PROCEDURAL_MEMORY_VERSION,
                "source": "explicit_user_instruction",
                "source_type": PROCEDURAL_SOURCE_TYPE,
                "author_role": "user",
                "source_id": source_key,
                "source_hash": source_hash,
            },
            observed_at=timestamp,
        )]


class ProceduralAdmissionPolicy:
    """Conservative policy; unsupported or ambiguous candidates do not write."""

    def evaluate(
        self,
        candidate: ProceduralCandidate | None,
    ) -> ProceduralAdmissionResult:
        if candidate is None:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="no_candidate",
            )
        if candidate.procedure_key != TECHNICAL_ARTICLE_PROCEDURE_KEY:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="unsupported_procedure_key",
            )
        if candidate.trigger != TECHNICAL_ARTICLE_TRIGGER:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="unsupported_trigger",
            )
        if candidate.guidance not in _SUPPORTED_GUIDANCE:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="unsupported_guidance",
            )
        if candidate.source_type != PROCEDURAL_SOURCE_TYPE:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.UNKNOWN,
                reason="source_is_not_explicit_user_instruction",
            )
        provenance = candidate.provenance
        if (
            provenance.get("source") != "explicit_user_instruction"
            or provenance.get("author_role") != "user"
            or provenance.get("procedural_contract") != PROCEDURAL_MEMORY_CONTRACT
        ):
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.UNKNOWN,
                reason="explicit_provenance_incomplete",
            )
        if candidate.confidence < 0.85:
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.UNKNOWN,
                reason="confidence_below_conservative_threshold",
            )
        folded = f"{candidate.trigger} {candidate.guidance}".casefold()
        if _RUNTIME_ID_RE.search(folded):
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="runtime_identity_in_guidance",
            )
        if _contains_any(folded, _HARD_RULE_MARKERS):
            return ProceduralAdmissionResult(
                decision=ProceduralAdmissionDecision.DROP,
                reason="runtime_or_policy_invariant",
            )
        return ProceduralAdmissionResult(
            decision=ProceduralAdmissionDecision.KEEP,
            reason="explicit_reusable_soft_guidance",
        )


class ProceduralMemoryService:
    """Canonical Procedural writer over the existing MemoryManager."""

    def __init__(
        self,
        memory_manager: MemoryManager,
        *,
        builder: ProceduralCandidateBuilder | None = None,
        policy: ProceduralAdmissionPolicy | None = None,
        enabled: bool = True,
    ) -> None:
        self._memory = memory_manager
        self._builder = builder or ProceduralCandidateBuilder()
        self._policy = policy or ProceduralAdmissionPolicy()
        self._enabled = bool(enabled)

    def build_candidates(
        self,
        user_text: str,
        *,
        user_id: str,
        tenant_id: str,
        observed_at: str | None = None,
        source_id: str | None = None,
    ) -> list[ProceduralCandidate]:
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
        candidate: ProceduralCandidate | None,
    ) -> ProceduralAdmissionResult:
        return self._policy.evaluate(candidate)

    def write(self, candidate: ProceduralCandidate | None) -> MemoryRecord | None:
        if not self._enabled or candidate is None:
            return None
        decision = self._policy.evaluate(candidate)
        if not decision.should_write:
            return None
        record = self._record(candidate)
        return self._memory.remember_procedural(
            record,
            procedure_key=candidate.procedure_key,
            trigger=candidate.trigger,
            guidance=candidate.guidance,
        )

    def process_user_instruction(
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
    def _memory_id(candidate: ProceduralCandidate) -> str:
        # The guidance is part of the value identity so a changed explicit
        # rule gets a new row that can be marked ACTIVE while the old value is
        # retained as SUPERSEDED. Replaying the same rule reuses this id.
        identity = "|".join((
            PROCEDURAL_MEMORY_CONTRACT,
            candidate.user_id,
            candidate.tenant_id,
            candidate.procedure_key,
            candidate.trigger,
            candidate.guidance,
        ))
        return f"prov1-{hashlib.sha256(identity.encode()).hexdigest()}"

    def _record(self, candidate: ProceduralCandidate) -> MemoryRecord:
        content = (
            f"\u6280\u672f\u6587\u7ae0\uff1a{candidate.guidance}"
            if any("\u4e00" <= char <= "\u9fff" for char in candidate.guidance)
            else f"Technical article: {candidate.guidance}"
        )
        return MemoryRecord(
            memory_id=self._memory_id(candidate),
            user_id=candidate.user_id,
            tenant_id=candidate.tenant_id,
            status=MemoryStatus.ACTIVE,
            memory_type=MemoryType.PROCEDURAL,
            content=content,
            structured_metadata={
                "memory_contract": PROCEDURAL_MEMORY_CONTRACT,
                "memory_version": PROCEDURAL_MEMORY_VERSION,
                "memory_role": PROCEDURAL_MEMORY_ROLE,
                "procedure_key": candidate.procedure_key,
                "trigger": candidate.trigger,
                "guidance": candidate.guidance,
                "advisory_only": True,
                "observed_at": candidate.observed_at,
                "provenance": dict(candidate.provenance),
            },
            importance=0.55,
            confidence=candidate.confidence,
            source_type=PROCEDURAL_SOURCE_TYPE,
            source_id=str(candidate.provenance.get("source_id") or "") or None,
        )


__all__ = [
    "PROCEDURAL_MEMORY_CONTRACT",
    "PROCEDURAL_MEMORY_ROLE",
    "PROCEDURAL_MEMORY_VERSION",
    "PROCEDURAL_SOURCE_TYPE",
    "TECHNICAL_ARTICLE_GUIDANCE",
    "TECHNICAL_ARTICLE_GUIDANCE_EN",
    "TECHNICAL_ARTICLE_DIRECT_GUIDANCE",
    "TECHNICAL_ARTICLE_DIRECT_GUIDANCE_EN",
    "TECHNICAL_ARTICLE_PROCEDURE_KEY",
    "TECHNICAL_ARTICLE_TRIGGER",
    "ProceduralAdmissionDecision",
    "ProceduralAdmissionPolicy",
    "ProceduralAdmissionResult",
    "ProceduralCandidate",
    "ProceduralCandidateBuilder",
    "ProceduralMemoryService",
]
