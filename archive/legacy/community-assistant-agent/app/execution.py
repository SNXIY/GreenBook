from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from app.domain import AdaptiveExecutionDecision, AgentPlan, VerificationDecision
from app.tools import RiskLevel, ToolRegistry


ExecutionPath = Literal["DIRECT", "TOOL", "CREATOR", "ORCHESTRATED"]
WorkloadLane = Literal["ROUTING", "READ", "WRITE"]


_EXPLICIT_CREATOR_REQUEST = re.compile(
    r"(?:帮我|请(?:帮我)?|麻烦(?:帮我)?|给我|替我|我要你|我想让你)"
    r".{0,16}(?:创作|撰写|写|生成|改写).{0,100}(?:帖子|文章|草稿|文案)",
    re.IGNORECASE | re.DOTALL,
)
_DIRECT_CREATOR_REQUEST = re.compile(
    r"^(?:创作|撰写|写|生成|改写)(?:一|1|个|篇|份).{0,100}"
    r"(?:帖子|文章|草稿|文案)",
    re.IGNORECASE | re.DOTALL,
)
_CREATOR_FOLLOW_UP_ACTION = re.compile(
    r"(?:并|然后|再|之后|随后|完成后).{0,20}(?:发布|定时|预约|删除|审核|回复)",
    re.IGNORECASE | re.DOTALL,
)
_CREATOR_UPSTREAM_WORK = re.compile(
    r"(?:先|首先).{0,30}(?:搜索|检索|查找|分析|总结)|"
    r"(?:搜索|检索|查找|分析|总结).{0,30}(?:再|然后|之后|后).{0,20}"
    r"(?:创作|撰写|写|生成|改写)|"
    r"参考.{0,20}(?:帖子|文章|评论)",
    re.IGNORECASE | re.DOTALL,
)
_IMMEDIATE_PUBLISH_FOLLOW_UP = re.compile(
    r"^(?:请)?(?:把)?(?:刚才|上一轮|上次)?(?:生成|创作|写)?(?:的)?"
    r"(?:它|这篇|那篇)?(?:帖子|文章|草稿)?(?:现在|立即|直接)?"
    r"(?:给我)?发布(?:吧|掉|出去)?[。！!]*$|"
    r"^(?:现在|立即|直接)?发布(?:它|这篇|那篇)?(?:帖子|文章|草稿)?(?:吧)?[。！!]*$",
    re.IGNORECASE,
)
_SCHEDULE_OR_BATCH_HINT = re.compile(
    r"(?:定时|预约|稍后|之后|分钟|小时|明天|后天|全部|批量|这些|所有)",
    re.IGNORECASE,
)


def parse_explicit_schedule_time(
    prompt: str,
    *,
    client_timezone: str,
    now: datetime | None = None,
    existing_run_at: datetime | None = None,
) -> str | None:
    """Compatibility façade over TemporalResolver (sole authority).

    Returns an ISO-8601 ``run_at`` string when resolution succeeds; otherwise
    ``None``. Relative delays are already solidified against ``now`` /
    ``existing_run_at`` inside TemporalResolver — never deferred to tool time.
    """

    from zoneinfo import ZoneInfo

    from app.temporal_resolver import resolve_schedule_time

    try:
        zone = ZoneInfo(client_timezone)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    if now is None:
        current = datetime.now(zone)  # noqa: DTZ005 — façade fallback only
    elif now.tzinfo is None:
        current = now.replace(tzinfo=zone)
    else:
        current = now.astimezone(zone)
    existing = None
    if existing_run_at is not None:
        if existing_run_at.tzinfo is None:
            existing = existing_run_at.replace(tzinfo=zone)
        else:
            existing = existing_run_at.astimezone(zone)
    resolution = resolve_schedule_time(
        message=prompt,
        current_time=current,
        timezone=client_timezone,
        existing_run_at=existing,
    )
    if resolution.run_at is not None:
        return resolution.run_at.isoformat()
    return None


def is_explicit_single_draft_request(prompt: str) -> bool:
    """Recognize only high-confidence, single-draft imperative commands.

    This is a topic-agnostic control-plane grammar, not a business-intent keyword
    router. Ambiguous requests and anything with upstream work or downstream
    side effects remain under LLM planning and policy control.
    """
    normalized = " ".join(prompt.strip().split())
    if not normalized or normalized.endswith(("?", "？")):
        return False
    if _CREATOR_FOLLOW_UP_ACTION.search(normalized):
        return False
    if _CREATOR_UPSTREAM_WORK.search(normalized):
        return False
    return bool(
        _EXPLICIT_CREATOR_REQUEST.search(normalized)
        or _DIRECT_CREATOR_REQUEST.search(normalized)
    )


