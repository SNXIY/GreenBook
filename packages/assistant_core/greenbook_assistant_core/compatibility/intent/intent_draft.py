"""Phase 6.8.1 Stage D-A — IntentDraft: simple intermediate representation.

IntentDraft lets the LLM output free-form text fields instead of strict enums.
The deterministic IntentCompiler then maps Draft → IntentSpec.

This separation means the LLM doesn't need to get enum values exactly right,
dramatically reducing empty-action failures on complex messages.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ...task.intent_models import (
    ActionType,
    ConditionType,
    ConstraintType,
    IntentAction,
    IntentCondition,
    IntentConstraint,
    IntentMode,
    IntentSpec,
    ResourceType,
)


# ═══════════════════════════════════════════════════════════════════════
# IntentDraft — LLM output target (simple, free-form)
# ═══════════════════════════════════════════════════════════════════════

class IntentDraft(BaseModel):
    """LLM produces this simple structure. No enum constraints."""

    goal: str = ""
    actions: list[str] = []        # ["search for posts", "create content", "publish"]
    conditions: list[str] = []     # ["if draft exists update else create"]
    constraints: list[str] = []    # ["approve before publish", "tomorrow 9am"]
    target_hint: str | None = None
    confidence: float = 0.9


# ═══════════════════════════════════════════════════════════════════════
# IntentCompiler — deterministic Draft → IntentSpec
# ═══════════════════════════════════════════════════════════════════════

class IntentCompiler:
    """Deterministically compiles a free-form IntentDraft into IntentSpec."""

    # ── Action keyword → (ActionType, ResourceType) ──
    _ACTION_PATTERNS: list[tuple[str, ActionType, ResourceType | None]] = [
        # CREATE patterns — generic, resource overridden by hints
        ("search", ActionType.SEARCH, ResourceType.POST),
        ("find", ActionType.SEARCH, ResourceType.POST),
        ("look up", ActionType.SEARCH, ResourceType.POST),
        ("搜", ActionType.SEARCH, ResourceType.POST),
        ("找", ActionType.SEARCH, ResourceType.POST),
        ("检索", ActionType.SEARCH, ResourceType.POST),
        # ANALYZE patterns
        ("analy", ActionType.ANALYZE, None),
        ("analys", ActionType.ANALYZE, None),
        ("summar", ActionType.ANALYZE, None),
        ("总结", ActionType.ANALYZE, None),
        ("分析", ActionType.ANALYZE, None),
        ("归纳", ActionType.ANALYZE, None),
        # CREATE patterns
        ("create", ActionType.CREATE, ResourceType.CONTENT),
        ("creat", ActionType.CREATE, ResourceType.CONTENT),
        ("write", ActionType.CREATE, ResourceType.CONTENT),
        ("writ", ActionType.CREATE, ResourceType.CONTENT),
        ("generat", ActionType.CREATE, ResourceType.CONTENT),
        ("draft", ActionType.CREATE, ResourceType.CONTENT),
        ("compos", ActionType.CREATE, ResourceType.CONTENT),
        ("写", ActionType.CREATE, ResourceType.CONTENT),
        ("创建", ActionType.CREATE, ResourceType.CONTENT),
        ("生成", ActionType.CREATE, ResourceType.CONTENT),
        ("新建", ActionType.CREATE, ResourceType.CONTENT),
        ("创作", ActionType.CREATE, ResourceType.CONTENT),
        ("搞", ActionType.CREATE, ResourceType.CONTENT),
        ("做", ActionType.CREATE, ResourceType.CONTENT),
        ("弄", ActionType.CREATE, ResourceType.CONTENT),
        ("来一篇", ActionType.CREATE, ResourceType.CONTENT),
        # UPDATE patterns
        ("update", ActionType.UPDATE, ResourceType.CONTENT),
        ("updat", ActionType.UPDATE, ResourceType.CONTENT),
        ("revis", ActionType.UPDATE, ResourceType.CONTENT),
        ("modif", ActionType.UPDATE, ResourceType.CONTENT),
        ("improv", ActionType.UPDATE, ResourceType.CONTENT),
        ("enhanc", ActionType.UPDATE, ResourceType.CONTENT),
        ("refine", ActionType.UPDATE, ResourceType.CONTENT),
        ("polish", ActionType.UPDATE, ResourceType.CONTENT),
        ("optim", ActionType.UPDATE, ResourceType.CONTENT),
        ("edit", ActionType.UPDATE, ResourceType.CONTENT),
        ("改", ActionType.UPDATE, ResourceType.CONTENT),
        ("修改", ActionType.UPDATE, ResourceType.CONTENT),
        ("优化", ActionType.UPDATE, ResourceType.CONTENT),
        ("完善", ActionType.UPDATE, ResourceType.CONTENT),
        ("调整", ActionType.UPDATE, ResourceType.CONTENT),
        ("润色", ActionType.UPDATE, ResourceType.CONTENT),
        ("改进", ActionType.UPDATE, ResourceType.CONTENT),
        ("补充", ActionType.UPDATE, ResourceType.CONTENT),
        ("更新", ActionType.UPDATE, ResourceType.CONTENT),
        # DELETE patterns (must be before PUBLISH/schedule)
        ("delet", ActionType.DELETE, ResourceType.SCHEDULE),
        ("cancel", ActionType.DELETE, ResourceType.SCHEDULE),
        ("remov", ActionType.DELETE, ResourceType.SCHEDULE),
        ("撤销", ActionType.DELETE, ResourceType.SCHEDULE),
        ("取消", ActionType.DELETE, ResourceType.SCHEDULE),
        # QUERY patterns (must be before PUBLISH/schedule)
        ("query", ActionType.QUERY, None),
        ("view", ActionType.QUERY, None),
        ("check", ActionType.QUERY, None),
        ("list", ActionType.QUERY, None),
        ("show", ActionType.QUERY, None),
        ("查", ActionType.QUERY, None),
        ("看", ActionType.QUERY, None),
        ("列出", ActionType.QUERY, None),
        ("查看", ActionType.QUERY, None),
        # PUBLISH patterns (check after DELETE and QUERY)
        ("publish", ActionType.PUBLISH, ResourceType.CONTENT),
        ("post", ActionType.PUBLISH, ResourceType.CONTENT),
        ("releas", ActionType.PUBLISH, ResourceType.CONTENT),
        ("send", ActionType.PUBLISH, ResourceType.CONTENT),
        ("发", ActionType.PUBLISH, ResourceType.CONTENT),
        ("发布", ActionType.PUBLISH, ResourceType.CONTENT),
        ("定时", ActionType.PUBLISH, ResourceType.SCHEDULE),
        ("schedule", ActionType.PUBLISH, ResourceType.SCHEDULE),
    ]

    # Resource hints in action text (DRAFT/SCHEDULE take priority over CONTENT)
    _RESOURCE_HINTS: list[tuple[str, ResourceType]] = [
        ("schedule", ResourceType.SCHEDULE),
        ("scheduled", ResourceType.SCHEDULE),
        ("定时", ResourceType.SCHEDULE),
        ("发布时间", ResourceType.SCHEDULE),
        ("draft", ResourceType.DRAFT),
        ("草稿", ResourceType.DRAFT),
        ("稿子", ResourceType.DRAFT),
        ("稿", ResourceType.DRAFT),
        ("post", ResourceType.POST),
        ("帖子", ResourceType.POST),
        ("community", ResourceType.POST),
        ("社区", ResourceType.POST),
        ("content", ResourceType.CONTENT),
        ("文章", ResourceType.CONTENT),
        ("内容", ResourceType.CONTENT),
        ("教程", ResourceType.CONTENT),
    ]

    # Time keywords in constraints
    _TIME_KEYWORDS = [
        "分钟", "小时", "天", "之后", "后发布", "明天", "今天",
        "晚上", "早上", "上午", "下午", "今晚", "明早", "明晚",
        "下周", "几点", "什么时间", "时间", "几点发",
        "minute", "hour", "tomorrow", "today", "tonight", "morning",
        "evening", "afternoon",
    ]

    # Approval keywords in constraints
    _APPROVAL_KEYWORDS = [
        "确认", "审核", "审一下", "看一下", "看过", "看看",
        "approv", "review", "check", "confirm",
        "先别发", "再发", "批准",
    ]

    def compile(self, draft: IntentDraft) -> IntentSpec:
        """Compile IntentDraft → IntentSpec deterministically."""
        # 1. Parse actions
        actions = self._compile_actions(draft.actions)
        action_set = {a.action for a in actions}

        # 2. Parse conditions
        conditions = self._compile_conditions(draft.conditions, action_set)

        # 3. Parse constraints
        constraints = self._compile_constraints(draft.constraints)

        # 4. Determine mode
        mode = self._determine_mode(actions, conditions)

        # 5. Determine goal
        goal = draft.goal or " ".join(draft.actions)[:200]

        return IntentSpec(
            mode=mode,
            goal=goal,
            actions=actions,
            conditions=conditions,
            constraints=constraints,
            target_hint=draft.target_hint,
            confidence=draft.confidence,
            source="L2",
        )

    def _compile_actions(self, raw_actions: list[str]) -> list[IntentAction]:
        """Map free-form action strings to typed IntentAction list."""
        if not raw_actions:
            return []

        results: list[IntentAction] = []
        seen: set[ActionType] = set()

        for text in raw_actions:
            lower = text.lower().strip()
            best_action: ActionType | None = None
            best_resource: ResourceType | None = None

            # Match action type by keyword
            for keyword, atype, rtype in self._ACTION_PATTERNS:
                if keyword in lower:
                    best_action = atype
                    best_resource = rtype
                    break

            if best_action is None:
                # Fallback: try to guess from the text
                if any(w in lower for w in ("new", "新", "建", "创", "写", "生成")):
                    best_action = ActionType.CREATE
                    best_resource = ResourceType.CONTENT
                elif any(w in lower for w in ("改", "修改", "优化", "完善")):
                    best_action = ActionType.UPDATE
                    best_resource = ResourceType.CONTENT
                else:
                    continue  # Can't classify this action

            # Resource hint matching: prefer DRAFT/SCHEDULE over generic CONTENT
            for hint, rtype in self._RESOURCE_HINTS:
                if hint in lower:
                    if best_resource is None or rtype != ResourceType.CONTENT:
                        best_resource = rtype
                    # Don't break — keep scanning for more specific resources

            # Deduplicate by action type (keep the most specific resource)
            if best_action in seen:
                # Update resource if new one is more specific
                for r in results:
                    if r.action == best_action and best_resource is not None:
                        r.resource = best_resource
                continue

            seen.add(best_action)
            results.append(IntentAction(
                action=best_action,
                resource=best_resource,
                confidence=0.9,
            ))

        # Handle UPDATE_OR_CREATE: if we have both update and create in conditions context
        return results

    def _compile_conditions(
        self, raw_conditions: list[str], action_set: set[ActionType],
    ) -> list[IntentCondition]:
        """Parse condition strings into IntentCondition list."""
        if not raw_conditions:
            return []

        results: list[IntentCondition] = []
        for text in raw_conditions:
            lower = text.lower().strip()

            # Detect condition type
            if any(w in lower for w in ("if_exists", "if exists", "exist", "有则", "已有", "存在",
                                          "found", "找到", "有", "existing")):
                cond_type = ConditionType.IF_EXISTS
            elif any(w in lower for w in ("if_not_exists", "not exist", "无则", "没有",
                                            "不存在", "not found", "找不到")):
                cond_type = ConditionType.IF_NOT_EXISTS
            else:
                continue

            # Determine resource
            resource: ResourceType | None = None
            for hint, rtype in self._RESOURCE_HINTS:
                if hint in lower:
                    resource = rtype
                    break
            if resource is None:
                resource = ResourceType.DRAFT  # default

            # Determine then/else actions
            then_action: ActionType | None = None
            else_action: ActionType | None = None

            then_part = ""
            else_part = ""

            # Split "X else Y" or "X 否则 Y" or "X or Y" or "X 没有则 Y"
            if "else" in lower or "否则" in lower or "没有则" in lower or "无则" in lower:
                parts = re.split(r"else|否则|没有则|无则", lower, maxsplit=1)
                then_part = parts[0]
                if len(parts) > 1:
                    else_part = parts[1]
            elif " or " in lower:
                parts = lower.split(" or ", 1)
                then_part = parts[0]
                if len(parts) > 1:
                    else_part = parts[1]

            for keyword, atype, _ in self._ACTION_PATTERNS:
                if keyword in then_part and then_action is None:
                    then_action = atype
                if keyword in else_part and else_action is None:
                    else_action = atype

            # For conditional create: update→update, create→create
            if then_action is None:
                then_action = ActionType.UPDATE
            if else_action is None:
                else_action = ActionType.CREATE

            results.append(IntentCondition(
                type=cond_type,
                resource=resource,
                then_action=then_action,
                else_action=else_action,
            ))

        # If conditions exist, add UPDATE_OR_CREATE action
        if results and ActionType.UPDATE_OR_CREATE not in action_set:
            pass  # caller handles this

        return results

    def _compile_constraints(self, raw_constraints: list[str]) -> list[IntentConstraint]:
        """Parse constraint strings into IntentConstraint list."""
        if not raw_constraints:
            return []

        results: list[IntentConstraint] = []
        for text in raw_constraints:
            lower = text.lower().strip()

            # Check TIME
            if any(kw in lower for kw in self._TIME_KEYWORDS):
                results.append(IntentConstraint(
                    type=ConstraintType.TIME, value=text,
                ))

            # Check APPROVAL
            if any(kw in lower for kw in self._APPROVAL_KEYWORDS):
                results.append(IntentConstraint(
                    type=ConstraintType.APPROVAL, value="BEFORE_PUBLISH",
                ))

        return results

    @staticmethod
    def _determine_mode(
        actions: list[IntentAction], conditions: list[IntentCondition],
    ) -> IntentMode:
        """Derive mode from actions and conditions."""
        if conditions:
            return IntentMode.CONDITIONAL
        if len(actions) >= 2:
            return IntentMode.COMPOSITE
        return IntentMode.SIMPLE
"""Deprecated compatibility implementation.

Do not extend. Migration target: IntentSpec.
"""
