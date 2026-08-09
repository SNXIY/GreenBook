"""Phase 6.8.1 Stage D-B — IntentElements: structured intermediate representation.

IntentElements uses structured {verb, object} pairs that the LLM can output
reliably, while IntentSpecBuilder uses clean mapping tables (not ad-hoc keyword
matching) to convert to IntentSpec.
"""

from __future__ import annotations

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
# IntentElements — LLM output target
# ═══════════════════════════════════════════════════════════════════════

class ActionMention(BaseModel):
    """A single action the user mentioned."""
    verb: str = ""       # "search", "create", "update", "publish", etc.
    object: str = ""     # "community posts", "article", "draft", etc.


class ConditionMention(BaseModel):
    """A conditional branch the user mentioned."""
    text: str = ""       # "if draft exists update else create"


class IntentElements(BaseModel):
    """LLM extracts these structured elements from user message."""

    goal: str = ""
    action_mentions: list[ActionMention] = []
    condition_mentions: list[ConditionMention] = []
    constraint_mentions: list[str] = []
    target_hint: str | None = None
    confidence: float = 0.9


# ═══════════════════════════════════════════════════════════════════════
# IntentSpecBuilder — clean mapping tables (not keyword matching)
# ═══════════════════════════════════════════════════════════════════════

class IntentSpecBuilder:
    """Deterministic Elements → IntentSpec using explicit verb/object mapping."""

    # Clean verb → ActionType mapping (not substring matching)
    _VERB_TO_ACTION: dict[str, ActionType] = {
        # SEARCH
        "search": ActionType.SEARCH, "find": ActionType.SEARCH,
        "lookup": ActionType.SEARCH, "look_up": ActionType.SEARCH,
        # CREATE
        "create": ActionType.CREATE, "write": ActionType.CREATE,
        "generate": ActionType.CREATE, "compose": ActionType.CREATE,
        "draft": ActionType.CREATE, "make": ActionType.CREATE,
        # UPDATE
        "update": ActionType.UPDATE, "edit": ActionType.UPDATE,
        "modify": ActionType.UPDATE, "improve": ActionType.UPDATE,
        "optimize": ActionType.UPDATE, "revise": ActionType.UPDATE,
        "enhance": ActionType.UPDATE, "refine": ActionType.UPDATE,
        "polish": ActionType.UPDATE, "fix": ActionType.UPDATE,
        "adjust": ActionType.UPDATE, "change": ActionType.UPDATE,
        # PUBLISH
        "publish": ActionType.PUBLISH, "post": ActionType.PUBLISH,
        "release": ActionType.PUBLISH, "schedule": ActionType.PUBLISH,
        "send": ActionType.PUBLISH, "share": ActionType.PUBLISH,
        # DELETE
        "delete": ActionType.DELETE, "cancel": ActionType.DELETE,
        "remove": ActionType.DELETE, "undo": ActionType.DELETE,
        "revoke": ActionType.DELETE,
        # ANALYZE
        "analyze": ActionType.ANALYZE, "analyse": ActionType.ANALYZE,
        "summarize": ActionType.ANALYZE, "summarise": ActionType.ANALYZE,
        "review": ActionType.ANALYZE, "evaluate": ActionType.ANALYZE,
        "assess": ActionType.ANALYZE,
        # QUERY
        "query": ActionType.QUERY, "view": ActionType.QUERY,
        "check": ActionType.QUERY, "list": ActionType.QUERY,
        "show": ActionType.QUERY, "get": ActionType.QUERY,
        "read": ActionType.QUERY, "see": ActionType.QUERY,
        "inspect": ActionType.QUERY,
    }

    # Object term → ResourceType mapping
    _OBJECT_TO_RESOURCE: dict[str, ResourceType] = {
        "post": ResourceType.POST, "posts": ResourceType.POST,
        "article": ResourceType.CONTENT, "articles": ResourceType.CONTENT,
        "content": ResourceType.CONTENT,
        "draft": ResourceType.DRAFT, "drafts": ResourceType.DRAFT,
        "schedule": ResourceType.SCHEDULE,
        "task": ResourceType.TASK, "tasks": ResourceType.TASK,
    }

    # Known condition verbs for then/else detection
    _CONDITION_THEN_VERBS = {
        "update", "edit", "modify", "improve", "optimize", "revise",
        "enhance", "refine", "polish", "publish", "post", "release",
    }
    _CONDITION_ELSE_VERBS = {
        "create", "write", "generate", "compose", "draft", "make",
    }

    def build(self, elements: IntentElements) -> IntentSpec:
        """Build IntentSpec from IntentElements deterministically."""
        # 1. Map action mentions to typed actions
        actions = self._build_actions(elements.action_mentions)
        action_types = {a.action for a in actions}

        # 2. Parse condition mentions
        conditions = self._build_conditions(
            elements.condition_mentions, action_types,
        )

        # 3. Parse constraint mentions
        constraints = self._build_constraints(elements.constraint_mentions)

        # 4. Determine mode
        mode = self._determine_mode(actions, conditions)

        return IntentSpec(
            mode=mode,
            goal=elements.goal,
            actions=actions,
            conditions=conditions,
            constraints=constraints,
            target_hint=elements.target_hint,
            confidence=elements.confidence,
            source="L2",
        )

    def _build_actions(self, mentions: list[ActionMention]) -> list[IntentAction]:
        """Map verb+object pairs to IntentAction list."""
        seen: set[ActionType] = set()
        results: list[IntentAction] = []

        for m in mentions:
            verb = m.verb.strip().lower()
            obj = m.object.strip().lower()

            # Map verb → ActionType
            action_type = self._VERB_TO_ACTION.get(verb)
            if action_type is None:
                continue  # Unknown verb, skip

            # Map object → ResourceType
            resource_type = self._infer_resource(obj)

            # Deduplicate: skip if same action type already seen
            if action_type in seen:
                continue
            seen.add(action_type)

            results.append(IntentAction(
                action=action_type,
                resource=resource_type,
                confidence=0.9,
            ))

        return results

    def _infer_resource(self, obj: str) -> ResourceType | None:
        """Infer ResourceType from object text using clean token matching."""
        if not obj:
            return None
        # Check for exact token matches
        tokens = obj.lower().split()
        for token in tokens:
            if token in self._OBJECT_TO_RESOURCE:
                return self._OBJECT_TO_RESOURCE[token]
        # Check for compound matches (e.g., "community posts" → POST)
        for key, rtype in self._OBJECT_TO_RESOURCE.items():
            if key in obj:
                return rtype
        return None

    def _build_conditions(
        self,
        mentions: list[ConditionMention],
        action_types: set[ActionType],
    ) -> list[IntentCondition]:
        """Parse condition mentions into IntentCondition list."""
        results: list[IntentCondition] = []

        for m in mentions:
            text = m.text.strip().lower()

            # Detect condition type
            if any(w in text for w in ("exist", "found", "已有", "有", "存在")):
                cond_type = ConditionType.IF_EXISTS
            elif any(w in text for w in ("not exist", "not found", "没有", "无", "不存在")):
                cond_type = ConditionType.IF_NOT_EXISTS
            else:
                continue

            # Detect resource from condition text
            resource: ResourceType | None = None
            for key, rtype in self._OBJECT_TO_RESOURCE.items():
                if key in text:
                    resource = rtype
                    break

            # Detect then/else actions from verb tokens
            then_action: ActionType | None = None
            else_action: ActionType | None = None

            # Split on "else" or equivalent
            parts = text.split(" else ") if " else " in text else [text]
            if len(parts) == 2:
                then_part, else_part = parts[0], parts[1]
            elif "otherwise" in text:
                parts = text.split(" otherwise ")
                then_part, else_part = parts[0], parts[1]
            else:
                then_part, else_part = text, ""

            # Extract verbs from then_part
            for verb in then_part.split():
                verb_clean = verb.strip(".,;:")
                if verb_clean in self._CONDITION_THEN_VERBS:
                    then_action = self._VERB_TO_ACTION.get(verb_clean)
                    break

            # Extract verbs from else_part
            for verb in else_part.split():
                verb_clean = verb.strip(".,;:")
                if verb_clean in self._CONDITION_ELSE_VERBS:
                    else_action = self._VERB_TO_ACTION.get(verb_clean)
                    break

            results.append(IntentCondition(
                type=cond_type,
                resource=resource,
                then_action=then_action or ActionType.UPDATE,
                else_action=else_action or ActionType.CREATE,
            ))

        return results

    def _build_constraints(self, mentions: list[str]) -> list[IntentConstraint]:
        """Parse constraint mentions into typed constraints."""
        results: list[IntentConstraint] = []

        for text in mentions:
            lower = text.lower().strip()

            # TIME detection: explicit time-related words
            has_time = any(w in lower for w in (
                "minute", "hour", "day", "tomorrow", "today", "tonight",
                "morning", "evening", "afternoon", "week", "month",
                "分钟", "小时", "天", "明天", "今天", "晚上", "早上",
                "上午", "下午",
            ))
            if has_time:
                results.append(IntentConstraint(
                    type=ConstraintType.TIME, value=text,
                ))

            # APPROVAL detection: explicit approval-related words
            has_approval = any(w in lower for w in (
                "approve", "approval", "confirm", "review", "check",
                "verify", "确认", "审核", "审阅", "批准", "通过",
            ))
            if has_approval:
                results.append(IntentConstraint(
                    type=ConstraintType.APPROVAL, value="BEFORE_PUBLISH",
                ))

        return results

    @staticmethod
    def _determine_mode(
        actions: list[IntentAction],
        conditions: list[IntentCondition],
    ) -> IntentMode:
        if conditions:
            return IntentMode.CONDITIONAL
        if len(actions) >= 2:
            return IntentMode.COMPOSITE
        return IntentMode.SIMPLE
"""Deprecated compatibility implementation.

Do not extend. Migration target: IntentSpec.
"""
