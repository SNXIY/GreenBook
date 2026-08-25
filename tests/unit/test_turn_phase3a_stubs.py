"""Explicit test stubs for Phase 3A Turn tests.

These are test-only stubs.  They never fake a real Java/DB/LLM verification
result; they only provide the boundary shapes the Fast Path hands to durable
execution.
"""

from __future__ import annotations

from greenbook_contracts.tool_contract import (
    SemanticAction,
    SideEffectMetadata,
    ToolMetadata,
    ToolPolicyMetadata,
)


def _target_candidate(*, task_id: str, kind: str = "TASK", resource_id: str | None = None):
    return {
        "kind": kind,
        "id": task_id,
        "task_id": task_id,
        "resource_id": resource_id or task_id,
    }


def _tool(
    name: str,
    *,
    semantic_action: SemanticAction | None,
    capabilities: tuple[str, ...],
    requires_approval: bool = False,
    destructive: bool = False,
    has_side_effect: bool = False,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description=f"test stub for {name}",
        capabilities=capabilities,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        provider="test",
        semantic_action=semantic_action,
        policy=ToolPolicyMetadata(
            requires_approval=requires_approval,
            side_effect=SideEffectMetadata(
                has_side_effect=has_side_effect,
                destructive=destructive,
            ),
        ),
    )


class StubTool:
    @staticmethod
    def update_draft() -> ToolMetadata:
        return _tool(
            "content.update_draft",
            semantic_action=SemanticAction.UPDATE_DRAFT,
            capabilities=("MANAGE_DRAFT",),
            has_side_effect=True,
        )

    @staticmethod
    def list_drafts() -> ToolMetadata:
        return _tool(
            "content.list_drafts",
            semantic_action=SemanticAction.LIST_DRAFTS,
            capabilities=("LIST_DRAFTS",),
        )

    @staticmethod
    def publish_now() -> ToolMetadata:
        return _tool(
            "publication.publish_now",
            semantic_action=SemanticAction.PUBLISH_NOW,
            capabilities=("PUBLISH_NOW",),
            has_side_effect=True,
            requires_approval=True,
        )
