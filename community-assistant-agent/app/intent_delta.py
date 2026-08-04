"""Turn-level IntentDelta parsing for the Goal-aware runtime.

The adaptive router already turns natural language into a typed intent. This
module converts that semantic route into a small, auditable mutation against
the current ConversationGoal. The Planner receives this contract instead of
having to rediscover whether a turn is a new goal or a continuation.
"""

from __future__ import annotations

import uuid
import re
from typing import Any

from app.domain import (
    ConversationGoal,
    IntentDelta,
    TargetContext,
    TurnIntent,
)
from app.execution import is_new_scheduled_post_request


_OPERATIONS = {
    "CREATE_POST",
    "APPEND_CONTENT",
    "REPLACE_CONTENT",
    "UPDATE_TITLE",
    "UPDATE_SCHEDULE",
    "PUBLISH_NOW",
    "CANCEL_SCHEDULE",
    "QUERY_SCHEDULE",
    "QUERY_CONTENT",
    "QUERY_PUBLICATION_STATUS",
    "OPEN_PLAN",
    "REPLY_COMMENT",
    "CONTINUE_ANALYSIS",
}

_READ_OPERATIONS = {
    "QUERY_SCHEDULE",
    "QUERY_CONTENT",
    "QUERY_PUBLICATION_STATUS",
}

_SIDE_EFFECT_OPERATIONS = {
    "UPDATE_SCHEDULE",
    "PUBLISH_NOW",
    "CANCEL_SCHEDULE",
}


class TurnIntentParser:
    """Interpret a turn without selecting or mutating a ConversationGoal."""

    def parse(
        self,
        *,
        message: str,
        has_target: bool,
        turn_relation: str = "NEW_GOAL",
        intent_domain: str | None = None,
        intent_goal: str | None = None,
        plan_intent: str | None = None,
    ) -> TurnIntent:
        text = message.strip()
        operation = IntentDeltaParser._operation(
            text=text,
            has_target=has_target,
            turn_relation=turn_relation,
            plan_intent=plan_intent,
            intent_domain=intent_domain,
            intent_goal=intent_goal,
        )
        explicit_refs = self._explicit_refs(text)
        return TurnIntent(
            operation=operation,  # type: ignore[arg-type]
            operation_class=IntentDeltaParser._operation_class(operation),  # type: ignore[arg-type]
            target_role=IntentDeltaParser._target_role(operation),  # type: ignore[arg-type]
            semantic_subject=self._semantic_subject(text, explicit_refs),
            raw_message=text,
            explicit_refs=explicit_refs,
            confidence=0.97 if operation in _READ_OPERATIONS else 0.92,
        )

    @staticmethod
    def read_operation(text: str) -> str | None:
        return IntentDeltaParser.read_operation(text)

    @staticmethod
    def _explicit_refs(text: str) -> list[str]:
        refs = re.findall(
            r"\b(?:goal|draft|schedule|post|artifact)[:\-][0-9a-zA-Z_-]+\b",
            text,
            flags=re.IGNORECASE,
        )
        refs.extend(
            re.findall(
                r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
                text,
            )
        )
        refs.extend(re.findall(r"\b[0-9]{8,}\b", text))
        return list(dict.fromkeys(refs))[:12]

    @staticmethod
    def _semantic_subject(text: str, explicit_refs: list[str]) -> str:
        subject = text.lower()
        for ref in explicit_refs:
            subject = subject.replace(ref.lower(), " ")
        removable = (
            "publication status",
            "publish time",
            "查询",
            "查看",
            "看看",
            "告诉我",
            "这条帖子",
            "这篇帖子",
            "这个帖子",
            "帖子",
            "草稿",
            "发布时间",
            "定时时间",
            "排期时间",
            "发布状态",
            "帖子内容",
            "草稿内容",
            "是多少",
            "是什么",
            "怎么样",
            "什么时候",
            "几点",
            "是否",
            "了吗",
            "内容",
            "正文",
            "的",
            "?",
            "？",
        )
        for token in removable:
            subject = subject.replace(token, " ")
        return " ".join(subject.split())[:500]


