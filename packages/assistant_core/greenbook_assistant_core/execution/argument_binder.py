"""Schema-driven binding from intent/plan steps to tool arguments."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.orchestration.context import PlanningContext
from greenbook_assistant_core.orchestration.models import PlanStep
from greenbook_assistant_core.task.intent_models import IntentSpec

from .temporal_resolver import TemporalResolver

ToolArguments = dict[str, Any]
ToolSchemaSource = (
    Mapping[str, Any]
    | Sequence[Mapping[str, Any]]
    | Callable[[str], Mapping[str, Any] | None]
)


class ArgumentBinder:
    """Bind a ``PlanStep`` using the selected tool's exported schema.

    The binder deliberately knows capabilities and field semantics, but not
    individual MCP tool names.  Tool names are resolved through the capability
    registry and field availability comes from the tool schema.  This keeps
    the binding layer usable with both the in-process MCP server and test
    doubles that expose OpenAI-style tool definitions.
    """

    def __init__(
        self,
        tool_schemas: ToolSchemaSource | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
    ) -> None:
        self._tool_schemas = tool_schemas
        self._registry = registry or CapabilityRegistry()
        self._timezone = timezone
        self._temporal = TemporalResolver(now=now)

    def bind(
        self,
        step: PlanStep,
        planning_context: PlanningContext | None = None,
        intent_spec: IntentSpec | None = None,
        *,
        user_message: str | None = None,
        timezone: str | None = None,
        active_draft_id: str | None = None,
        active_schedule_id: str | None = None,
    ) -> ToolArguments:
        """Return arguments accepted by the tool selected for ``step``."""

        tool_name = self._tool_name(step)
        schema = self._schema_for(tool_name)
        properties, required = self._schema_fields(step, schema)

        goal, constraints = self._intent_text_and_constraints(
            planning_context,
            intent_spec,
            user_message=user_message,
            step_description=step.description,
        )
        effective_timezone = timezone or self._timezone
        existing = dict(step.constraints or {})
        temporal = self._temporal.resolve(
            goal,
            constraints=[*constraints, existing],
            timezone=effective_timezone,
        )

        values = self._semantic_values(
            goal=goal,
            temporal=temporal,
            timezone=effective_timezone,
            existing=existing,
            active_draft_id=active_draft_id,
            active_schedule_id=active_schedule_id,
        )

        # Only schema fields may cross the MCP boundary.  This removes planner
        # metadata such as ``time``/``approval`` before Pydantic validation.
        arguments: ToolArguments = {
            key: value
            for key, value in existing.items()
            if key in properties and value is not None and value != ""
        }
        for field, field_schema in properties.items():
            if field in arguments:
                continue
            value = self._value_for_field(
                field,
                values,
                goal,
                field_schema=field_schema,
                allow_untyped=field in required,
            )
            if value is not None and value != "":
                arguments[field] = value

        # A schema may be intentionally minimal in a test double.  Required
        # fields still get a generic goal value rather than an empty argument.
        for field in required:
            if field not in arguments:
                value = self._value_for_field(
                    field,
                    values,
                    goal,
                    field_schema=properties.get(field, {}),
                    allow_untyped=True,
                )
                if value is not None and value != "":
                    arguments[field] = value
        return arguments

    def bind_plan(
        self,
        plan: Any,
        planning_context: PlanningContext | None = None,
        intent_spec: IntentSpec | None = None,
        *,
        user_message: str | None = None,
        timezone: str | None = None,
        active_draft_id: str | None = None,
        active_schedule_id: str | None = None,
    ) -> Any:
        """Bind known arguments into every step before execution starts."""

        for step in plan.steps:
            bound = self.bind(
                step,
                planning_context,
                intent_spec,
                user_message=user_message,
                timezone=timezone,
                active_draft_id=active_draft_id,
                active_schedule_id=active_schedule_id,
            )
            # Replace planner metadata with the concrete tool arguments.  In
            # particular, ``time``/``approval`` are intent-level fields; the
            # schedule step must carry canonical ``run_at`` instead.
            step.constraints = bound
        return plan

    def _tool_name(self, step: PlanStep) -> str:
        capability = self._registry.get(step.capability)
        if capability and capability.tools:
            return capability.tools[0]
        return step.capability

    def _schema_for(self, tool_name: str) -> Mapping[str, Any] | None:
        source = self._tool_schemas
        if source is None:
            return None
        raw: Any = None
        if callable(source) and not isinstance(source, type):
            raw = source(tool_name)
        elif isinstance(source, Mapping):
            raw = source.get(tool_name) or source.get(tool_name.replace(".", "_"))
        else:
            for item in source:
                if not isinstance(item, Mapping):
                    continue
                candidate = item.get("function", item)
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("name")
                    in {tool_name, tool_name.replace(".", "_")}
                ):
                    raw = candidate
                    break
        if not isinstance(raw, Mapping):
            return None
        function = raw.get("function")
        if isinstance(function, Mapping):
            raw = function
        parameters = raw.get("parameters")
        if isinstance(parameters, Mapping):
            return parameters
        return raw

    def _schema_fields(
        self,
        step: PlanStep,
        schema: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Mapping[str, Any] | None], list[str]]:
        properties = schema.get("properties") if schema else None
        if isinstance(properties, Mapping) and properties:
            property_map = {
                str(name): value if isinstance(value, Mapping) else {}
                for name, value in properties.items()
            }
            required = [
                str(name)
                for name in schema.get("required", [])
                if str(name) in property_map
            ]
            return property_map, required

        capability = self._registry.get(step.capability)
        if capability is None:
            return {str(name): None for name in step.constraints}, []
        names = list(dict.fromkeys(capability.inputs.required + capability.inputs.optional))
        return {name: None for name in names}, list(capability.inputs.required)

    @staticmethod
    def _intent_text_and_constraints(
        planning_context: PlanningContext | None,
        intent_spec: IntentSpec | None,
        *,
        user_message: str | None,
        step_description: str = "",
    ) -> tuple[str, list[Any]]:
        spec = intent_spec or (planning_context.intent_spec if planning_context else None)
        task_intent = planning_context.task_intent if planning_context else None
        goal = str(
            user_message
            or (spec.goal if spec and spec.goal else "")
            or getattr(task_intent, "goal", "")
            or step_description
        ).strip()
        constraints: list[Any] = []
        if spec is not None:
            constraints.extend(spec.constraints)
        if task_intent is not None:
            constraints.extend(getattr(task_intent, "constraints", []) or [])
        return goal, constraints

    @staticmethod
    def _semantic_values(
        *,
        goal: str,
        temporal: str | None,
        timezone: str,
        existing: Mapping[str, Any],
        active_draft_id: str | None,
        active_schedule_id: str | None,
    ) -> dict[str, Any]:
        subject = str(
            existing.get("topic")
            or existing.get("subject")
            or existing.get("keyword")
            or _extract_subject(goal)
        ).strip()
        content = _content_brief(goal, subject)
        values: dict[str, Any] = {
            "title": _title_for(goal, subject),
            "content": content,
            "instruction": content,
            "revision_instruction": content,
            "body": content,
            "body_markdown": content,
            "summary": content[:200],
            "description": content[:200],
            "topic": subject,
            "query": subject or goal,
            "keyword": subject or goal,
            "keywords": [subject] if subject else [],
            "run_at": temporal,
            "publish_at": temporal,
            "timezone": timezone,
            "timezone_name": timezone,
            "draft_id": existing.get("draft_id") or active_draft_id,
            "schedule_id": existing.get("schedule_id") or active_schedule_id,
            "post_id": existing.get("post_id"),
            "content_id": existing.get("content_id"),
        }
        return values

    @staticmethod
    def _value_for_field(
        field: str,
        values: Mapping[str, Any],
        goal: str,
        *,
        field_schema: Mapping[str, Any] | None = None,
        allow_untyped: bool = False,
    ) -> Any:
        if field in values:
            return values[field]
        normalized = field.lower().replace("-", "_")
        if normalized in values:
            return values[normalized]
        if normalized.endswith("_id"):
            return None
        # Only fill unknown fields when the exported schema accepts a string.
        # Optional arrays/objects (for example ``references`` or pagination
        # metadata) must remain absent instead of receiving the whole goal.
        if field_schema is None and not allow_untyped:
            return None
        if not _schema_accepts_string(field_schema):
            return None
        return goal if goal else None


def _schema_accepts_string(schema: Mapping[str, Any] | None) -> bool:
    if not schema:
        return True
    schema_type = schema.get("type")
    if schema_type == "string":
        return True
    if schema_type is not None:
        if isinstance(schema_type, list):
            return "string" in schema_type
        return False
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, Sequence):
            return any(
                isinstance(variant, Mapping) and _schema_accepts_string(variant)
                for variant in variants
            )
    return True


def _extract_subject(goal: str) -> str:
    text = goal.strip()
    text = re.split(r"[，,。；;]|然后|之后|同时|并且|再", text, maxsplit=1)[0]
    text = re.sub(r"^(?:请|帮我|请帮我|麻烦|我想|想要)\s*", "", text)
    # Remove a leading relative schedule phrase before extracting the topic.
    # The same user sentence may describe both content and publication time.
    text = re.sub(
        r"^(?:今天|明天|后天)[^，,。；;]*?(?:发布|发)\s*",
        "",
        text,
    )
    text = re.sub(r"^(?:一篇|一则|一个|个)\s*", "", text)
    text = re.sub(r"^(?:写|创建|创作|生成|发布|发|来|做|搞)(?:一篇|一则|一个|个)?", "", text)
    text = re.sub(r"^(?:关于|有关|主题是)\s*", "", text)
    text = re.sub(r"(?:文章|帖子|内容|草稿)\s*$", "", text)
    text = re.sub(r"\s*的\s*$", "", text)
    text = re.sub(r"学习\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:、")
    return text or "GreenBook"


def _title_for(goal: str, subject: str) -> str:
    if "如何学好" in goal:
        topic = subject.replace(" ", "").strip(" 的")
        if topic.startswith("如何学好"):
            return topic[:256]
        if topic and topic != "GreenBook":
            return f"如何学好{topic}"[:256]
    if "学习" in goal:
        return f"如何学好{subject.replace(' ', '')}"[:256]
    return subject[:256]


def _content_brief(goal: str, subject: str) -> str:
    if "学习" in goal or "学好" in goal:
        return f"根据用户目标生成{subject}学习路线文章：覆盖核心概念、实践路径和常见问题。"
    return f"根据用户目标生成{subject}主题文章：{goal}"[:12000]


__all__ = ["ArgumentBinder", "ToolArguments"]
