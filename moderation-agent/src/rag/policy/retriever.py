import re
from collections.abc import Sequence
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from moderation.schemas import ModerationAction, PolicyEvidence, RiskType


class PolicyRetriever(Protocol):
    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 5,
    ) -> list[PolicyEvidence]: ...


class InMemoryPolicyRetriever:
    """Small deterministic fallback used before an external index is configured."""

    _policies = (
        {
            "code": "NORMAL-001",
            "title": "Allowed ordinary content",
            "description": "Ordinary conversation without advertising, abuse, or private data is allowed.",
            "risk_type": RiskType.NORMAL,
            "default_action": ModerationAction.PASS,
            "keywords": (),
        },
        {
            "code": "ADV-001",
            "title": "Unsolicited advertising",
            "description": "Unsolicited promotions, repetitive sales messages, referral links, and requests to move transactions off platform are prohibited.",
            "risk_type": RiskType.ADVERTISING,
            "default_action": ModerationAction.REJECT,
            "keywords": ("advert", "promotion", "discount", "buy", "sale", "wechat", "telegram"),
        },
        {
            "code": "ABUSE-001",
            "title": "Abusive or threatening language",
            "description": "Targeted insults, harassment, degrading language, and credible threats against a person are prohibited.",
            "risk_type": RiskType.ABUSE,
            "default_action": ModerationAction.REJECT,
            "keywords": ("idiot", "stupid", "hate", "kill", "threat", "harass"),
        },
        {
            "code": "PRIVACY-001",
            "title": "Personal and private information",
            "description": "Do not expose phone numbers, identity numbers, precise addresses, credentials, or other private personal data without authorization.",
            "risk_type": RiskType.PRIVACY,
            "default_action": ModerationAction.REJECT,
            "keywords": ("phone", "address", "password", "identity", "credential", "email"),
        },
    )

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 5,
    ) -> list[PolicyEvidence]:
        del platform
        query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        matches: list[PolicyEvidence] = []
        for policy in self._policies:
            if policy["risk_type"] not in risk_types:
                continue
            keyword_hits = len(query_terms.intersection(policy["keywords"]))
            score = min(1.0, 0.75 + keyword_hits * 0.05)
            matches.append(
                PolicyEvidence(
                    policy_id=uuid5(NAMESPACE_URL, f"moderation-policy:{policy['code']}"),
                    code=policy["code"],
                    title=policy["title"],
                    excerpt=policy["description"],
                    score=score,
                    risk_type=policy["risk_type"],
                    default_action=policy["default_action"],
                    version=1,
                )
            )
        return matches[:limit]


class DelegatingPolicyRetriever:
    def __init__(self, backend: PolicyRetriever | None = None) -> None:
        self._backend = backend or InMemoryPolicyRetriever()

    def configure(self, backend: PolicyRetriever) -> None:
        self._backend = backend

    def reset(self) -> None:
        self._backend = InMemoryPolicyRetriever()

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 5,
    ) -> list[PolicyEvidence]:
        return await self._backend.search(
            query=query,
            platform=platform,
            risk_types=risk_types,
            limit=limit,
        )


default_policy_retriever = DelegatingPolicyRetriever()
