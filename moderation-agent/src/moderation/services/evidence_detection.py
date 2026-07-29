import re
from dataclasses import dataclass
from re import Pattern
from typing import Literal

from moderation.schemas import (
    ContactDetectionData,
    ContactFinding,
    ObfuscatedExpressionData,
    ObfuscatedExpressionMatch,
)
from moderation.security import find_sensitive_text, redact_text


@dataclass(frozen=True, slots=True)
class _ObfuscationRule:
    pattern: Pattern[str]
    normalized_form: str
    category: Literal["CONTACT", "ADVERTISING", "ABUSE", "UNKNOWN"]
    explanation: str
    confidence: float


_WECHAT_HINT = re.compile(
    r"(?i)(?:\b(?:vx|wx)\b|v[\s._-]*信|微[\s._-]*信|薇[\s._-]*信|威[\s._-]*信|加[\s+＋]*v)"
)
_QQ_HINT = re.compile(r"(?i)(?:\bqq\b|扣扣|企鹅号)")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]{3,500}")
_OBFUSCATION_RULES = (
    _ObfuscationRule(
        pattern=_WECHAT_HINT,
        normalized_form="微信/WeChat",
        category="CONTACT",
        explanation="该表达是常见的微信联系方式或导流变体。",
        confidence=0.95,
    ),
    _ObfuscationRule(
        pattern=_QQ_HINT,
        normalized_form="QQ",
        category="CONTACT",
        explanation="该表达是常见的 QQ 联系方式变体。",
        confidence=0.95,
    ),
    _ObfuscationRule(
        pattern=re.compile(r"(?i)(?:私[\s._-]*聊|私[\s._-]*我|走[\s._-]*私)"),
        normalized_form="转入私聊",
        category="ADVERTISING",
        explanation="该表达可能表示将交流或交易转移到私聊渠道。",
        confidence=0.8,
    ),
)


class EvidenceDetectionService:
    def detect_contact_information(self, content: str) -> ContactDetectionData:
        findings: list[ContactFinding] = []
        for sensitive_match in find_sensitive_text(content):
            findings.append(
                ContactFinding(
                    kind=sensitive_match.kind,
                    masked_value=redact_text(sensitive_match.value),
                    start=sensitive_match.start,
                    end=sensitive_match.end,
                    confidence=1.0,
                )
            )

        for kind, pattern, confidence in (
            ("WECHAT_HINT", _WECHAT_HINT, 0.9),
            ("QQ_HINT", _QQ_HINT, 0.9),
            ("URL", _URL, 0.95),
        ):
            for regex_match in pattern.finditer(content):
                findings.append(
                    ContactFinding(
                        kind=kind,
                        masked_value=redact_text(regex_match.group(0))[:500],
                        start=regex_match.start(),
                        end=regex_match.end(),
                        confidence=confidence,
                    )
                )

        unique = {(finding.kind, finding.start, finding.end): finding for finding in findings}
        ordered = sorted(unique.values(), key=lambda item: (item.start, item.end, item.kind))
        return ContactDetectionData(
            has_contact_information=bool(ordered),
            findings=ordered[:50],
        )

    def explain_obfuscated_expression(
        self,
        expression: str,
        context: str | None = None,
    ) -> ObfuscatedExpressionData:
        source = expression if not context else f"{expression}\n{context}"
        matches: list[ObfuscatedExpressionMatch] = []
        seen: set[tuple[str, str]] = set()
        for rule in _OBFUSCATION_RULES:
            for match in rule.pattern.finditer(source):
                matched_text = redact_text(match.group(0))[:200]
                signature = (matched_text.casefold(), rule.normalized_form)
                if signature in seen:
                    continue
                seen.add(signature)
                matches.append(
                    ObfuscatedExpressionMatch(
                        matched_text=matched_text,
                        normalized_form=rule.normalized_form,
                        category=rule.category,
                        explanation=rule.explanation,
                        confidence=rule.confidence,
                    )
                )
        return ObfuscatedExpressionData(
            matches=matches[:20],
            context_used=bool(context),
        )