class IntentDeltaParser:
    """Compile one user turn into an explicit Goal mutation."""

    def parse(
        self,
        *,
        message: str,
        goal: ConversationGoal,
        target_context: TargetContext | None = None,
        run_id: str,
        message_id: str,
        turn_relation: str = "NEW_GOAL",
        intent_domain: str | None = None,
        intent_goal: str | None = None,
        plan_intent: str | None = None,
    ) -> IntentDelta:
        text = message.strip()
        context = target_context or goal.target_context
        has_target = any(
            target is not None
            for target in (
                context.content_target,
                context.schedule_target,
                context.publication_target,
                context.interaction_target,
            )
        )
        turn_intent = TurnIntentParser().parse(
            message=text,
            has_target=has_target,
            turn_relation=turn_relation,
            plan_intent=plan_intent,
            intent_domain=intent_domain,
            intent_goal=intent_goal,
        )
        return self.bind(
            turn_intent=turn_intent,
            message=text,
            goal=goal,
            target_context=context,
            run_id=run_id,
            message_id=message_id,
            turn_relation=turn_relation,
            intent_domain=intent_domain,
            intent_goal=intent_goal,
        )

    def bind(
        self,
        *,
        turn_intent: TurnIntent,
        message: str,
        goal: ConversationGoal,
        target_context: TargetContext,
        run_id: str,
        message_id: str,
        turn_relation: str,
        intent_domain: str | None,
        intent_goal: str | None,
    ) -> IntentDelta:
        text = message.strip()
        operation = turn_intent.operation
        operation_target = target_context.for_operation(operation)
        target_ref = (
            f"{operation_target.target_type.lower()}:{operation_target.target_id}"
            if operation_target is not None
            else goal.active_target_ref
        )

        preserve = self._preserve(operation, target_context)
        delta: dict[str, Any] = {
            "message": text,
            "intent_domain": intent_domain,
            "intent_goal": intent_goal,
            "turn_relation": turn_relation,
            "semantic_subject": turn_intent.semantic_subject,
            "explicit_refs": turn_intent.explicit_refs,
        }
        if operation in {"UPDATE_SCHEDULE"}:
            delta["schedule_request"] = text
        elif operation in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE"}:
            delta["instruction"] = text
            # When the user also asks to change the schedule ("并且五分钟
            # 之后发布"), capture that request so the compiler can rebind
            # the schedule to the revised content version.
            if IntentDeltaParser._has_schedule_request(text):
                delta["schedule_request"] = text

        return IntentDelta(
            delta_id=str(uuid.uuid4()),
            goal_id=goal.goal_id,
            run_id=run_id,
            message_id=message_id,
            operation=operation,  # type: ignore[arg-type]
            operation_class=turn_intent.operation_class,
            target_role=turn_intent.target_role,
            target_ref=target_ref,
            delta=delta,
            preserve=preserve,
            confidence=turn_intent.confidence,
            status="ACTIVE",
        )

    @staticmethod
    def _target_role(operation: str) -> str | None:
        if operation in {
            "APPEND_CONTENT",
            "REPLACE_CONTENT",
            "UPDATE_TITLE",
            "QUERY_CONTENT",
        }:
            return "CONTENT"
        if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "QUERY_SCHEDULE"}:
            return "SCHEDULE"
        if operation == "QUERY_PUBLICATION_STATUS":
            return "PUBLICATION"
        if operation == "PUBLISH_NOW":
            return "CONTENT"
        return None

    @staticmethod
    def _operation_class(operation: str) -> str:
        if operation in _READ_OPERATIONS:
            return "READ"
        if operation in _SIDE_EFFECT_OPERATIONS:
            return "SIDE_EFFECT"
        return "WRITE"

    @staticmethod
    def _has_schedule_request(text: str) -> bool:
        lowered = text.lower()
        # Digits or Chinese numerals followed by time units and "之后/后"
        if re.search(
            r"(?:\d+|[一二三四五六七八九十百千]+)\s*(?:分钟|小时|天|秒)"
            r"\s*(?:之后|后|以内|内)",
            lowered,
        ):
            return True
        # Content-modification keywords appearing near schedule keywords
        if re.search(
            r"(?:分钟之后|分钟后|小时之后|小时后|之后发布|之后发|"
            r"分钟以内|分钟后发布)",
            lowered,
        ):
            return True
        return False

    @staticmethod
    def _has_content_mutation_request(text: str) -> bool:
        """True when the user asks to change draft body/title substance."""

        lowered = text.lower()
        if re.search(
            r"(?:修改|调整|改一下|改动).{0,12}(?:内容|正文|帖子)",
            lowered,
        ):
            return True
        content_tokens = (
            "增加",
            "添加",
            "补充",
            "加上",
            "加入",
            "代码",
            "append",
            "add",
            "加一些",
            "加个",
            "加点",
            "加段",
            "加一点",
            "实战经验",
            "修改内容",
            "修改一下内容",
            "改一下内容",
            "修改帖子",
            "改一下帖子",
            "修改这个帖子",
            "改一下这个帖子",
        )
        return any(token in lowered for token in content_tokens)

    @staticmethod
    def _operation(
        *,
        text: str,
        has_target: bool,
        turn_relation: str,
        plan_intent: str | None,
        intent_domain: str | None,
        intent_goal: str | None,
    ) -> str:
        # A new post with an explicit publish time starts a fresh Goal even if
        # the conversation still contains older drafts or schedules.
        if is_new_scheduled_post_request(text):
            return "CREATE_POST"
        read_operation = IntentDeltaParser.read_operation(text)
        if read_operation is not None:
            return read_operation
        # Explicit lifecycle commands are authoritative. The semantic router
        # may understand the goal correctly while still attaching a stale or
        # contradictory proposed plan label. A user asking to cancel a
        # scheduled publication must never be downgraded to a content edit.
        semantic_text = f"{text} {intent_goal or ''}".lower()
        cancel_verbs = (
            "取消",
            "撤销",
            "停止",
            "作废",
            "不要",
            "别发",
            "cancel",
            "unschedule",
        )
        publication_objects = ("发布", "定时", "排期", "schedule")
        if any(token in semantic_text for token in cancel_verbs) and any(
            token in semantic_text for token in publication_objects
        ):
            return "CANCEL_SCHEDULE"
        if any(token in semantic_text for token in ("标题", "title")) and any(
            token in semantic_text
            for token in ("修改", "更新", "调整", "改一下", "change", "update")
        ):
            return "UPDATE_TITLE" if has_target else "CREATE_POST"
        if any(token in semantic_text for token in ("正文", "内容", "content")) and any(
            token in semantic_text
            for token in ("重写", "替换", "改写", "replace", "rewrite")
        ):
            return "REPLACE_CONTENT" if has_target else "CREATE_POST"
        lowered = text.lower()
        # Content±schedule compounds must win before pure schedule heuristics.
        # Example: "修改这个帖子的内容……然后发布时间改成五分钟之后" contains
        # both "修改…内容" and "发布时间…改成". UPDATE_SCHEDULE alone would
        # drop the content change and force an open Planner repair loop.
        content_mutation = IntentDeltaParser._has_content_mutation_request(lowered)
        if has_target and content_mutation:
            return "APPEND_CONTENT"
        # Keep common Chinese scheduling variants explicit. These checks run
        # before the generic MODIFY fallback so a time change is never treated
        # as content append.
        if "发布时间" in lowered and any(
            token in lowered
            for token in (
                "修改",
                "调整",
                "改成",
                "改为",
                "改到",
                "提前",
                "延后",
                "推迟",
                "分钟之后",
                "分钟后",
                "小时之后",
                "小时后",
            )
        ):
            return "UPDATE_SCHEDULE" if has_target else "CREATE_POST"
        if any(
            token in lowered
            for token in (
                "改时间",
                "调整时间",
                "修改发布时间",
                "调整发布时间",
                "发布时间调整",
                "发布时间改",
                "延后发布",
                "推迟发布",
                "推迟到",
                "延后到",
                "改到",
            )
        ) and any(
            token in lowered
            for token in ("发布", "定时", "排期", "分钟", "小时", "schedule")
        ):
            return "UPDATE_SCHEDULE" if has_target else "CREATE_POST"
        if any(token in lowered for token in ("取消定时", "取消发布", "撤销定时", "cancel schedule")):
            return "CANCEL_SCHEDULE"
        if any(token in lowered for token in ("立即发布", "现在发布", "发布吧", "直接发布", "publish now")):
            return "PUBLISH_NOW" if has_target else "CREATE_POST"
        if any(token in lowered for token in ("改标题", "修改标题", "更新标题", "change title")):
            return "UPDATE_TITLE" if has_target else "CREATE_POST"
        replace_tokens = (
            "替换正文", "重写正文", "改成", "replace content",
            "重写内容", "重写一下",
        )
        if any(token in lowered for token in replace_tokens):
            return "REPLACE_CONTENT" if has_target else "CREATE_POST"
        if any(token in lowered for token in ("改时间", "修改发布时间", "定时", "之后发布", "schedule")):
            return "UPDATE_SCHEDULE" if has_target else "CREATE_POST"
        # A model-proposed label is advisory. It is considered only after the
        # user text and the structured semantic goal have failed to identify a
        # concrete lifecycle command.
        normalized_plan = str(plan_intent or "").upper().strip()
        if normalized_plan == "PUBLISH_CONTINUATION_DRAFT":
            return "PUBLISH_NOW"
        if normalized_plan in _OPERATIONS and normalized_plan != "OPEN_PLAN":
            return normalized_plan
        domain = (intent_domain or "").lower()
        content_like = domain.startswith("content") or any(
            token in text.lower()
            for token in ("帖子", "草稿", "创作", "写一篇", "发布一篇")
        )
        if domain in {"comment_interaction"} or any(
            token in text.lower() for token in ("回复评论", "回复这条评论", "reply to comment")
        ):
            return "REPLY_COMMENT"
        if domain in {"data_analysis"} and turn_relation in {
            "CONTINUE",
            "MODIFY",
            "RETRY",
        }:
            return "CONTINUE_ANALYSIS"
        if not has_target or turn_relation == "NEW_GOAL":
            if content_like:
                return "CREATE_POST"
            return "OPEN_PLAN"
        # Do not guess APPEND_CONTENT for every CONTINUE/MODIFY. Unclassified
        # follow-ups fall through to the open Planner instead of mutating drafts.
        if has_target and turn_relation in {"MODIFY", "CONTINUE"} and content_like:
            return "APPEND_CONTENT"
        if has_target and domain in {"data_analysis"}:
            return "CONTINUE_ANALYSIS"
        return "OPEN_PLAN"

    @staticmethod
    def read_operation(text: str) -> str | None:
        """Recognize bounded lifecycle reads before mutation fallbacks run."""

        lowered = "".join(text.lower().split())
        mutation_markers = (
            "修改",
            "调整",
            "改成",
            "改为",
            "改到",
            "提前",
            "延后",
            "推迟",
            "取消",
            "撤销",
            "立即发布",
            "现在发布",
            "直接发布",
            "append",
            "update",
            "change",
            "cancel",
            "publishnow",
        )
        if any(marker in lowered for marker in mutation_markers):
            return None

        question_markers = (
            "多少",
            "几点",
            "什么时候",
            "是什么",
            "怎么样",
            "如何",
            "查询",
            "查看",
            "看看",
            "告诉我",
            "是否",
            "了吗",
            "?",
            "？",
            "what",
            "when",
            "status",
            "show",
            "get",
        )
        is_question = any(marker in lowered for marker in question_markers)
        if not is_question:
            return None

        if any(
            marker in lowered
            for marker in ("发布状态", "是否发布", "发布了吗", "有没有发布", "publicationstatus")
        ):
            return "QUERY_PUBLICATION_STATUS"
        if any(
            marker in lowered
            for marker in ("发布时间", "定时时间", "排期时间", "几点发布", "什么时候发布", "schedule")
        ):
            return "QUERY_SCHEDULE"
        if any(
            marker in lowered
            for marker in ("帖子内容", "草稿内容", "内容", "正文", "写了什么", "content")
        ):
            return "QUERY_CONTENT"
        return None

    @staticmethod
    def _preserve(
        operation: str,
        target_context: TargetContext,
    ) -> dict[str, Any]:
        has_schedule = target_context.schedule_target is not None
        if operation in _READ_OPERATIONS or operation == "OPEN_PLAN":
            return {}
        if operation == "CREATE_POST":
            return {"schedule": False, "content": False}
        if operation == "UPDATE_SCHEDULE":
            return {"content": True, "draft": True}
        if operation == "PUBLISH_NOW":
            return {"content": True, "schedule": False}
        if operation == "CANCEL_SCHEDULE":
            return {"content": True, "schedule": False}
        return {"schedule": has_schedule, "target": "content_target"}


class IntentDeltaBinder(IntentDeltaParser):
    """Bind an already resolved TurnIntent to one ConversationGoal."""


__all__ = ["IntentDeltaBinder", "IntentDeltaParser", "TurnIntentParser"]
