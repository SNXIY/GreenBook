"""Control-plane invariants for bounded multi-turn operations."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain import AgentPlan, IntentDelta


class OperationPlanViolation(RuntimeError):
    """A proposed plan contradicts the authoritative IntentDelta."""


@dataclass(frozen=True)
class OperationContract:
    allowed_tools: frozenset[str]
    required_tools: frozenset[str]
    required_any_tools: frozenset[str] = frozenset()


_CONTRACTS = {
    "QUERY_SCHEDULE": OperationContract(
        allowed_tools=frozenset({"publication.get_schedule"}),
        required_tools=frozenset({"publication.get_schedule"}),
    ),
    "QUERY_CONTENT": OperationContract(
        allowed_tools=frozenset({"community.get_own_draft"}),
        required_tools=frozenset({"community.get_own_draft"}),
    ),
    "QUERY_PUBLICATION_STATUS": OperationContract(
        allowed_tools=frozenset(
            {
                "community.get_post",
                "community.get_own_draft",
                "publication.get_schedule",
            }
        ),
        required_tools=frozenset(),
        required_any_tools=frozenset(
            {
                "community.get_post",
                "community.get_own_draft",
                "publication.get_schedule",
            }
        ),
    ),
    "APPEND_CONTENT": OperationContract(
        allowed_tools=frozenset(
            {
                "community.get_own_draft",
                "creator.revise_draft",
                "publication.get_schedule",
                "publication.update_schedule",
            }
        ),
        required_tools=frozenset({"creator.revise_draft"}),
    ),
    "REPLACE_CONTENT": OperationContract(
        allowed_tools=frozenset(
            {
                "community.get_own_draft",
                "creator.revise_draft",
                "publication.get_schedule",
                "publication.update_schedule",
            }
        ),
        required_tools=frozenset({"creator.revise_draft"}),
    ),
    "UPDATE_TITLE": OperationContract(
        allowed_tools=frozenset(
            {
                "community.get_own_draft",
                "creator.revise_draft",
                "publication.get_schedule",
                "publication.update_schedule",
            }
        ),
        required_tools=frozenset({"creator.revise_draft"}),
    ),
    # Content improvement with LLM-detected need_reference — allows a search
    # step before the revise so the Creator Agent can use community posts as
    # reference material.
    "IMPROVE_CONTENT": OperationContract(
        allowed_tools=frozenset(
            {
                "community.search_posts",
                "community.get_own_draft",
                "creator.revise_draft",
                "publication.get_schedule",
                "publication.update_schedule",
            }
        ),
        required_tools=frozenset({"creator.revise_draft"}),
    ),
    "UPDATE_SCHEDULE": OperationContract(
        allowed_tools=frozenset(
            {"publication.get_schedule", "publication.update_schedule"}
        ),
        required_tools=frozenset({"publication.update_schedule"}),
    ),
    "CANCEL_SCHEDULE": OperationContract(
        allowed_tools=frozenset(
            {"publication.get_schedule", "publication.cancel_schedule"}
        ),
        required_tools=frozenset({"publication.cancel_schedule"}),
    ),
    "PUBLISH_NOW": OperationContract(
        allowed_tools=frozenset(
            {
                "publication.get_schedule",
                "publication.cancel_schedule",
                "community.get_own_draft",
                "publication.publish_now",
            }
        ),
        required_tools=frozenset({"publication.publish_now"}),
    ),
}


class OperationPlanGuard:
    """Make IntentDelta authoritative over model-proposed plan metadata."""

    def enforce(
        self,
        *,
        intent_delta: IntentDelta | None,
        plan: AgentPlan,
    ) -> AgentPlan:
        if intent_delta is None or intent_delta.operation in {
            "CREATE_POST",
            "OPEN_PLAN",
        }:
            return plan
        contract = _CONTRACTS.get(intent_delta.operation)
        if contract is None:
            return plan
        tools = {step.tool for step in plan.steps}
        forbidden = sorted(tools - contract.allowed_tools)
        missing = sorted(contract.required_tools - tools)
        missing_any = bool(contract.required_any_tools) and tools.isdisjoint(
            contract.required_any_tools
        )
        if forbidden or missing or missing_any:
            details = []
            if forbidden:
                details.append(f"禁止工具: {', '.join(forbidden)}")
            if missing:
                details.append(f"缺少工具: {', '.join(missing)}")
            if missing_any:
                details.append(
                    "缺少任一只读工具: "
                    + ", ".join(sorted(contract.required_any_tools))
                )
            raise OperationPlanViolation(
                f"{intent_delta.operation} 执行计划违反操作契约（{'；'.join(details)}）"
            )
        return plan.model_copy(update={"intent": intent_delta.operation})


__all__ = ["OperationPlanGuard", "OperationPlanViolation"]
