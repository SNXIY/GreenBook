import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from evals.moderation.schemas import EvalPolicyReference, EvalPolicySnapshot
from moderation.schemas import (
    ModerationAction,
    ModerationPolicyCreate,
    ModerationPolicyRead,
    PolicySeverity,
    RiskType,
)


class PolicyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    title: str
    description: str
    risk_type: RiskType
    default_action: ModerationAction
    severity: PolicySeverity
    platform: str
    enabled: bool
    priority: int
    applicability_conditions: tuple[str, ...]
    exclusion_conditions: tuple[str, ...]
    violation_examples: tuple[str, ...]
    safe_examples: tuple[str, ...]
    suggested_actions: tuple[ModerationAction, ...]
    tags: tuple[str, ...]
    version: str
    fingerprint_sha256: str

    @classmethod
    def from_policy(
        cls,
        policy: ModerationPolicyCreate | ModerationPolicyRead,
    ) -> "PolicyDefinition":
        content = {
            "code": policy.code,
            "title": policy.title,
            "description": policy.description,
            "risk_type": policy.risk_type,
            "default_action": policy.default_action,
            "severity": policy.severity,
            "platform": policy.platform,
            "enabled": policy.enabled,
            "priority": policy.priority,
            "applicability_conditions": policy.applicability_conditions,
            "exclusion_conditions": policy.exclusion_conditions,
            "violation_examples": policy.violation_examples,
            "safe_examples": policy.safe_examples,
            "suggested_actions": policy.suggested_actions,
            "tags": policy.tags,
        }
        fingerprint = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return cls(
            **content,
            version=str(getattr(policy, "version", 1)),
            fingerprint_sha256=fingerprint,
        )

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"fingerprint_sha256"})


def build_policy_snapshot(policies: Sequence[PolicyDefinition]) -> EvalPolicySnapshot:
    fingerprint = hashlib.sha256(
        "\n".join(f"{policy.code}:{policy.fingerprint_sha256}" for policy in policies).encode(
            "utf-8"
        )
    ).hexdigest()
    return EvalPolicySnapshot(
        snapshot_id=f"policy-set-{fingerprint[:16]}",
        policies=[
            EvalPolicyReference(
                code=policy.code,
                version=policy.version,
                fingerprint_sha256=policy.fingerprint_sha256,
            )
            for policy in policies
        ],
    )
