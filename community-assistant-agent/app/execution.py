from __future__ import annotations

from typing import Any, Literal

from app.domain import AdaptiveExecutionDecision, AgentPlan
from app.tools import RiskLevel, ToolRegistry


ExecutionPath = Literal["DIRECT", "TOOL", "CREATOR", "ORCHESTRATED"]
WorkloadLane = Literal["ROUTING", "READ", "WRITE"]


def normalize_execution_decision(
    decision: AdaptiveExecutionDecision,
    registry: ToolRegistry,
) -> tuple[ExecutionPath, AgentPlan]:
    """Enforce the fast-path boundary with deterministic tool metadata.

    The model may propose a path, but it cannot downgrade a multi-step or
    side-effecting plan into a cheaper execution path.
    """

    path: ExecutionPath = decision.execution_path
    plan = decision.plan

    if path == "DIRECT":
        return (
            path,
            AgentPlan(
                intent="ANSWER",
                summary=decision.classification_summary,
                response_guidance="直接回答用户，不调用工具。",
                intent_detail=decision.intent,
                steps=[],
            ),
        )

    if plan is None:
        return (
            "ORCHESTRATED",
            AgentPlan(
                intent="ORCHESTRATE",
                summary=decision.classification_summary,
                intent_detail=decision.intent,
                steps=[],
            ),
        )

    plan = plan.model_copy(update={"intent_detail": decision.intent})
    definitions = [registry.get(step.tool) for step in plan.steps]

    if path == "TOOL":
        is_single_read = (
            len(plan.steps) == 1
            and definitions[0].risk == RiskLevel.READ
            and not definitions[0].side_effecting
        )
        if not is_single_read:
            path = "ORCHESTRATED"
    elif path == "CREATOR":
        is_single_creator = (
            len(plan.steps) == 1
            and plan.steps[0].tool == "creator.create_draft"
        )
        if not is_single_creator:
            path = "ORCHESTRATED"

    return path, plan


def workload_lane(
    *,
    path: ExecutionPath,
    plan: AgentPlan,
    registry: ToolRegistry,
    persists_comment_reply: bool,
) -> Literal["READ", "WRITE"]:
    if persists_comment_reply:
        return "WRITE"
    if path == "CREATOR":
        return "WRITE"
    if any(registry.get(step.tool).side_effecting for step in plan.steps):
        return "WRITE"
    return "READ"


def requires_verification(path: ExecutionPath) -> bool:
    return path == "ORCHESTRATED"


def render_creator_result(outputs: list[dict[str, Any]]) -> str:
    result = next(
        (
            dict(item.get("result") or {})
            for item in reversed(outputs)
            if item.get("tool") == "creator.create_draft"
        ),
        {},
    )
    draft_id = str(result.get("draft_id") or "").strip()
    title = str(result.get("title") or "").strip()
    if not draft_id:
        raise ValueError("Creator completed without a bound Java draft")

    subject = f"《{title}》" if title else "帖子"
    return (
        f"已完成{subject}的创作，并保存为可编辑草稿（草稿号：{draft_id}）。"
        "你可以进入发布流程继续调整正文、补充图片，然后确认发布。"
    )
