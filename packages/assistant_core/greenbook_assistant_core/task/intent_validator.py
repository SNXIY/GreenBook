"""Phase 6.8.1 — IntentValidator: consistency checks for IntentSpec.

Validator 不重新理解用户消息。
Validator 只检查 IntentSpec 内部一致性 + 与原文的结构一致性。
"""

from __future__ import annotations

import re

from .intent_models import (
    ActionType,
    ConstraintType,
    IntentCondition,
    IntentMode,
    IntentSpec,
    IntentValidationIssue,
    IntentValidationResult,
    ResourceType,
)


class IntentValidator:
    """Check IntentSpec for internal consistency and structural alignment."""

    def validate(self, spec: IntentSpec, original_text: str) -> IntentValidationResult:
        result = IntentValidationResult()

        # Rule 1: CONDITIONAL mode → must have conditions
        if spec.mode == IntentMode.CONDITIONAL and not spec.conditions:
            result.needs_repair = True
            result.errors.append("CONDITIONAL mode but no conditions defined")

        # Rule 2: conditional text exists but mode is SIMPLE
        if self._has_conditional_text(original_text) and spec.mode == IntentMode.SIMPLE:
            result.needs_repair = True
            result.errors.append("Text has conditional signals but mode is SIMPLE")
            result.suggested_fixes.append("Set mode=CONDITIONAL and add conditions")

        # Rule 3: "发布时间" in text → UPDATE should target SCHEDULE, not CONTENT
        if self._has_schedule_time_text(original_text):
            for a in spec.actions:
                if a.action == ActionType.UPDATE and a.resource == ResourceType.CONTENT:
                    result.needs_repair = True
                    result.suggested_fixes.append(
                        "Text mentions schedule time → UPDATE should target SCHEDULE"
                    )

        # Rule 4: "发布前确认/审核" → should have PUBLISH + APPROVAL constraint
        if self._has_approval_text(original_text):
            has_publish = any(a.action == ActionType.PUBLISH for a in spec.actions)
            has_approval = any(
                c.type == ConstraintType.APPROVAL for c in spec.constraints
            )
            if not has_publish or not has_approval:
                result.needs_repair = True
                result.errors.append(
                    "Text asks for approval before publish but missing PUBLISH action or APPROVAL constraint"
                )
                if not has_publish:
                    result.suggested_fixes.append("Add PUBLISH action")
                if not has_approval:
                    result.suggested_fixes.append("Add APPROVAL constraint")

        # Rule 5: UPDATE_OR_CREATE action must have a corresponding condition
        has_upsert = any(a.action == ActionType.UPDATE_OR_CREATE for a in spec.actions)
        if has_upsert and not spec.conditions:
            result.needs_repair = True
            result.errors.append(
                "UPDATE_OR_CREATE action requires a condition (IF_EXISTS/IF_NOT_EXISTS)"
            )
            result.suggested_fixes.append(
                "Add condition: IF_EXISTS → UPDATE, IF_NOT_EXISTS → CREATE"
            )

        # Rule 6: ALL modes should have at least one action
        if not spec.actions:
            result.needs_repair = True
            result.errors.append("Empty actions — must have at least one action")
            result.suggested_fixes.append("Add at least one action based on user message")

        # Rule 7: non-SIMPLE mode should have actions
        if spec.mode != IntentMode.SIMPLE and not spec.actions:
            result.needs_repair = True
            result.errors.append(f"{spec.mode.value} mode with empty actions")

        # Rule 7: low confidence warning
        if spec.confidence < 0.3:
            result.warnings.append("Very low confidence — consider L1 fallback")

        if self._has_time_constraint_text(original_text):
            has_time = any(c.type == ConstraintType.TIME for c in spec.constraints)
            if not has_time:
                result.needs_repair = True
                result.errors.append("Missing TIME constraint")
                result.suggested_fixes.append("Add TIME constraint")

        self._attach_structured_issues(result)
        result.is_valid = not result.needs_repair
        return result

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _attach_structured_issues(result: IntentValidationResult) -> None:
        """Map legacy error strings to stable machine-readable issue codes."""
        existing = {issue.type for issue in result.issues}

        def add(issue_type: str, message: str, fields: list[str], suggestion: list[str]) -> None:
            if issue_type in existing:
                return
            result.issues.append(IntentValidationIssue(
                type=issue_type,
                message=message,
                expected_fields=fields,
                suggestion=suggestion,
            ))
            existing.add(issue_type)

        for error in result.errors:
            lower = error.lower()
            if "empty actions" in lower:
                add("EMPTY_ACTIONS", "IntentSpec requires at least one action",
                    ["actions"], ["ADD_ACTION_FROM_MESSAGE"])
            elif "conditional mode" in lower and "no conditions" in lower:
                add("MISSING_CONDITION", error, ["conditions"],
                    ["IF_EXISTS", "then_action", "else_action"])
            elif "conditional signals" in lower:
                add("CONDITIONAL_MODE_MISMATCH", error, ["mode", "conditions"],
                    ["CONDITIONAL"])
            elif "approval before publish" in lower:
                if "approval" in lower:
                    add("MISSING_APPROVAL", error, ["constraints"],
                        ["APPROVAL", "BEFORE_PUBLISH"])
                if "publish" in lower:
                    add("MISSING_PUBLISH_ACTION", error, ["actions"], ["PUBLISH"])
            elif "update_or_create" in lower and "condition" in lower:
                add("MISSING_CONDITION", error, ["conditions"],
                    ["IF_EXISTS", "UPDATE", "CREATE"])
            elif "missing time" in lower:
                add("MISSING_TIME_CONSTRAINT", error, ["constraints"], ["TIME"])

        if any("schedule time" in fix.lower() for fix in result.suggested_fixes):
            add("SCHEDULE_RESOURCE_MISMATCH",
                "Schedule-time update must target SCHEDULE",
                ["actions[].resource"], ["SCHEDULE"])

    @staticmethod
    def _has_conditional_text(text: str) -> bool:
        return bool(re.search(r"如果|否则|有则|无则|要是|假如", text))

    @staticmethod
    def _has_schedule_time_text(text: str) -> bool:
        return bool(re.search(r"发布时间|定时发布|改时间|延后发布|几点发", text))

    @staticmethod
    def _has_time_constraint_text(text: str) -> bool:
        return IntentValidator._has_schedule_time_text(text) or bool(re.search(
            r"\d+\s*(?:minutes?|hours?|days?|\u5206\u949f|\u5c0f\u65f6|\u5929)\s*"
            r"(?:after|later|from now|\u540e|\u4e4b\u540e)?|"
            r"\u660e\u5929|\u4eca\u5929|\u4e0a\u5348|\u4e0b\u5348|\u665a\u4e0a|"
            r"tomorrow|tonight|\b\d{1,2}:\d{2}\b",
            text,
            re.IGNORECASE,
        ))

    @staticmethod
    def _has_approval_text(text: str) -> bool:
        return bool(re.search(
            r"发布.*(?:前|之前).*(?:确认|审核|审|看)|"
            r"(?:确认|审核).*(?:后|之后|再).*发布|"
            r"让我.*(?:确认|审核|审一下|看一下)",
            text,
        ))