def is_new_scheduled_post_request(prompt: str) -> bool:
    """Recognize a new post request that also contains a publish time.

    This guard is intentionally conservative.  It prevents stale schedules
    from turning a new request such as “明天上午八点发布一篇关于 Kafka 的帖子”
    into an update of an older schedule.  References to an existing object are
    excluded and remain on the normal target-resolution path.
    """
    normalized = "".join(prompt.strip().split())
    if not normalized:
        return False
    existing_ref = re.search(
        r"(?:\u521a\u624d|\u4e0a\u4e00\u8f6e|\u4e0a\u6b21|\u8fd9\u7bc7|\u8fd9\u4e2a|\u8be5|\u5b83|\u8349\u7a3f|\u5e16\u5b50\u53f7|draft:|post:|schedule:)",
        normalized,
        re.IGNORECASE,
    )
    if existing_ref and re.search(
        r"(?:\u4fee\u6539|\u66f4\u65b0|\u8c03\u6574|\u589e\u52a0|\u8865\u5145|\u6539\u6210|\u6539\u4e3a|\u53d1\u5e03\u65f6\u95f4)",
        normalized,
    ):
        return False
    has_post_shape = bool(
        re.search(
            r"(?:\u53d1\u5e03|\u521b\u4f5c|\u64b0\u5199|\u5199|\u751f\u6210).{0,80}(?:\u4e00\u7bc7|\u4e00\u6761|\u4e00\u4e2a).{0,120}(?:\u5e16\u5b50|\u6587\u7ae0|\u8349\u7a3f)",
            normalized,
            re.IGNORECASE,
        )
    )
    has_schedule_shape = bool(
        re.search(
            r"(?:\u660e\u5929|\u540e\u5929|\u4eca\u5929|\u4e0a\u5348|\u4e0b\u5348|\u65e9\u4e0a|\u665a\u4e0a|\u51e0\u5206\u949f\u540e|\u5c0f\u65f6\u540e|\u5b9a\u65f6|"
            r"(?:\d{1,4}|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e]{1,4})"
            r"\s*(?:\u5206\u949f|\u5c0f\u65f6)\s*(?:\u4e4b\u540e|\u540e)|"
            r"\u5728\u300f?\d{1,2}[:\uff1a]\d{2})",
            normalized,
        )
    )
    return has_post_shape and has_schedule_shape


def is_immediate_publish_follow_up(prompt: str) -> bool:
    """Recognize an unambiguous request to publish the immediately prior draft."""
    normalized = "".join(prompt.strip().split())
    if not normalized or len(normalized) > 40:
        return False
    if _SCHEDULE_OR_BATCH_HINT.search(normalized):
        return False
    return bool(_IMMEDIATE_PUBLISH_FOLLOW_UP.fullmatch(normalized))


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
        is_plain_answer = (
            decision.intent.domain == "general_answer"
            and not decision.intent.required_capabilities
        )
        if not is_plain_answer:
            return (
                "ORCHESTRATED",
                AgentPlan(
                    intent="ORCHESTRATE",
                    summary="结构化意图包含社区任务，已升级为受控编排",
                    intent_detail=decision.intent,
                    steps=[],
                ),
            )
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
    try:
        for step in plan.steps:
            registry.get(step.tool)
    except ValueError:
        # The deterministic Plan Compiler will return a structured diagnostic
        # and give the Planner a bounded opportunity to repair invented tools.
        return "ORCHESTRATED", plan

    return normalize_compiled_path(path, plan, registry), plan


def normalize_compiled_path(
    path: ExecutionPath,
    plan: AgentPlan,
    registry: ToolRegistry,
) -> ExecutionPath:
    """Re-check a path after Plan Compiler repair or dynamic replanning."""
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

    return path


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


