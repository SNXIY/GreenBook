"""Canonical ContextSnapshot builder.

The builder is the only place that joins Conversation, Task, Execution,
Artifact, and Memory projections for a model-facing decision.  Every source
is injected behind a small protocol-shaped object so the core remains usable
with PostgreSQL, test repositories, or a worker process.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from .models import ContextBudget, ContextSnapshot
from .projection import as_dict, project_artifact, project_execution, project_goal, project_task


class ContextBuilder:
    """Build a bounded snapshot from durable fact sources."""

    def __init__(
        self,
        *,
        conversation_source: Any | None = None,
        task_provider: Any | None = None,
        task_manager: Any | None = None,
        execution_repository: Any | None = None,
        artifact_store: Any | None = None,
        memory_retriever: Any | None = None,
        memory_provider: Any | None = None,
        preference_provider: Any | None = None,
        task_scope_factory: Any | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        self._conversation_source = conversation_source
        self._task_provider = task_provider
        self._task_manager = task_manager
        self._execution_repository = execution_repository
        self._artifact_store = artifact_store
        self._memory_retriever = memory_retriever
        self._memory_provider = memory_provider
        self._preference_provider = preference_provider
        self._task_scope_factory = task_scope_factory
        self._budget = budget or ContextBudget()

    async def build(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str = "",
        timezone: str = "Asia/Shanghai",
        session: Any | None = None,
        history: Sequence[Mapping[str, Any]] | None = None,
        current_command: Any | None = None,
        current_goal: Any | None = None,
        target_query: str = "",
    ) -> ContextSnapshot:
        try:
            conversation = await self._load_conversation(
                conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        except LookupError:
            conversation = None
        session_value = getattr(conversation, "session", None) or session or conversation
        conversation_id = str(
            getattr(session_value, "conversation_id", "") or conversation_id
        )
        user_id = str(getattr(session_value, "user_id", "") or user_id)
        tenant_id = str(getattr(session_value, "tenant_id", "") or tenant_id)
        timezone = str(getattr(session_value, "timezone", "") or timezone)

        raw_messages = list(
            history
            or getattr(conversation, "recent_messages", None)
            or []
        )
        messages = _bounded_messages(raw_messages, self._budget)
        summary = (
            getattr(conversation, "summary", None)
            or getattr(session_value, "conversation_summary", None)
            or _mapping_value(conversation, "conversation_summary")
        )

        tasks = await self._load_tasks(
            conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        task_values = [project_task(item) for item in tasks][: self._budget.max_tasks]
        goal_values: list[dict[str, Any]] = []
        artifact_values: list[dict[str, Any]] = []
        resource_values: list[dict[str, Any]] = []
        for task, task_value in zip(tasks, task_values, strict=False):
            task_id = str(task_value.get("task_id", ""))
            for goal in getattr(task, "goals", ()) or ():
                status = str(getattr(goal, "status", "")).upper()
                if status not in {"COMPLETED", "CANCELLED"}:
                    goal_values.append(project_goal(goal, task_id=task_id))
            for artifact in getattr(task, "artifacts", ()) or ():
                artifact_value = project_artifact(artifact, task_id=task_id)
                artifact_values.append(artifact_value)
                if artifact_value.get("resource_id") and artifact_value.get("resource_kind"):
                    resource_values.append({
                        "kind": str(artifact_value["resource_kind"]).upper(),
                        "id": str(artifact_value["resource_id"]),
                        "resource_id": str(artifact_value["resource_id"]),
                        "resource_kind": str(artifact_value["resource_kind"]).upper(),
                        "artifact_id": artifact_value.get("artifact_id"),
                        "task_id": task_id,
                        "label": artifact_value.get("title") or artifact_value.get("summary"),
                        "updated_at": artifact_value.get("created_at"),
                    })
            for resource in getattr(task, "resource_index", ()) or ():
                resource_value = as_dict(resource)
                resource_value["task_id"] = task_id
                resource_values.append(resource_value)

            if self._artifact_store is not None and task_id:
                finder = getattr(self._artifact_store, "find_by_task", None)
                if callable(finder):
                    loaded = finder(task_id)
                    loaded = await loaded if inspect.isawaitable(loaded) else loaded
                    known = {item.get("artifact_id") for item in artifact_values}
                    artifact_values.extend(
                        item for item in (
                            project_artifact(value, task_id=task_id)
                            for value in (loaded or ())
                        ) if item.get("artifact_id") not in known
                    )

        goal_values = goal_values[: self._budget.max_goals]
        artifact_values = artifact_values[: self._budget.max_artifacts]
        resource_values = resource_values[: self._budget.max_resources]

        executions = await self._load_executions({item.get("task_id") for item in task_values})
        execution_values = [project_execution(item) for item in executions]
        operations = _operations(session_value, conversation)
        preferences = await self._load_preferences(user_id)
        recalled = await self._recall(
            user_id=user_id,
            conversation_id=conversation_id,
            command=current_command,
            goal=current_goal,
            target_query=target_query,
        )
        recalled_preferences = [
            {
                "key": item.get("structured_metadata", {}).get("preference_type", ""),
                "value": item.get("structured_metadata", {}).get("value", ""),
                "confidence": item.get("confidence", 0.0),
                "memory_id": item.get("memory_id", ""),
            }
            for item in recalled
            if str(item.get("memory_type", "")) in {"SEMANTIC", "PREFERENCE"}
            and item.get("structured_metadata", {}).get("preference_type")
        ]
        preferences = preferences or recalled_preferences
        preferences = [
            _compact_preference(item) for item in preferences[: self._budget.max_memories]
        ]

        targets = _target_candidates(
            task_values,
            artifact_values,
            resource_values,
            execution_values,
            session_value,
            limit=self._budget.max_target_candidates,
        )
        command_value = as_dict(current_command)
        goal_value = as_dict(current_goal)
        return ContextSnapshot(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone,
            active_task_id=getattr(session_value, "active_task_id", None),
            active_artifact_id=getattr(session_value, "active_artifact_id", None),
            active_draft_id=getattr(session_value, "active_draft_id", None),
            active_post_id=getattr(session_value, "active_post_id", None),
            active_schedule_id=getattr(session_value, "active_schedule_id", None),
            active_execution_id=getattr(session_value, "active_execution_id", None),
            current_command=command_value,
            current_goal=goal_value,
            recent_messages=messages,
            summary=str(summary)[: self._budget.summary_chars] if summary else None,
            active_tasks=task_values,
            unfinished_goals=goal_values,
            task_states=[
                {
                    "task_id": item.get("task_id"),
                    "status": item.get("status"),
                    "active_execution_id": item.get("active_execution_id"),
                    "plan_version": item.get("plan_version", 0),
                }
                for item in task_values
            ],
            recent_operations=[
                _compact_operation(item, self._budget.max_operation_chars)
                for item in operations[: self._budget.max_operations]
            ],
            artifacts=artifact_values,
            execution_states=execution_values,
            available_resources=resource_values,
            target_candidates=targets,
            user_preferences=preferences,
            recalled_memories=[
                _compact_memory(item, self._budget.max_memory_chars)
                for item in recalled[: self._budget.max_memories]
            ],
            memory_ids_used=[
                str(item.get("memory_id"))
                for item in recalled[: self._budget.max_memories]
                if item.get("memory_id")
            ],
            plan_version=max(
                [int(item.get("plan_version", 0) or 0) for item in task_values] or [0]
            ),
        )

    async def _load_conversation(self, conversation_id: str, *, user_id: str, tenant_id: str) -> Any:
        source = self._conversation_source
        if source is None:
            return None
        loader = getattr(source, "load", None)
        if not callable(loader):
            return source
        try:
            value = loader(conversation_id, user_id=user_id, tenant_id=tenant_id)
        except TypeError:
            value = loader(conversation_id)
        return await value if inspect.isawaitable(value) else value

    async def _load_tasks(self, conversation_id: str, *, user_id: str, tenant_id: str) -> list[Any]:
        source = self._task_provider
        if source is not None:
            finder = getattr(source, "list_tasks", None)
            if callable(finder):
                scope_factory = self._task_scope_factory
                scope = (
                    scope_factory(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                    )
                    if callable(scope_factory)
                    else type("Scope", (), {
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "tenant_id": tenant_id,
                    })()
                )
                try:
                    value = finder(scope)
                except TypeError:
                    value = finder(conversation_id)
                return list(await value if inspect.isawaitable(value) else value)
        manager = self._task_manager
        finder = getattr(manager, "get_active_tasks", None)
        if callable(finder):
            value = finder(conversation_id, user_id=user_id, tenant_id=tenant_id)
            return list(await value if inspect.isawaitable(value) else value)
        return []

    async def _load_executions(self, task_ids: set[Any]) -> list[Any]:
        source = self._execution_repository
        finder = getattr(source, "list_all", None)
        if not callable(finder):
            return []
        value = finder()
        values = list(await value if inspect.isawaitable(value) else value)
        normalized = {str(item) for item in task_ids if item}
        return [item for item in values if str(getattr(item, "task_id", "")) in normalized]

    async def _load_preferences(self, user_id: str) -> list[dict[str, Any]]:
        provider = self._preference_provider
        if provider is None:
            return []
        loader = getattr(provider, "list_preferences", None)
        if not callable(loader):
            return []
        value = loader(user_id=user_id)
        values = await value if inspect.isawaitable(value) else value
        return [as_dict(item) for item in (values or ())]

    async def _recall(self, **kwargs: Any) -> list[dict[str, Any]]:
        provider = self._memory_retriever or self._memory_provider
        if provider is None or self._budget.max_memories == 0:
            return []
        retrieve = getattr(provider, "retrieve", None)
        if not callable(retrieve):
            return []
        try:
            value = retrieve(limit=self._budget.max_memories, **kwargs)
        except TypeError:
            value = retrieve(user_id=kwargs["user_id"], limit=self._budget.max_memories)
        values = await value if inspect.isawaitable(value) else value
        return [as_dict(item) for item in (values or ())]


def _bounded_messages(values: Sequence[Mapping[str, Any]], budget: ContextBudget) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    chars = 0
    for raw in reversed(list(values)):
        if len(selected) >= budget.recent_message_limit:
            break
        item = dict(raw)
        content = str(item.get("content", ""))
        remaining = budget.recent_message_chars - chars
        if remaining <= 0:
            break
        item["content"] = content[:remaining]
        selected.append(item)
        chars += len(item["content"])
    return list(reversed(selected))


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _operations(session: Any, conversation: Any) -> list[dict[str, Any]]:
    for value in (
        getattr(session, "recent_tool_calls", None),
        getattr(conversation, "recent_operations", None),
        _mapping_value(conversation, "recent_tool_calls"),
    ):
        if value:
            return [as_dict(item) for item in value]
    return []


def _compact_operation(value: Mapping[str, Any], limit: int) -> dict[str, Any]:
    """Keep correlation and outcome facts without embedding tool payloads."""

    return {
        key: _bounded_text(item, limit)
        if key in {"arguments", "result", "message", "error_message"}
        else item
        for key, item in value.items()
        if key in {
            "tool_name",
            "tool_call_id",
            "run_id",
            "execution_id",
            "status",
            "code",
            "error_code",
            "arguments",
            "result",
            "message",
            "error_message",
            "created_at",
            "updated_at",
        }
    }


def _compact_memory(value: Mapping[str, Any], limit: int) -> dict[str, Any]:
    """Project recalled memory as bounded evidence, never as an article body."""

    result = {
        key: item
        for key, item in value.items()
        if key in {
            "memory_id",
            "memory_type",
            "conversation_id",
            "task_id",
            "importance",
            "confidence",
            "source_type",
            "source_id",
            "created_at",
            "updated_at",
            "last_accessed_at",
            "access_count",
            "expires_at",
        }
    }
    result["content"] = _bounded_text(value.get("content"), limit)
    metadata = value.get("structured_metadata")
    if isinstance(metadata, Mapping):
        result["structured_metadata"] = {
            str(key): _bounded_text(item, limit // 2)
            for key, item in list(metadata.items())[:20]
            if key not in {"body", "content", "embedding", "raw_result"}
        }
    return result


def _compact_preference(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _bounded_text(item, 800)
        for key, item in value.items()
        if key not in {"embedding", "body", "content", "raw_result"}
    }


def _bounded_text(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_text(item, max(100, limit // 4))
            for key, item in list(value.items())[:20]
            if str(key) not in {"body", "content", "embedding", "raw_result"}
        }
    if isinstance(value, list):
        return [_bounded_text(item, max(100, limit // 4)) for item in value[:20]]
    return value


def _target_candidates(
    tasks: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    resources: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    session: Any,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    values.extend({**dict(item), "kind": "TASK"} for item in tasks)
    values.extend({**dict(item), "kind": "ARTIFACT"} for item in artifacts)
    values.extend(dict(item) for item in resources)
    values.extend(dict(item) for item in executions)
    for kind, field in (
        ("TASK", "active_task_id"),
        ("ARTIFACT", "active_artifact_id"),
        ("DRAFT", "active_draft_id"),
        ("POST", "active_post_id"),
        ("SCHEDULE", "active_schedule_id"),
        ("EXECUTION", "active_execution_id"),
    ):
        identifier = getattr(session, field, None)
        if identifier:
            values.append({"id": str(identifier), "resource_id": str(identifier), "kind": kind})
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in values:
        kind = str(item.get("kind", "TASK")).upper()
        preferred = {
            "TASK": ("id", "task_id", "resource_id"),
            "ARTIFACT": ("id", "artifact_id", "resource_id", "task_id"),
            "EXECUTION": ("id", "execution_id", "resource_id", "task_id"),
        }.get(kind, ("id", "resource_id", "task_id", "artifact_id", "execution_id"))
        identifier = next((str(item.get(key)) for key in preferred if item.get(key)), "")
        if not identifier:
            continue
        key = (str(item.get("kind", "TASK")), identifier)
        if key in seen:
            continue
        seen.add(key)
        result.append({**item, "id": identifier})
    return result[:limit]


__all__ = ["ContextBuilder"]
