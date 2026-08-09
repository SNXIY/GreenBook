from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Any

from app.tools import RiskLevel, ToolDefinition, ToolRegistry


class PolicyDecisionType(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    ALLOW_WITH_LIMIT = "ALLOW_WITH_LIMIT"


@dataclass(frozen=True)
class PolicyContext:
    run_id: str
    user_id: str
    tenant_id: str
    principal_role: str
    action: str
    resource: dict[str, Any]
    approval_granted: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyDecisionType
    reason: str
    policy_version: str
    limits: dict[str, int]

    @property
    def allowed(self) -> bool:
        return self.decision in {
            PolicyDecisionType.ALLOW,
            PolicyDecisionType.ALLOW_WITH_LIMIT,
        }


class CommunityPolicyEngine:
    """Deterministic, deny-by-default community capability policy."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.version = str(payload.get("version") or "").strip()
        if not self.version:
            raise ValueError("Policy manifest requires a version")
        if payload.get("default_decision") != "DENY":
            raise ValueError("Community policy must be deny-by-default")
        guards = payload.get("hard_guards")
        roles = payload.get("roles")
        if not isinstance(guards, dict) or not isinstance(roles, dict):
            raise ValueError("Policy manifest requires hard_guards and roles")
        required_guards = {
            "registered_tools_only": True,
            "direct_database_access": False,
            "internet_access": False,
            "java_authority_for_community_writes": True,
            "publish_state_machine_bypass": False,
        }
        if any(guards.get(key) != value for key, value in required_guards.items()):
            raise ValueError("Community hard guards cannot be weakened")

    @classmethod
    def from_manifest(cls, path: str | Path) -> "CommunityPolicyEngine":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Policy manifest must be a JSON object")
        return cls(payload)

    def evaluate(
        self,
        *,
        context: PolicyContext,
        definition: ToolDefinition,
        registry: ToolRegistry,
    ) -> PolicyDecision:
        try:
            registered = registry.get(context.action)
        except ValueError:
            return self._deny("工具未注册，调用被拒绝")
        if registered != definition:
            return self._deny("工具契约与注册表不一致")

        role_policy = dict(
            self._payload["roles"].get(context.principal_role.upper()) or {}
        )
        allowed = tuple(str(value) for value in role_policy.get("allow") or [])
        if context.action.startswith("mcp."):
            allowed = tuple(
                str(value)
                for value in self._payload.get("allowed_mcp_namespaces") or []
            )
        if not any(_matches(pattern, context.action) for pattern in allowed):
            return self._deny(
                f"角色 {context.principal_role} 未被授予 {context.action}"
            )

        if context.action.startswith("community.") and definition.side_effecting:
            if context.resource.get("authority") != "JAVA":
                return self._deny("社区写操作必须由 Java 业务服务执行")
        if context.action.startswith("publication."):
            if context.resource.get("authority") != "JAVA":
                return self._deny("发布必须经过 Java 发布状态机")
        if context.action.startswith("mcp.") and context.resource.get("open_world"):
            return self._deny("社区助手禁止开放互联网或社区外部系统访问")

        approvals = tuple(
            str(value) for value in role_policy.get("require_approval") or []
        )
        requires_approval = (
            definition.risk == RiskLevel.EXTERNAL_WRITE
            or any(_matches(pattern, context.action) for pattern in approvals)
        )
        if requires_approval and not context.approval_granted:
            return PolicyDecision(
                PolicyDecisionType.REQUIRE_APPROVAL,
                "高风险或外部写入需要当前任务的人工确认",
                self.version,
                {},
            )
        return PolicyDecision(
            PolicyDecisionType.ALLOW,
            "已通过社区能力、角色、资源和风险策略",
            self.version,
            {},
        )

    def signature(self) -> str:
        encoded = json.dumps(
            self._payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def public_summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "default_decision": "DENY",
            "hard_guards": dict(self._payload["hard_guards"]),
            "roles": {
                role: {
                    "allow": list(config.get("allow") or []),
                    "require_approval": list(
                        config.get("require_approval") or []
                    ),
                }
                for role, config in self._payload["roles"].items()
            },
            "allowed_mcp_namespaces": list(
                self._payload.get("allowed_mcp_namespaces") or []
            ),
        }

    def _deny(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            PolicyDecisionType.DENY,
            reason,
            self.version,
            {},
        )


def _matches(pattern: str, action: str) -> bool:
    if pattern.endswith(".*"):
        return action.startswith(pattern[:-1])
    return action == pattern


community_policy = CommunityPolicyEngine.from_manifest(
    Path(__file__).with_name("community_policy.json")
)
