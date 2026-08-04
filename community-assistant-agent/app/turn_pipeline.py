"""Control-plane turn pipeline: TurnPlan → Goal resolve → bind → compile.

Worker owns persistence and execution; this module owns the pure sequencing of
goal-aware interpretation so natural multi-goal dialogue does not depend on
keyword-selected scripts inside the worker loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.change_compiler import ChangeCompiler
from app.domain import (
    AdaptiveExecutionDecision,
    AgentPlan,
    CommunityIntent,
    ConversationGoal,
    GoalResolution,
    IntentDelta,
    TargetContext,
    TurnIntent,
)
from app.goal_resolver import GoalResolver
from app.goal_workspace import goals_for_resolution
from app.intent_delta import TurnIntentParser
from app.turn_plan import (
    TurnPlan,
    TurnPlanBuilder,
    intent_delta_from_turn_plan,
)


@dataclass
class TurnPipelineResult:
    turn_plan: TurnPlan
    turn_intent: TurnIntent
    goal_resolution: GoalResolution
    intent_delta: IntentDelta | None = None
    compiled_plan: AgentPlan | None = None
    open_plan: bool = False


class TurnPipeline:
    """Resolve which Goal a turn addresses and compile its Changes."""

    def __init__(self) -> None:
        self.turn_intent_parser = TurnIntentParser()
        self.turn_plan_builder = TurnPlanBuilder()
        self.goal_resolver = GoalResolver()
        self.change_compiler = ChangeCompiler()

    def interpret(
        self,
        *,
        message: str,
        decision: AdaptiveExecutionDecision,
        conversation_goals: list[ConversationGoal],
        has_established_goals: bool,
        focus_goal_refs: list[str] | None = None,
    ) -> tuple[TurnIntent, TurnPlan, GoalResolution]:
        goals = goals_for_resolution(conversation_goals)
        # Concrete router operations beat keyword cascades. OPEN_PLAN / vague
        # open_plan hints do not — they must not erase bounded Changes.
        router_op = decision.primary_operation
        router_concrete = (
            router_op
            if router_op
            and router_op != "OPEN_PLAN"
            else None
        )
        turn_plan = self.turn_plan_builder.build(
            message=message,
            turn_relation=decision.turn_relation,
            intent_domain=decision.intent.domain,
            intent_goal=decision.intent.goal,
            plan_intent=decision.plan.intent if decision.plan else None,
            has_target=has_established_goals,
            router_operation=router_op,
            router_open_plan=decision.open_plan,
            follow_up_prompts=list(decision.follow_up_prompts or []),
            prefer_router=bool(router_concrete),
        )
        primary_text = (
            turn_plan.raw_message
            if not turn_plan.tasks
            else split_primary(message, turn_plan)
        )
        from app.turn_plan import primary_operation_from_changes, reconcile_router_operation

        parsed_intent = self.turn_intent_parser.parse(
            message=primary_text,
            has_target=has_established_goals,
            turn_relation=decision.turn_relation,
            intent_domain=decision.intent.domain,
            intent_goal=decision.intent.goal,
            plan_intent=decision.plan.intent if decision.plan else None,
        )
        plan_operation = (
            primary_operation_from_changes(turn_plan.changes, open_plan=False)
            if turn_plan.changes
            else ("OPEN_PLAN" if turn_plan.open_plan else parsed_intent.operation)
        )
        operation = reconcile_router_operation(
            router_operation=router_concrete,
            parsed_operation=plan_operation
            if plan_operation != "OPEN_PLAN"
            else parsed_intent.operation,
            message=primary_text,
            prefer_router=bool(router_concrete),
        )
        turn_intent = parsed_intent.model_copy(
            update={
                "operation": operation,  # type: ignore[arg-type]
                "operation_class": (
                    "READ"
                    if operation.startswith("QUERY_")
                    else (
                        "SIDE_EFFECT"
                        if operation
                        in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}
                        else "WRITE"
                    )
                ),
                "semantic_subject": turn_plan.semantic_subject
                or parsed_intent.semantic_subject,
                "raw_message": primary_text,
                "explicit_refs": list(
                    turn_plan.explicit_refs or parsed_intent.explicit_refs
                ),
                "confidence": max(turn_plan.confidence, parsed_intent.confidence),
            }
        )
        if turn_plan.open_plan and not turn_plan.changes and operation == "OPEN_PLAN":
            turn_intent = turn_intent.model_copy(update={"operation": "OPEN_PLAN"})
        resolution = self.goal_resolver.resolve(
            turn_intent=turn_intent,
            goals=goals,
            raw_message=message,
            focus_goal_refs=focus_goal_refs,
        )
        if turn_plan.open_plan and decision.turn_relation == "NEW_GOAL":
            resolution = GoalResolution(
                outcome="NEW_GOAL",
                confidence=turn_intent.confidence,
            )
        elif turn_intent.operation == "CREATE_POST":
            resolution = GoalResolution(
                outcome="NEW_GOAL",
                confidence=turn_intent.confidence,
            )
        if resolution.outcome == "RESOLVED" and resolution.goal_id:
            turn_plan = turn_plan.model_copy(
                update={"goal_ref": f"goal:{resolution.goal_id}"}
            )
        return turn_intent, turn_plan, resolution

    def bind_and_compile(
        self,
        *,
        turn_plan: TurnPlan,
        goal: ConversationGoal,
        run_id: str,
        message_id: str,
        intent: CommunityIntent,
        target_context: TargetContext | None = None,
        intent_domain: str | None = None,
        intent_goal: str | None = None,
        client_timezone: str = "Asia/Shanghai",
        current_time: datetime | None = None,
        existing_run_at: datetime | None = None,
    ) -> TurnPipelineResult:
        context = target_context or goal.target_context
        intent_delta = intent_delta_from_turn_plan(
            turn_plan=turn_plan,
            goal=goal,
            run_id=run_id,
            message_id=message_id,
            target_context=context,
            intent_domain=intent_domain,
            intent_goal=intent_goal,
        )
        open_plan = turn_plan.open_plan or intent_delta.operation in {
            "OPEN_PLAN",
            "CREATE_POST",
            "REPLY_COMMENT",
            "CONTINUE_ANALYSIS",
        }
        compiled = None
        if not open_plan:
            compiled = self.change_compiler.compile(
                turn_plan=turn_plan,
                target_context=context,
                intent=intent,
                client_timezone=client_timezone,
                current_time=current_time,
                existing_run_at=existing_run_at,
            )
            if compiled is None:
                open_plan = intent_delta.operation == "CREATE_POST"
        return TurnPipelineResult(
            turn_plan=turn_plan,
            turn_intent=TurnIntent(
                operation=intent_delta.operation,  # type: ignore[arg-type]
                operation_class=intent_delta.operation_class,
                target_role=intent_delta.target_role,
                semantic_subject=turn_plan.semantic_subject,
                raw_message=turn_plan.raw_message,
                explicit_refs=turn_plan.explicit_refs,
                confidence=turn_plan.confidence,
            ),
            goal_resolution=GoalResolution(
                outcome="RESOLVED",
                goal_id=goal.goal_id,
                confidence=turn_plan.confidence,
            ),
            intent_delta=intent_delta,
            compiled_plan=compiled,
            open_plan=open_plan and compiled is None,
        )


def split_primary(message: str, turn_plan: TurnPlan) -> str:
    if turn_plan.tasks:
        # Primary is the message without follow-ups when tasks were nested.
        return turn_plan.raw_message if not turn_plan.changes else (
            turn_plan.raw_message.split("顺便")[0].strip(" ，,")
            or turn_plan.raw_message
        )
    return message


__all__ = ["TurnPipeline", "TurnPipelineResult"]
