"""Security projection over the canonical ToolMetadata policy catalog."""

from __future__ import annotations

from enum import StrEnum

from greenbook_contracts.tool_contract import TOOL_POLICY_CATALOG


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SecurityPolicy:
    """Small injectable facade used by the Runtime composition root."""

    def risk_level(self, tool_name: str) -> RiskLevel:
        return tool_risk_level(tool_name)

    def requires_approval(self, tool_name: str) -> bool:
        return requires_approval(tool_name)


def tool_risk_level(tool_name: str) -> RiskLevel:
    """Project canonical contract risk into the security enum."""

    policy = TOOL_POLICY_CATALOG.get(tool_name)
    if policy is None:
        return RiskLevel.HIGH
    return {
        "READ": RiskLevel.LOW,
        "IDEMPOTENT_WRITE": RiskLevel.MEDIUM,
        "DESTRUCTIVE_WRITE": RiskLevel.HIGH,
    }.get(policy.risk_level, RiskLevel.HIGH)


def requires_approval(tool_name: str) -> bool:
    policy = TOOL_POLICY_CATALOG.get(tool_name)
    # Unknown tools remain fail-closed until their canonical contract is
    # registered with an explicit policy.
    return True if policy is None else policy.requires_approval
