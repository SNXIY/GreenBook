"""Canonical metadata and contracts for GreenBook tools.

``ToolMetadata`` is the discovery contract and ``ToolPolicyMetadata`` is its
single policy source.  The Agent Runtime, MCP-compatible in-process runtime,
and security gate consume these models; none of them maintain a second risk,
approval, retry, or timeout catalog.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .tool_result import ToolResult


class PermissionPolicy(BaseModel):
    """Authorization scopes attached to a tool policy."""

    model_config = ConfigDict(frozen=True)

    required_scopes: tuple[str, ...] = ()


class RetryPolicy(BaseModel):
    """Bounded retry policy attached to a tool policy."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=1, ge=1)
    retryable_error_codes: tuple[str, ...] = ()
    backoff_seconds: float = Field(default=0.0, ge=0.0)


class SideEffectMetadata(BaseModel):
    """External state-change classification for one tool."""

    model_config = ConfigDict(frozen=True)

    has_side_effect: bool = False
    idempotent: bool = True
    destructive: bool = False
    external_systems: tuple[str, ...] = ()


class ToolPolicyMetadata(BaseModel):
    """Canonical execution policy for one concrete tool."""

    model_config = ConfigDict(frozen=True)

    risk_level: str = "READ"
    requires_approval: bool = False
    permission: PermissionPolicy = Field(default_factory=PermissionPolicy)
    side_effect: SideEffectMetadata = Field(default_factory=SideEffectMetadata)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    cost: float = Field(default=0.0, ge=0.0)
    timeout_seconds: float = Field(default=120.0, gt=0.0)


class ToolMetadata(BaseModel):
    """Stable discovery metadata exposed to Agent intelligence."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    capabilities: tuple[str, ...] = ()
    input_schema: Any
    output_schema: Any
    provider: str = "mcp"
    tags: tuple[str, ...] = ()
    policy: ToolPolicyMetadata = Field(default_factory=ToolPolicyMetadata)


class ToolRegistry:
    """Metadata-only registry; execution remains owned by MCP handlers."""

    def __init__(self, metadata: Any = ()) -> None:
        self._items: dict[str, ToolMetadata] = {}
        for item in metadata:
            self.register(item)

    def register(self, metadata: ToolMetadata) -> ToolMetadata:
        self._items[metadata.name] = metadata
        return metadata

    def get(self, name: str) -> ToolMetadata | None:
        return self._items.get(name)

    def get_required(self, name: str) -> ToolMetadata:
        metadata = self.get(name)
        if metadata is None:
            raise KeyError(name)
        return metadata

    def list(self) -> list[ToolMetadata]:
        return list(self._items.values())

    def as_dict(self) -> dict[str, ToolMetadata]:
        return dict(self._items)


class ToolContract(BaseModel):
    """Canonical contract shared by a capability and its concrete handler."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    category: str
    capability: str
    handler: Any
    input_schema: type[BaseModel]
    output_schema: type[BaseModel] = ToolResult
    operations: tuple[str, ...] = ()
    policy: ToolPolicyMetadata = Field(default_factory=ToolPolicyMetadata)

    @property
    def metadata(self) -> ToolMetadata:
        """Return the execution-neutral discovery projection."""

        return ToolMetadata(
            name=self.name,
            description=self.description,
            capabilities=(self.capability,),
            input_schema=self.input_schema.model_json_schema(),
            output_schema=self.output_schema.model_json_schema(),
            provider=self.category,
            policy=self.policy,
        )

    @property
    def input_model(self) -> type[BaseModel]:
        return self.input_schema

    @property
    def output_model(self) -> type[BaseModel]:
        return self.output_schema


_TRANSIENT_ERRORS = (
    "TIMEOUT",
    "NETWORK_ERROR",
    "RATE_LIMIT",
    "TEMPORARY_UNAVAILABLE",
    "CREATOR_UNAVAILABLE",
    "JAVA_BACKEND_UNAVAILABLE",
)


def _policy(
    risk_level: str,
    *,
    requires_approval: bool = False,
    has_side_effect: bool = False,
    idempotent: bool = True,
    destructive: bool = False,
    max_attempts: int = 1,
    external_systems: tuple[str, ...] = (),
    timeout_seconds: float = 120.0,
) -> ToolPolicyMetadata:
    return ToolPolicyMetadata(
        risk_level=risk_level,
        requires_approval=requires_approval,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            retryable_error_codes=_TRANSIENT_ERRORS,
            backoff_seconds=0.25 if max_attempts > 1 else 0.0,
        ),
        side_effect=SideEffectMetadata(
            has_side_effect=has_side_effect,
            idempotent=idempotent,
            destructive=destructive,
            external_systems=external_systems,
        ),
        timeout_seconds=timeout_seconds,
    )


# The only concrete tool-policy catalog in the repository.  MCP registers
# handlers against this mapping and the security gate reads the same objects.
TOOL_POLICY_CATALOG: dict[str, ToolPolicyMetadata] = {
    "community.search_public_posts": _policy("READ"),
    "community.get_post": _policy("READ"),
    "community.list_own_posts": _policy("READ"),
    "content.create_draft": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("creator", "java"),
        timeout_seconds=600.0,
    ),
    "content.build_strategy": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("creator",),
        timeout_seconds=600.0,
    ),
    "content.get_draft": _policy("READ"),
    "content.list_drafts": _policy("READ"),
    "content.revise_draft": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("creator", "java"),
        timeout_seconds=600.0,
    ),
    "publication.schedule": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("java",),
    ),
    "publication.get_status": _policy("READ"),
    "publication.update_schedule": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("java",),
    ),
    "publication.cancel_schedule": _policy(
        "IDEMPOTENT_WRITE",
        has_side_effect=True,
        max_attempts=2,
        external_systems=("java",),
    ),
    "publication.publish_now": _policy(
        "DESTRUCTIVE_WRITE",
        requires_approval=True,
        has_side_effect=True,
        idempotent=False,
        destructive=True,
        external_systems=("java",),
    ),
    "interaction.list_comments": _policy("READ"),
    "interaction.send_reply": _policy(
        "DESTRUCTIVE_WRITE",
        requires_approval=True,
        has_side_effect=True,
        idempotent=False,
        destructive=True,
        external_systems=("java",),
    ),
    "analytics.get_post_performance": _policy("READ"),
    "analytics.get_account_summary": _policy("READ"),
}


__all__ = [
    "PermissionPolicy",
    "RetryPolicy",
    "SideEffectMetadata",
    "ToolPolicyMetadata",
    "ToolMetadata",
    "ToolRegistry",
    "ToolContract",
    "TOOL_POLICY_CATALOG",
]