def deterministic_verification(
    *,
    plan: AgentPlan,
    outputs: list[dict[str, Any]],
    registry: ToolRegistry,
) -> VerificationDecision | None:
    """Accept a completed write workflow without another model judgment.

    Plan Compiler already proved goal capability coverage, Tool Registry has
    validated every typed output, and the side-effect ledger is the authority
    for writes. Re-asking an LLM whether those receipts exist adds latency and
    can incorrectly fail an already committed operation. Pure read/analysis
    plans still use the semantic Verifier because empty or weak evidence may
    require replanning.
    """
    if not plan.steps or not any(
        registry.get(step.tool).side_effecting for step in plan.steps
    ):
        return None

    by_task = {
        str(item.get("task_id")): item
        for item in outputs
        if item.get("task_id")
    }
    evidence_collections = {
        "post_search_results": "results",
        "user_set": "users",
        "post_collection": "posts",
        "topic_analysis": "topics",
        "owned_post_set": "posts",
    }
    for step in plan.steps:
        record = by_task.get(str(step.task_id))
        if record is None or not isinstance(record.get("result"), dict):
            return None
        result = record["result"]
        if result.get("skipped"):
            # Conditional skips require semantic assessment of goal coverage.
            return None
        expected = str(step.expected_artifact_type or "").lower()
        actual = str(record.get("artifact_type") or "").lower()
        if expected and actual != expected:
            return None
        evidence_field = evidence_collections.get(actual)
        if evidence_field and not list(result.get(evidence_field) or []):
            # A valid but empty envelope needs semantic assessment; it cannot
            # prove an evidence-dependent write goal was satisfied.
            return None

    return VerificationDecision(
        decision="COMPLETE",
        reason=(
            "所有计划步骤均已产生通过类型校验的 Artifact，"
            "外部写入已由副作用账本确认完成。"
        ),
        next_focus="",
    )


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


def render_goal_delta_result(
    plan: AgentPlan,
    outputs: list[dict[str, Any]],
) -> str | None:
    """Render bounded Goal mutations from typed receipts without another LLM."""
    if plan.intent == "UPDATE_SCHEDULE":
        updated = next(
            (
                dict(item.get("result") or {})
                for item in reversed(outputs)
                if item.get("tool") == "publication.update_schedule"
            ),
            {},
        )
        action_id = str(updated.get("action_id") or "").strip()
        run_at = str(updated.get("run_at") or updated.get("runAt") or "").strip()
        if not action_id or not run_at:
            return None
        from app.temporal_resolver import format_run_at_for_user

        return (
            f"已修改这个帖子的发布时间为 {format_run_at_for_user(run_at)}"
            f"（定时任务号：{action_id}）。"
        )
    if plan.intent == "CANCEL_SCHEDULE":
        cancelled = next(
            (
                dict(item.get("result") or {})
                for item in reversed(outputs)
                if item.get("tool") == "publication.cancel_schedule"
            ),
            {},
        )
        action_id = str(
            cancelled.get("action_id")
            or cancelled.get("actionId")
            or cancelled.get("schedule_id")
            or ""
        ).strip()
        if not action_id:
            return None
        return f"已取消这个帖子的定时发布任务（任务号：{action_id}）。草稿仍然保留，可以继续修改或重新安排发布时间。"
    if plan.intent == "PUBLISH_NOW":
        published = next(
            (
                dict(item.get("result") or {})
                for item in reversed(outputs)
                if item.get("tool") == "publication.publish_now"
            ),
            {},
        )
        post_id = str(
            published.get("post_id") or published.get("postId") or ""
        ).strip()
        if not post_id:
            return None
        return f"帖子已立即发布（帖子号：{post_id}）。"
    if plan.intent not in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE"}:
        return None
    revised = next(
        (
            dict(item.get("result") or {})
            for item in reversed(outputs)
            if item.get("tool") == "creator.revise_draft"
        ),
        {},
    )
    draft_id = str(revised.get("draft_id") or revised.get("draftId") or "").strip()
    if not draft_id:
        return None
    title = str(revised.get("title") or "当前帖子").strip()
    schedule = next(
        (
            dict(item.get("result") or {})
            for item in reversed(outputs)
            if item.get("tool") == "publication.update_schedule"
        ),
        None,
    )
    action = {
        "APPEND_CONTENT": "已按你的要求补充内容",
        "REPLACE_CONTENT": "已按你的要求重写正文",
        "UPDATE_TITLE": "已按你的要求修改标题",
    }[plan.intent]
    response = f"{action}：《{title}》（草稿号：{draft_id}）。"
    if schedule is not None:
        run_at = str(schedule.get("run_at") or schedule.get("runAt") or "").strip()
        response += "原定时发布任务已换绑到这个新版本"
        if run_at:
            response += f"，发布时间仍为 {run_at}"
        response += "。"
    return response


def render_continuation_publish_result(outputs: list[dict[str, Any]]) -> str:
    draft = next(
        (
            item.get("result", {})
            for item in outputs
            if item.get("tool") == "community.get_own_draft"
        ),
        {},
    )
    receipt = next(
        (
            item.get("result", {})
            for item in outputs
            if item.get("tool") == "publication.publish_now"
        ),
        {},
    )
    post_id = str(receipt.get("post_id") or receipt.get("id") or "").strip()
    if not post_id:
        raise ValueError("发布完成但缺少帖子回执")
    title = str(draft.get("title") or "上一轮草稿").strip()
    return f"《{title}》已发布成功（帖子号：{post_id}）。"
