"""Typed submission boundary from Agent decisions to Reliable Execution."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionSubmissionService(Protocol):
    """Submit compiled work without exposing Worker/Queue internals to AgentLoop."""

    async def submit(self, *, graph: Any, plan: Any, state: Any) -> Mapping[str, Any]: ...


class QueueExecutionSubmissionService:
    """Typed bridge from AgentLoop to the queue-owning Runtime service.

    The callable is the already-composed ``submit_plan`` method of
    ``RuntimeAgentService``. This class owns no execution policy and does not
    execute a step; it preserves the typed submission boundary while keeping
    queue allocation in the Reliable Execution layer.
    """

    def __init__(self, callback: Callable[..., Any]) -> None:
        self._callback = callback

    async def submit(self, *, graph: Any, plan: Any, state: Any) -> Mapping[str, Any]:
        result = self._callback(graph=graph, plan=plan, state=state)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Mapping):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        return {"ok": bool(result), "value": result}


class RecordingExecutionSubmissionService:
    """Small queue-shaped test double that records durable submissions."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    async def submit(self, *, graph: Any, plan: Any, state: Any) -> Mapping[str, Any]:
        record = {
            "graph": graph.model_dump(mode="json") if hasattr(graph, "model_dump") else graph,
            "plan": plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan,
            "task_id": getattr(getattr(state, "goal", None), "goal_id", ""),
        }
        self.submissions.append(record)
        return {
            "ok": True,
            "queued": True,
            "plan_id": getattr(plan, "plan_id", ""),
            "task_id": record["task_id"],
        }


__all__ = [
    "ExecutionSubmissionService",
    "QueueExecutionSubmissionService",
    "RecordingExecutionSubmissionService",
]
