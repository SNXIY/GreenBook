"""GreenBook shared contracts."""

from greenbook_contracts.errors import ErrorCode, GreenBookError
from greenbook_contracts.events import BusinessEvent
from greenbook_contracts.external_agent_failure import (
    ExternalAgentFailure,
    FailureNormalizer,
    RecoveryAction,
    SideEffectState,
    normalize_external_failure,
)
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_contract import (
    TOOL_POLICY_CATALOG,
    PermissionPolicy,
    RetryPolicy,
    SideEffectMetadata,
    ToolContract,
    ToolMetadata,
    ToolPolicyMetadata,
    ToolRegistry,
)
from greenbook_contracts.tool_result import ToolResult

__all__ = [
    "ToolResult",
    "ErrorCode",
    "GreenBookError",
    "BusinessEvent",
    "AuthContext",
    "PermissionPolicy",
    "RetryPolicy",
    "SideEffectMetadata",
    "ToolPolicyMetadata",
    "ToolMetadata",
    "ToolRegistry",
    "ToolContract",
    "TOOL_POLICY_CATALOG",
    "ExternalAgentFailure",
    "FailureNormalizer",
    "RecoveryAction",
    "SideEffectState",
    "normalize_external_failure",
]
