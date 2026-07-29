import logging

from database import DatabaseManager
from moderation.repositories import ModerationPolicyRepository
from moderation.schemas import (
    ModerationPolicyCreate,
    ModerationPolicyRead,
    PolicySeverity,
    RiskType,
)
from moderation.services.mappers import policy_to_read
from moderation.services.ports import KnowledgeIndex, NoopKnowledgeIndex

logger = logging.getLogger(__name__)

DEFAULT_POLICIES = (
    ModerationPolicyCreate(
        code="NORMAL-001",
        title="Allowed ordinary content",
        description="Ordinary conversation without advertising, abuse, or private data is allowed.",
        risk_type=RiskType.NORMAL,
        default_action="PASS",
        applicability_conditions=["No advertising, abuse, or private-data exposure is present."],
        exclusion_conditions=["A more specific risk policy applies to the current content."],
        safe_examples=["Technical notes and ordinary community discussion."],
        severity=PolicySeverity.LOW,
        tags=["ordinary-content", "safe"],
        priority=100,
    ),
    ModerationPolicyCreate(
        code="ADV-001",
        title="Unsolicited advertising",
        description="Unsolicited promotions, repetitive sales messages, referral links, and off-platform transactions are prohibited.",
        risk_type=RiskType.ADVERTISING,
        default_action="REJECT",
        applicability_conditions=[
            "The content promotes a product, service, referral, or off-platform transaction."
        ],
        exclusion_conditions=["A contact reference has no promotional or transactional purpose."],
        violation_examples=["Add an off-platform account to receive sales material."],
        safe_examples=["A non-promotional discussion that mentions a communication platform."],
        severity=PolicySeverity.HIGH,
        tags=["promotion", "off-platform", "spam"],
        priority=100,
    ),
    ModerationPolicyCreate(
        code="ABUSE-001",
        title="Abusive or threatening language",
        description="Targeted insults, harassment, degrading language, and credible threats are prohibited.",
        risk_type=RiskType.ABUSE,
        default_action="REJECT",
        applicability_conditions=[
            "The language targets a person or group with abuse or harassment."
        ],
        exclusion_conditions=[
            "The phrase is a non-targeted quotation or benign contextual reference."
        ],
        violation_examples=["A targeted degrading insult or credible threat."],
        safe_examples=["Criticism of an idea without attacking a person."],
        severity=PolicySeverity.HIGH,
        tags=["harassment", "targeted-insult", "threat"],
        priority=100,
    ),
    ModerationPolicyCreate(
        code="PRIVACY-001",
        title="Personal and private information",
        description="Phone numbers, identity numbers, precise addresses, credentials, and other private personal data must not be exposed without authorization.",
        risk_type=RiskType.PRIVACY,
        default_action="REJECT",
        applicability_conditions=[
            "The content exposes personal data without adequate authorization or necessity."
        ],
        exclusion_conditions=["The information is non-sensitive and legitimately self-published."],
        violation_examples=["Publishing another person's phone or identity number."],
        safe_examples=["A redacted or synthetic contact-information example."],
        severity=PolicySeverity.CRITICAL,
        tags=["personal-data", "contact-information", "identity"],
        priority=100,
    ),
)


class ModerationPolicyService:
    def __init__(
        self,
        database: DatabaseManager,
        knowledge_index: KnowledgeIndex | None = None,
    ) -> None:
        self.database = database
        self.repository = ModerationPolicyRepository()
        self.knowledge_index = knowledge_index or NoopKnowledgeIndex()

    async def create(self, request: ModerationPolicyCreate) -> ModerationPolicyRead:
        async with self.database.session() as session:
            policy = await self.repository.create(session, request)
            await session.commit()
            result = policy_to_read(policy)
        try:
            await self.knowledge_index.index_policy(policy)
        except Exception:
            logger.exception(
                "Policy %s was saved but could not be added to the vector index",
                policy.id,
            )
        return result

    async def list(
        self,
        *,
        platform: str | None = None,
        risk_type: RiskType | None = None,
        enabled_only: bool = False,
    ) -> list[ModerationPolicyRead]:
        async with self.database.session() as session:
            policies = await self.repository.list(
                session,
                platform=platform,
                risk_types=[risk_type] if risk_type else None,
                enabled_only=enabled_only,
            )
            return [policy_to_read(policy) for policy in policies]

    async def ensure_defaults(self) -> None:
        for request in DEFAULT_POLICIES:
            async with self.database.session() as session:
                existing = await self.repository.find_by_code(
                    session,
                    platform=request.platform,
                    code=request.code,
                )
                if existing is not None:
                    continue
                policy = await self.repository.create(session, request)
                await session.commit()
            try:
                await self.knowledge_index.index_policy(policy)
            except Exception:
                logger.exception(
                    "Default policy %s was saved but could not be added to the vector index",
                    policy.id,
                )
