"""Compile composable TurnPlan Changes into an executable AgentPlan DAG.

Replaces one-operation script compilers: content ± schedule ± publish are
combined from the Change list instead of keyword-selected branches.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.domain import (
    AgentPlan,
    AgentPlanStep,
    CommunityIntent,
    IntentDelta,
    TargetContext,
)
from app.temporal_resolver import (
    TemporalResolution,
    normalize_run_at_for_tool,
    resolve_schedule_time,
)
from app.turn_plan import TurnPlan, turn_plan_from_intent_delta


class ChangeCompiler:
    """Deterministic DAG builder for bounded Goal changes."""

    def compile(
        self,
        *,
        turn_plan: TurnPlan | None,
        target_context: TargetContext,
        intent: CommunityIntent,
        client_timezone: str = "Asia/Shanghai",
        current_time: datetime | None = None,
        existing_run_at: datetime | None = None,
    ) -> AgentPlan | None:
        if turn_plan is None or turn_plan.open_plan:
            return None
        if not turn_plan.changes:
            return None

        content = next(
            (c for c in turn_plan.changes if c.role == "CONTENT"),
            None,
        )
        schedule = next(
            (c for c in turn_plan.changes if c.role == "SCHEDULE"),
            None,
        )
        publication = next(
            (c for c in turn_plan.changes if c.role == "PUBLICATION"),
            None,
        )

        # Pure reads.
        if all(c.op == "QUERY" for c in turn_plan.changes):
            return self._compile_query(
                turn_plan=turn_plan,
                target_context=target_context,
                intent=intent,
            )

        if publication is not None and publication.op == "PUBLISH_NOW":
            return self._compile_publish_now(
                target_context=target_context,
                intent=intent,
            )

        if schedule is not None and schedule.op == "CANCEL" and content is None:
            return self._compile_cancel_schedule(
                target_context=target_context,
                intent=intent,
            )

        if (
            schedule is not None
            and schedule.op == "UPDATE"
            and content is None
        ):
            return self._compile_update_schedule(
                schedule_request=str(
                    schedule.payload.get("schedule_request")
                    or turn_plan.raw_message
                ),
                schedule_payload=dict(schedule.payload or {}),
                target_context=target_context,
                intent=intent,
                client_timezone=client_timezone,
                current_time=current_time,
                existing_run_at=existing_run_at,
            )

        if content is not None and content.op in {
            "APPEND",
            "REPLACE",
            "UPDATE_TITLE",
        }:
            return self._compile_content_mutation(
                content=content,
                schedule=schedule,
                target_context=target_context,
                intent=intent,
                client_timezone=client_timezone,
                message=turn_plan.raw_message,
                current_time=current_time,
                existing_run_at=existing_run_at,
            )

        if content is not None and content.op == "CREATE":
            if schedule is not None and schedule.op in {"UPDATE", "CREATE"}:
                return self._compile_create_and_schedule(
                    content=content,
                    schedule=schedule,
                    intent=intent,
                    client_timezone=client_timezone,
                    message=turn_plan.raw_message,
                    current_time=current_time,
                )
            # Pure content creation without a schedule — single-step creator path.
            return self._compile_create_only(
                content=content,
                intent=intent,
                message=turn_plan.raw_message,
                target_context=target_context,
            )

        # ANALYSIS (search/research) combined with content creation.
        analysis = next(
            (c for c in turn_plan.changes if c.role == "ANALYSIS"),
            None,
        )
        if analysis is not None and content is not None and content.op == "CREATE":
            return self._compile_research_and_create(
                analysis=analysis,
                content=content,
                schedule=schedule,
                intent=intent,
                client_timezone=client_timezone,
                message=turn_plan.raw_message,
                current_time=current_time,
            )

        return None

    def compile_intent_delta(
        self,
        *,
        intent_delta: IntentDelta | None,
        target_context: TargetContext,
        intent: CommunityIntent,
        client_timezone: str = "Asia/Shanghai",
        current_time: datetime | None = None,
        existing_run_at: datetime | None = None,
    ) -> AgentPlan | None:
        if intent_delta is None:
            return None
        if intent_delta.operation == "OPEN_PLAN":
            return None
        return self.compile(
            turn_plan=turn_plan_from_intent_delta(intent_delta),
            target_context=target_context,
            intent=intent,
            client_timezone=client_timezone,
            current_time=current_time,
            existing_run_at=existing_run_at,
        )

    def _compile_create_only(
        self,
        *,
        content: Any,
        intent: CommunityIntent,
        message: str,
        target_context: TargetContext | None = None,
    ) -> AgentPlan | None:
        # If the goal already has a draft, a bare CREATE_POST likely means
        # the user wants to revise or repurpose — let the Planner sort it out.
        if target_context is not None and target_context.content_target is not None:
            return None
        instruction = str(
            content.payload.get("instruction")
            or content.payload.get("message")
            or message
            or intent.goal
        ).strip()
        if not instruction:
            return None
        normalized = intent.model_copy(
            update={"required_capabilities": ["generation"]}
        )
        return AgentPlan(
            intent="CREATE_POST",
            summary="创作一篇新的帖子草稿",
            response_guidance=(
                "根据 Creator 返回的草稿内容告知用户帖子已生成，"
                "列出草稿号并提示可以继续修改或安排发布。"
            ),
            intent_detail=normalized,
            steps=[
                AgentPlanStep(
                    task_id="create-draft",
                    agent="ContentCreationAgent",
                    primary_capability="generation",
                    capabilities=["generation"],
                    tool="creator.create_draft",
                    label="创作帖子草稿",
                    arguments={"instruction": instruction},
                    expected_artifact_type="content_draft",
                    max_attempts=2,
                )
            ],
        )

    def _compile_create_and_schedule(
        self,
        *,
        content: Any,
        schedule: Any,
        intent: CommunityIntent,
        client_timezone: str,
        message: str,
        current_time: datetime | None,
    ) -> AgentPlan | None:
        instruction = str(
            content.payload.get("instruction")
            or content.payload.get("message")
            or message
            or intent.goal
        ).strip()
        if not instruction:
            return None
        schedule_request = str(
            schedule.payload.get("schedule_request")
            or content.payload.get("schedule_request")
            or message
        )
        run_at, _resolution = self._solidify_run_at(
            schedule_request=schedule_request,
            schedule_payload=dict(schedule.payload or {}),
            client_timezone=client_timezone,
            current_time=current_time,
            existing_run_at=None,
        )
        if run_at is None:
            # Time resolution failed — fall back to create-only with
            # the time expression preserved so Planner can handle it.
            # Pass target_context=None to skip the "already has draft" guard;
            # the user explicitly asked for a new post with a schedule.
            return self._compile_create_only(
                content=content,
                intent=intent,
                message=message,
                target_context=None,
            )
        normalized = intent.model_copy(
            update={"required_capabilities": ["generation", "schedule_publish"]}
        )
        return AgentPlan(
            intent="CREATE_AND_SCHEDULE",
            summary="创作帖子草稿并按指定时间安排定时发布",
            response_guidance=(
                "告知用户帖子草稿已生成并已安排定时发布。"
                "明确说出草稿号和北京时间发布时刻。"
            ),
            intent_detail=normalized,
            steps=[
                AgentPlanStep(
                    task_id="create-draft",
                    agent="ContentCreationAgent",
                    primary_capability="generation",
                    capabilities=["generation"],
                    tool="creator.create_draft",
                    label="创作帖子草稿",
                    arguments={"instruction": instruction},
                    expected_artifact_type="content_draft",
                    max_attempts=2,
                ),
                AgentPlanStep(
                    task_id="schedule-draft",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.schedule",
                    label="安排定时发布",
                    arguments={"run_at": run_at},
                    depends_on=["create-draft"],
                    expected_artifact_type="schedule_receipt",
                    max_attempts=2,
                ),
            ],
        )

    def _compile_research_and_create(
        self,
        *,
        analysis: Any,
        content: Any,
        schedule: Any | None,
        intent: CommunityIntent,
        client_timezone: str,
        message: str,
        current_time: datetime | None,
    ) -> AgentPlan | None:
        search_query = str(
            analysis.payload.get("instruction")
            or analysis.payload.get("message")
            or message
        ).strip()
        instruction = str(
            content.payload.get("instruction")
            or content.payload.get("message")
            or message
            or intent.goal
        ).strip()
        if not search_query or not instruction:
            return None
        steps: list[AgentPlanStep] = [
            AgentPlanStep(
                task_id="search-posts",
                agent="SearchAgent",
                primary_capability="search",
                capabilities=["search"],
                tool="community.search_posts",
                label="检索社区热门帖子作为参考",
                arguments={"query": search_query, "limit": 5},
                expected_artifact_type="post_search_results",
                max_attempts=2,
            ),
            AgentPlanStep(
                task_id="create-draft",
                agent="ContentCreationAgent",
                primary_capability="generation",
                capabilities=["generation"],
                tool="creator.create_draft",
                label="参考检索结果创作帖子草稿",
                arguments={"instruction": instruction},
                depends_on=["search-posts"],
                expected_artifact_type="content_draft",
                max_attempts=2,
            ),
        ]
        capabilities = ["search", "generation"]
        if schedule is not None and schedule.op in {"UPDATE", "CREATE"}:
            schedule_request = str(
                schedule.payload.get("schedule_request")
                or content.payload.get("schedule_request")
                or message
            )
            run_at, _resolution = self._solidify_run_at(
                schedule_request=schedule_request,
                schedule_payload=dict(schedule.payload or {}),
                client_timezone=client_timezone,
                current_time=current_time,
                existing_run_at=None,
            )
            if run_at is not None:
                capabilities.append("schedule_publish")
                steps.append(
                    AgentPlanStep(
                        task_id="schedule-draft",
                        agent="PublishAgent",
                        primary_capability="schedule_publish",
                        capabilities=["schedule_publish"],
                        tool="publication.schedule",
                        label="安排定时发布",
                        arguments={"run_at": run_at},
                        depends_on=["create-draft"],
                        expected_artifact_type="schedule_receipt",
                        max_attempts=2,
                    )
                )
        normalized = intent.model_copy(
            update={"required_capabilities": capabilities}
        )
        summary = (
            "检索社区帖子 → 参考创作草稿 → 安排定时发布"
            if schedule is not None
            else "检索社区帖子 → 参考创作草稿"
        )
        return AgentPlan(
            intent="RESEARCH_AND_CREATE",
            summary=summary,
            response_guidance=(
                "告知用户已完成社区检索和草稿创作。"
                "列出检索到的关键参考帖子和生成的草稿号。"
            ),
            intent_detail=normalized,
            steps=steps,
        )

    def _compile_query(
        self,
        *,
        turn_plan: TurnPlan,
        target_context: TargetContext,
        intent: CommunityIntent,
    ) -> AgentPlan | None:
        change = turn_plan.changes[0]
        if change.role == "SCHEDULE":
            target = target_context.schedule_target
            if target is None:
                return None
            tool, agent, capability = (
                "publication.get_schedule",
                "PublishAgent",
                "schedule_publish",
            )
            arguments = {"action_id": target.target_id}
            artifact_type = "schedule_receipt"
            summary = "查询当前目标帖子的发布时间"
            operation = "QUERY_SCHEDULE"
        elif change.role == "CONTENT":
            target = target_context.content_target
            if target is None or target.target_type != "DRAFT":
                return None
            tool, agent, capability = (
                "community.get_own_draft",
                "PublishAgent",
                "publishing",
            )
            arguments = {"draft_id": target.target_id}
            artifact_type = "content_draft"
            summary = "查询当前目标帖子的内容"
            operation = "QUERY_CONTENT"
        else:
            publication = target_context.publication_target
            schedule = target_context.schedule_target
            content = target_context.content_target
            if publication is not None:
                tool, agent, capability = (
                    "community.get_post",
                    "SearchAgent",
                    "read_post",
                )
                arguments = {"post_id": publication.target_id}
                artifact_type = "post_content"
            elif schedule is not None:
                tool, agent, capability = (
                    "publication.get_schedule",
                    "PublishAgent",
                    "schedule_publish",
                )
                arguments = {"action_id": schedule.target_id}
                artifact_type = "schedule_receipt"
            elif content is not None and content.target_type == "DRAFT":
                tool, agent, capability = (
                    "community.get_own_draft",
                    "PublishAgent",
                    "publishing",
                )
                arguments = {"draft_id": content.target_id}
                artifact_type = "content_draft"
            else:
                return None
            summary = "查询当前目标帖子的发布状态"
            operation = "QUERY_PUBLICATION_STATUS"

        normalized = intent.model_copy(
            update={"required_capabilities": [capability], "risk": "low"}
        )
        return AgentPlan(
            intent=operation,
            summary=summary,
            response_guidance="只根据只读工具返回的当前状态回答，不执行任何修改或发布操作。",
            intent_detail=normalized,
            steps=[
                AgentPlanStep(
                    task_id=f"read-{operation.lower().replace('_', '-')}",
                    agent=agent,
                    primary_capability=capability,
                    capabilities=[capability],
                    tool=tool,
                    label=summary,
                    arguments=arguments,
                    expected_artifact_type=artifact_type,
                    max_attempts=2,
                )
            ],
        )

    def _compile_cancel_schedule(
        self,
        *,
        target_context: TargetContext,
        intent: CommunityIntent,
    ) -> AgentPlan | None:
        schedule_target = target_context.schedule_target
        if schedule_target is None:
            return None
        normalized = intent.model_copy(
            update={"required_capabilities": ["schedule_publish"]}
        )
        return AgentPlan(
            intent="CANCEL_SCHEDULE",
            summary="取消当前目标绑定的定时发布任务",
            response_guidance="根据真实取消回执说明定时发布已取消。",
            intent_detail=normalized,
            steps=[
                AgentPlanStep(
                    task_id="read-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.get_schedule",
                    label="核验当前定时发布任务",
                    arguments={"action_id": schedule_target.target_id},
                    expected_artifact_type="schedule_receipt",
                    max_attempts=2,
                ),
                AgentPlanStep(
                    task_id="cancel-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.cancel_schedule",
                    label="取消当前定时发布任务",
                    arguments={},
                    depends_on=["read-current-schedule"],
                    expected_artifact_type="schedule_receipt",
                    max_attempts=2,
                ),
            ],
        )

    def _compile_update_schedule(
        self,
        *,
        schedule_request: str,
        schedule_payload: dict[str, Any],
        target_context: TargetContext,
        intent: CommunityIntent,
        client_timezone: str,
        current_time: datetime | None,
        existing_run_at: datetime | None,
    ) -> AgentPlan | None:
        schedule_target = target_context.schedule_target
        # Pure time mutation: UPDATE_SCHEDULE_TIME — run_at is mandatory.
        run_at, _resolution = self._solidify_run_at(
            schedule_request=schedule_request,
            schedule_payload=schedule_payload,
            client_timezone=client_timezone,
            current_time=current_time,
            existing_run_at=existing_run_at
            or self._existing_from_target(target_context),
        )
        if schedule_target is None or run_at is None:
            # Gate: never emit update_schedule without a solidified absolute time.
            return None
        normalized = intent.model_copy(
            update={"required_capabilities": ["schedule_publish"]}
        )
        return AgentPlan(
            intent="UPDATE_SCHEDULE",
            summary="修改当前目标绑定的定时发布时间",
            response_guidance=(
                "根据真实排期回执说明新的发布时间，使用北京时间向用户确认。"
            ),
            intent_detail=normalized,
            steps=[
                AgentPlanStep(
                    task_id="read-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.get_schedule",
                    label="核验当前定时发布任务",
                    arguments={"action_id": schedule_target.target_id},
                    expected_artifact_type="schedule_receipt",
                ),
                AgentPlanStep(
                    task_id="update-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.update_schedule",
                    label="修改当前定时发布时间",
                    arguments={"run_at": run_at},
                    depends_on=["read-current-schedule"],
                    expected_artifact_type="schedule_receipt",
                ),
            ],
        )

    def _compile_publish_now(
        self,
        *,
        target_context: TargetContext,
        intent: CommunityIntent,
    ) -> AgentPlan | None:
        content_target = target_context.content_target
        if content_target is None or content_target.target_type != "DRAFT":
            return None
        steps: list[AgentPlanStep] = []
        schedule_target = target_context.schedule_target
        if schedule_target is not None:
            steps.extend(
                [
                    AgentPlanStep(
                        task_id="read-current-schedule",
                        agent="PublishAgent",
                        primary_capability="schedule_publish",
                        capabilities=["schedule_publish"],
                        tool="publication.get_schedule",
                        label="核验当前定时发布任务",
                        arguments={"action_id": schedule_target.target_id},
                        expected_artifact_type="schedule_receipt",
                    ),
                    AgentPlanStep(
                        task_id="cancel-current-schedule",
                        agent="PublishAgent",
                        primary_capability="schedule_publish",
                        capabilities=["schedule_publish"],
                        tool="publication.cancel_schedule",
                        label="取消原定时发布任务",
                        depends_on=["read-current-schedule"],
                        expected_artifact_type="schedule_receipt",
                    ),
                ]
            )
        steps.append(
            AgentPlanStep(
                task_id="read-current-draft",
                agent="PublishAgent",
                primary_capability="publishing",
                capabilities=["publishing"],
                tool="community.get_own_draft",
                label="核验当前帖子草稿",
                arguments={"draft_id": content_target.target_id},
                expected_artifact_type="content_draft",
            )
        )
        publish_dependencies = ["read-current-draft"]
        if schedule_target is not None:
            publish_dependencies.append("cancel-current-schedule")
        steps.append(
            AgentPlanStep(
                task_id="publish-current-draft",
                agent="PublishAgent",
                primary_capability="publishing",
                capabilities=["publishing"],
                tool="publication.publish_now",
                label="立即发布当前帖子草稿",
                depends_on=publish_dependencies,
                expected_artifact_type="publication_receipt",
            )
        )
        normalized = intent.model_copy(
            update={"required_capabilities": ["publishing"]}
        )
        return AgentPlan(
            intent="PUBLISH_NOW",
            summary="立即发布当前目标绑定的帖子草稿",
            response_guidance="根据真实发布回执说明帖子已经发布。",
            intent_detail=normalized,
            steps=steps,
        )

    def _compile_content_mutation(
        self,
        *,
        content: Any,
        schedule: Any | None,
        target_context: TargetContext,
        intent: CommunityIntent,
        client_timezone: str,
        message: str,
        current_time: datetime | None = None,
        existing_run_at: datetime | None = None,
    ) -> AgentPlan | None:
        content_target = target_context.content_target
        if content_target is None or content_target.target_type != "DRAFT":
            return None
        instruction = str(
            content.payload.get("instruction")
            or content.payload.get("message")
            or message
            or intent.goal
        ).strip()
        if not instruction:
            return None

        op_name = {
            "APPEND": "APPEND_CONTENT",
            "REPLACE": "REPLACE_CONTENT",
            "UPDATE_TITLE": "UPDATE_TITLE",
        }.get(content.op, "APPEND_CONTENT")
        label = {
            "APPEND_CONTENT": "按本轮要求补充帖子内容",
            "REPLACE_CONTENT": "按本轮要求重写帖子正文",
            "UPDATE_TITLE": "按本轮要求修改帖子标题",
        }.get(op_name, "修订当前帖子草稿")

        steps: list[AgentPlanStep] = [
            AgentPlanStep(
                task_id="read-current-draft",
                agent="PublishAgent",
                primary_capability="publishing",
                capabilities=["publishing"],
                tool="community.get_own_draft",
                label="核验当前帖子草稿",
                arguments={"draft_id": content_target.target_id},
                expected_artifact_type="content_draft",
                max_attempts=2,
            ),
            AgentPlanStep(
                task_id="revise-current-draft",
                agent="ContentCreationAgent",
                primary_capability="rewrite_content",
                capabilities=["rewrite_content"],
                tool="creator.revise_draft",
                label=label,
                arguments={"instruction": instruction},
                depends_on=["read-current-draft"],
                expected_artifact_type="content_draft",
                max_attempts=2,
            ),
        ]

        schedule_target = target_context.schedule_target
        wants_schedule = schedule is not None or bool(
            content.payload.get("schedule_request")
        )
        new_run_at: str | None = None
        schedule_payload: dict[str, Any] = {}
        if schedule is not None and schedule.op == "UPDATE":
            schedule_payload = dict(schedule.payload or {})
            new_run_at, _ = self._solidify_run_at(
                schedule_request=str(
                    schedule.payload.get("schedule_request")
                    or content.payload.get("schedule_request")
                    or message
                ),
                schedule_payload=schedule_payload,
                client_timezone=client_timezone,
                current_time=current_time,
                existing_run_at=existing_run_at
                or self._existing_from_target(target_context),
            )
        elif content.payload.get("schedule_request"):
            new_run_at, _ = self._solidify_run_at(
                schedule_request=str(content.payload.get("schedule_request")),
                schedule_payload=dict(content.payload or {}),
                client_timezone=client_timezone,
                current_time=current_time,
                existing_run_at=existing_run_at
                or self._existing_from_target(target_context),
            )

        # Preserve/rebind schedule whenever the goal already has one, or the
        # user asked for a schedule change alongside the content edit.
        # REBIND_SCHEDULE_DRAFT may omit run_at; UPDATE_SCHEDULE_TIME must not
        # invent a time — only attach run_at when TemporalResolver solidified one.
        if schedule_target is not None and (
            wants_schedule or schedule_target is not None
        ):
            steps.insert(
                1,
                AgentPlanStep(
                    task_id="read-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.get_schedule",
                    label="核验当前定时发布任务",
                    arguments={"action_id": schedule_target.target_id},
                    # Serialize with draft verify so parallel TOOL_OUTPUT binds
                    # cannot race on assistant_target_bindings(goal_id, version).
                    depends_on=["read-current-draft"],
                    expected_artifact_type="schedule_receipt",
                    max_attempts=2,
                ),
            )
            rebind_args: dict[str, Any] = {}
            if new_run_at:
                rebind_args["run_at"] = new_run_at
            steps.append(
                AgentPlanStep(
                    task_id="rebind-current-schedule",
                    agent="PublishAgent",
                    primary_capability="schedule_publish",
                    capabilities=["schedule_publish"],
                    tool="publication.update_schedule",
                    label=(
                        "将定时任务换绑到修订后的草稿版本并修改发布时间"
                        if new_run_at
                        else "将定时任务换绑到修订后的草稿版本"
                    ),
                    arguments=rebind_args,
                    depends_on=[
                        "read-current-schedule",
                        "revise-current-draft",
                    ],
                    expected_artifact_type="schedule_receipt",
                    max_attempts=2,
                )
            )

        required = ["rewrite_content"]
        if schedule_target is not None:
            required.append("schedule_publish")
        normalized = intent.model_copy(update={"required_capabilities": required})
        preserves = schedule_target is not None
        action = {
            "APPEND_CONTENT": "补充当前帖子内容",
            "REPLACE_CONTENT": "重写当前帖子正文",
            "UPDATE_TITLE": "修改当前帖子标题",
        }.get(op_name, "修订当前帖子")
        summary = (
            f"{action}，并保持原定时发布时间"
            if preserves and not new_run_at
            else (
                f"{action}，并调整定时发布时间"
                if preserves and new_run_at
                else action
            )
        )
        return AgentPlan(
            intent=op_name,
            summary=summary,
            response_guidance=(
                "说明修改的是当前会话已绑定的原草稿。"
                "若调整了定时发布时间，用 Asia/Shanghai 本地时间明确说出新的发布时间，"
                "不要说“仍为/保持原时间”；若只换绑版本未改时间，再说明时间未变。"
            ),
            intent_detail=normalized,
            steps=steps,
        )


# Backward-compatible façade used by existing Worker imports.
class IntentDeltaPlanCompiler:
    """Adapter: IntentDelta → ChangeCompiler."""

    def __init__(self) -> None:
        self._compiler = ChangeCompiler()

    def compile(
        self,
        *,
        intent_delta: IntentDelta | None,
        target_context: TargetContext,
        intent: CommunityIntent,
        client_timezone: str = "Asia/Shanghai",
        current_time: datetime | None = None,
        existing_run_at: datetime | None = None,
    ) -> AgentPlan | None:
        return self._compiler.compile_intent_delta(
            intent_delta=intent_delta,
            target_context=target_context,
            intent=intent,
            client_timezone=client_timezone,
            current_time=current_time,
            existing_run_at=existing_run_at,
        )


def _coerce_aware(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


# Attach helpers onto ChangeCompiler without bloating the earlier class body edits.
def _solidify_run_at(
    self: ChangeCompiler,
    *,
    schedule_request: str,
    schedule_payload: dict[str, Any],
    client_timezone: str,
    current_time: datetime | None,
    existing_run_at: datetime | None,
) -> tuple[str | None, TemporalResolution | None]:
    """Return UTC ISO run_at for tools; prefer payload-stamped absolute time."""

    stamped = schedule_payload.get("run_at")
    if stamped:
        return normalize_run_at_for_tool(str(stamped)), None
    try:
        zone = ZoneInfo(client_timezone)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    if current_time is None:
        # Callers (Worker) must inject message/run created_at. Fallback is last
        # resort for unit tests that exercise ChangeCompiler in isolation.
        current = datetime.now(zone)
    else:
        current = _coerce_aware(current_time, client_timezone)
    existing = (
        _coerce_aware(existing_run_at, client_timezone)
        if existing_run_at is not None
        else None
    )
    resolution = resolve_schedule_time(
        message=schedule_request,
        current_time=current,
        timezone=client_timezone,
        existing_run_at=existing,
    )
    if resolution.run_at is None:
        return None, resolution
    return normalize_run_at_for_tool(resolution.run_at), resolution


def _existing_from_target(self: ChangeCompiler, target_context: TargetContext) -> datetime | None:
    del self
    schedule = target_context.schedule_target
    if schedule is None:
        return None
    raw = getattr(schedule, "run_at", None)
    if raw is None and hasattr(schedule, "model_dump"):
        raw = (schedule.model_dump(mode="json") or {}).get("run_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=ZoneInfo("UTC"))
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


ChangeCompiler._solidify_run_at = _solidify_run_at  # type: ignore[method-assign]
ChangeCompiler._existing_from_target = _existing_from_target  # type: ignore[method-assign]


__all__ = ["ChangeCompiler", "IntentDeltaPlanCompiler"]
