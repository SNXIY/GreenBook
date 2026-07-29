"""Pre-graph L0/L1 cascade gate.

L0 uses deterministic detectors (PII / contact / hard abuse patterns).
L1 optionally calls the OpenAI Moderations API as a cheap first pass.
Ambiguous traffic continues into the existing LangGraph Agent path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from moderation.schemas import (
    ModerationAction,
    ModerationContentType,
    ModerationSignalEvidence,
    ModerationSignalType,
    RiskClassification,
    RiskType,
    SignalSource,
)
from moderation.services.evidence_detection import EvidenceDetectionService

logger = logging.getLogger(__name__)

PreflightDisposition = Literal["enforce", "continue"]
PreflightLayer = Literal["L0", "L1"]

_ABUSE_HARD = re.compile(
    r"(?i)(?:"
    r"去死|死全家|操你|傻逼|nmsl|"
    r"\bkys\b|kill\s+yourself|go\s+die"
    r")"
)
_AD_HARD = re.compile(
    r"(?i)(?:"
    r"(?:加我|私聊|走[\s._-]*私).{0,12}(?:vx|wx|微信|薇信|qq|扣扣)|"
    r"(?:vx|wx|微信|薇信|qq|扣扣).{0,12}(?:加我|私聊|优惠|代理|刷单)|"
    r"(?:日入|稳赚|免费领取).{0,8}(?:加|私|vx|wx)"
    r")"
)

_L1_ABUSE_CATEGORIES = (
    "hate",
    "hate/threatening",
    "harassment",
    "harassment/threatening",
    "violence",
    "violence/graphic",
)
_L1_SKIP_AUTO_CATEGORIES = (
    "self-harm",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual/minors",
)


@dataclass(frozen=True, slots=True)
class PreflightConfig:
    l0_enabled: bool = True
    l1_enabled: bool = False
    l1_api_key: str | None = None
    l1_base_url: str = "https://api.openai.com/v1"
    l1_timeout_seconds: float = 5.0
    l1_enforce_score_min: float = 0.90
    l1_clear_safe_score_max: float = 0.05


@dataclass(frozen=True, slots=True)
class PreflightResult:
    disposition: PreflightDisposition
    layer: PreflightLayer | None = None
    classification: RiskClassification | None = None
    recommended_action: ModerationAction | None = None
    reason: str = ""
    reasons: list[str] = field(default_factory=list)
    signals: list[ModerationSignalEvidence] = field(default_factory=list)
    source_evidence: list[str] = field(default_factory=list)


class ModerationPreflightService:
    def __init__(
        self,
        config: PreflightConfig,
        detector: EvidenceDetectionService | None = None,
    ) -> None:
        self.config = config
        self.detector = detector or EvidenceDetectionService()

    async def evaluate(
        self,
        *,
        content: str,
        content_type: ModerationContentType,
        metadata: dict[str, Any] | None = None,
        existing_signals: list[ModerationSignalEvidence] | None = None,
    ) -> PreflightResult:
        metadata = metadata or {}
        signals = list(existing_signals or [])
        must_continue = _requires_agent_path(content_type, metadata)

        if self.config.l0_enabled:
            l0 = self._evaluate_l0(content)
            signals = _merge_signals(signals, l0.signals)
            if l0.disposition == "enforce" and not must_continue:
                return PreflightResult(
                    disposition="enforce",
                    layer="L0",
                    classification=l0.classification,
                    recommended_action=l0.recommended_action,
                    reason=l0.reason,
                    reasons=l0.reasons,
                    signals=signals,
                    source_evidence=l0.source_evidence,
                )

        if self.config.l1_enabled and self.config.l1_api_key:
            l1 = await self._evaluate_l1(content)
            signals = _merge_signals(signals, l1.signals)
            # Comments/reports always continue into the Agent path.
            if l1.disposition == "enforce" and not must_continue:
                return PreflightResult(
                    disposition="enforce",
                    layer="L1",
                    classification=l1.classification,
                    recommended_action=l1.recommended_action,
                    reason=l1.reason,
                    reasons=l1.reasons,
                    signals=signals,
                    source_evidence=l1.source_evidence,
                )
            return PreflightResult(
                disposition="continue",
                layer="L1" if l1.reasons else None,
                reasons=l1.reasons,
                signals=signals,
                source_evidence=l1.source_evidence,
            )

        return PreflightResult(disposition="continue", signals=signals)

    def _evaluate_l0(self, content: str) -> PreflightResult:
        signals: list[ModerationSignalEvidence] = []
        contacts = self.detector.detect_contact_information(content)
        obfuscation = self.detector.explain_obfuscated_expression(content)

        identity_hits = [
            finding
            for finding in contacts.findings
            if finding.kind == "IDENTITY_NUMBER" and finding.confidence >= 0.95
        ]
        phone_hits = [
            finding
            for finding in contacts.findings
            if finding.kind == "PHONE" and finding.confidence >= 0.95
        ]
        contact_hints = [
            finding
            for finding in contacts.findings
            if finding.kind in {"WECHAT_HINT", "QQ_HINT", "URL", "EMAIL"}
        ]
        abuse_obfuscations = [
            match
            for match in obfuscation.matches
            if match.category == "ABUSE" and match.confidence >= 0.9
        ]
        ad_obfuscations = [
            match
            for match in obfuscation.matches
            if match.category in {"ADVERTISING", "CONTACT"} and match.confidence >= 0.8
        ]

        if identity_hits or phone_hits or contact_hints or abuse_obfuscations or ad_obfuscations:
            indicators = []
            if identity_hits:
                indicators.append("identity_number")
            if phone_hits:
                indicators.append("phone")
            if contact_hints:
                indicators.append("contact_channel")
            if abuse_obfuscations:
                indicators.append("abuse_obfuscation")
            if ad_obfuscations:
                indicators.append("advertising_obfuscation")
            signals.append(
                ModerationSignalEvidence(
                    signal_type=ModerationSignalType.TEXT_PATTERN,
                    source=SignalSource.CONTENT,
                    score=0.85,
                    details={"layer": "L0", "indicators": indicators[:10]},
                )
            )

        if identity_hits:
            classification = RiskClassification(
                risk_type=RiskType.PRIVACY,
                risk_score=0.95,
                confidence=0.95,
                indicators=["identity_number"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L0",
                classification=classification,
                recommended_action=ModerationAction.LIMIT,
                reason=(
                    "L0 deterministic gate: the content exposes an identity number "
                    "and is limited without requiring the Agent path."
                ),
                reasons=["L0_PRIVACY_IDENTITY"],
                signals=signals,
                source_evidence=["l0:identity_number"],
            )

        if _ABUSE_HARD.search(content) or abuse_obfuscations:
            classification = RiskClassification(
                risk_type=RiskType.ABUSE,
                risk_score=0.92,
                confidence=0.9,
                indicators=["hard_abuse_pattern"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L0",
                classification=classification,
                recommended_action=ModerationAction.REJECT,
                reason=(
                    "L0 deterministic gate: a high-confidence abuse pattern was matched "
                    "and the item is rejected without the Agent path."
                ),
                reasons=["L0_ABUSE_HARD"],
                signals=signals,
                source_evidence=["l0:abuse_pattern"],
            )

        if _AD_HARD.search(content) or (
            contact_hints and ad_obfuscations and (phone_hits or contact_hints)
        ):
            classification = RiskClassification(
                risk_type=RiskType.ADVERTISING,
                risk_score=0.9,
                confidence=0.88,
                indicators=["contact_advertising"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L0",
                classification=classification,
                recommended_action=ModerationAction.REJECT,
                reason=(
                    "L0 deterministic gate: contact solicitation / advertising patterns "
                    "were matched and the item is rejected without the Agent path."
                ),
                reasons=["L0_ADVERTISING_HARD"],
                signals=signals,
                source_evidence=["l0:advertising_pattern"],
            )

        if phone_hits and contact_hints:
            classification = RiskClassification(
                risk_type=RiskType.PRIVACY,
                risk_score=0.88,
                confidence=0.85,
                indicators=["phone", "contact_channel"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L0",
                classification=classification,
                recommended_action=ModerationAction.LIMIT,
                reason=(
                    "L0 deterministic gate: a phone number plus contact channel was "
                    "detected and the item is limited without the Agent path."
                ),
                reasons=["L0_PRIVACY_PHONE_CONTACT"],
                signals=signals,
                source_evidence=["l0:phone_contact"],
            )

        return PreflightResult(disposition="continue", signals=signals)

    async def _evaluate_l1(self, content: str) -> PreflightResult:
        try:
            payload = await self._call_openai_moderation(content)
        except Exception as exc:  # noqa: BLE001 - preflight must never block the graph
            logger.warning("L1 moderation preflight failed: %s", exc)
            return PreflightResult(
                disposition="continue",
                reasons=["L1_UNAVAILABLE"],
            )

        results = payload.get("results") or []
        if not results:
            return PreflightResult(disposition="continue", reasons=["L1_EMPTY_RESULT"])

        result = results[0]
        categories = result.get("categories") or {}
        scores = result.get("category_scores") or {}
        flagged = bool(result.get("flagged"))
        max_score = max((float(value) for value in scores.values()), default=0.0)
        top_categories = sorted(
            ((name, float(score)) for name, score in scores.items()),
            key=lambda item: item[1],
            reverse=True,
        )[:5]

        signals: list[ModerationSignalEvidence] = []
        if flagged or max_score >= self.config.l1_clear_safe_score_max:
            signals.append(
                ModerationSignalEvidence(
                    signal_type=ModerationSignalType.TEXT_PATTERN,
                    source=SignalSource.CONTENT,
                    score=min(1.0, max_score),
                    details={
                        "layer": "L1",
                        "flagged": flagged,
                        "top_categories": [
                            {"name": name, "score": score} for name, score in top_categories
                        ],
                    },
                )
            )

        if any(categories.get(name) for name in _L1_SKIP_AUTO_CATEGORIES):
            return PreflightResult(
                disposition="continue",
                reasons=["L1_SENSITIVE_CATEGORY_REQUIRES_AGENT"],
                signals=signals,
                source_evidence=["l1:openai_moderation"],
            )

        abuse_score = max(
            (float(scores.get(name, 0.0)) for name in _L1_ABUSE_CATEGORIES),
            default=0.0,
        )
        if flagged and abuse_score >= self.config.l1_enforce_score_min:
            classification = RiskClassification(
                risk_type=RiskType.ABUSE,
                risk_score=min(1.0, abuse_score),
                confidence=min(1.0, abuse_score),
                indicators=["openai_moderation"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L1",
                classification=classification,
                recommended_action=ModerationAction.REJECT,
                reason=(
                    "L1 OpenAI Moderation gate: high-confidence abuse categories were "
                    "flagged and the item is rejected before the Agent path."
                ),
                reasons=["L1_ABUSE_ENFORCE"],
                signals=signals,
                source_evidence=["l1:openai_moderation"],
            )

        if (
            not flagged
            and max_score <= self.config.l1_clear_safe_score_max
        ):
            classification = RiskClassification(
                risk_type=RiskType.NORMAL,
                risk_score=max_score,
                confidence=0.95,
                indicators=["openai_moderation_clear"],
            )
            return PreflightResult(
                disposition="enforce",
                layer="L1",
                classification=classification,
                recommended_action=ModerationAction.PASS,
                reason=(
                    "L1 OpenAI Moderation gate: no categories were flagged and all "
                    "scores stayed below the clear-safe threshold."
                ),
                reasons=["L1_CLEAR_SAFE"],
                signals=[],
                source_evidence=["l1:openai_moderation"],
            )

        return PreflightResult(
            disposition="continue",
            reasons=["L1_AMBIGUOUS"],
            signals=signals,
            source_evidence=["l1:openai_moderation"],
        )

    async def _call_openai_moderation(self, content: str) -> dict[str, Any]:
        base = self.config.l1_base_url.rstrip("/")
        url = f"{base}/moderations"
        headers = {
            "Authorization": f"Bearer {self.config.l1_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.config.l1_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json={"input": content})
            response.raise_for_status()
            return response.json()


def _requires_agent_path(
    content_type: ModerationContentType,
    metadata: dict[str, Any],
) -> bool:
    if content_type == ModerationContentType.COMMENT:
        return True
    if metadata.get("review_trigger") == "REPORT":
        return True
    return False


def _merge_signals(
    existing: list[ModerationSignalEvidence],
    extra: list[ModerationSignalEvidence],
) -> list[ModerationSignalEvidence]:
    merged = list(existing)
    seen = {
        (
            signal.signal_type,
            signal.source,
            round(signal.score, 4),
            repr(signal.details or {}),
        )
        for signal in existing
    }
    for signal in extra:
        signature = (
            signal.signal_type,
            signal.source,
            round(signal.score, 4),
            repr(signal.details or {}),
        )
        if signature in seen:
            continue
        seen.add(signature)
        merged.append(signal)
    return merged
