"""Phase 6.8.1 — IntentSpec: Action x Resource x Condition decomposition.

IntentSpec 只表达用户意图，不生成执行计划。
禁止字段: seq, step_id, depends_on, parallel, tool — 这些属于 Planner 职责。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────────────

class IntentMode(StrEnum):
    SIMPLE = "SIMPLE"           # single action, no conditions
    COMPOSITE = "COMPOSITE"     # multiple actions, single goal
    CONDITIONAL = "CONDITIONAL" # actions with conditions


class ActionType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    QUERY = "QUERY"
    SEARCH = "SEARCH"
    ANALYZE = "ANALYZE"
    PUBLISH = "PUBLISH"
    UPDATE_OR_CREATE = "UPDATE_OR_CREATE"


class ResourceType(StrEnum):
    CONTENT = "CONTENT"       # 文章/帖子正文
    DRAFT = "DRAFT"           # 草稿
    SCHEDULE = "SCHEDULE"     # 定时发布
    POST = "POST"             # 已发布帖子
    TASK = "TASK"             # 抽象任务


class ConditionType(StrEnum):
    IF_EXISTS = "IF_EXISTS"
    IF_NOT_EXISTS = "IF_NOT_EXISTS"


class ConstraintType(StrEnum):
    TIME = "TIME"
    APPROVAL = "APPROVAL"
    USER_INPUT = "USER_INPUT"


# ── Core types ──────────────────────────────────────────────────────────

class IntentAction(BaseModel):
    """用户想做的一个动作."""
    action: ActionType
    resource: ResourceType | None = None
    confidence: float = 0.0


class IntentCondition(BaseModel):
    """条件分支."""
    type: ConditionType
    resource: ResourceType | None = None
    then_action: ActionType | None = None
    else_action: ActionType | None = None


class IntentConstraint(BaseModel):
    """执行约束."""
    type: ConstraintType
    value: str = ""


class IntentSpec(BaseModel):
    """用户意图的完整结构化表示.

    不包含任何执行计划信息 (无 seq, depends_on, step_id).
    这些由 Orchestrator 从 actions + conditions 推导.
    """

    mode: IntentMode = IntentMode.SIMPLE
    goal: str = ""

    actions: list[IntentAction] = []
    conditions: list[IntentCondition] = []
    constraints: list[IntentConstraint] = []

    target_hint: str | None = None
    confidence: float = 0.0
    source: str = "L1"


class IntentValidationIssue(BaseModel):
    """A machine-readable IntentSpec validation issue."""

    type: str
    message: str
    expected_fields: list[str] = []
    suggestion: list[str] = []


class IntentValidationResult(BaseModel):
    """IntentValidator 的输出."""

    is_valid: bool = True
    needs_repair: bool = False
    warnings: list[str] = []
    errors: list[str] = []
    suggested_fixes: list[str] = []
    issues: list[IntentValidationIssue] = []
