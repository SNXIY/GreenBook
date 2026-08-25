"""Thin adapter from evaluation cases to the production Agent Runtime.

The adapter owns transport concerns only.  It creates a conversation, submits
the case turns, waits for the real Run to settle, and projects facts returned by
the Runtime into the existing ``EvaluationRunner`` payload.  It deliberately
does not interpret user text, plan work, select tools, or reproduce lifecycle
logic.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import EvalCase, EvaluationReport
from .runner import EvaluationRunner


class RuntimeEvaluationTransport(Protocol):
    """Minimum transport contract used by ``RuntimeEvaluationAdapter``."""

    async def create_conversation(self, title: str) -> Mapping[str, Any]: ...

    async def submit_message(
        self,
        conversation_id: str,
        content: str,
        *,
        timezone: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    async def get_run(self, run_id: str) -> Mapping[str, Any]: ...

    async def list_tasks(self, conversation_id: str) -> Sequence[Mapping[str, Any]]: ...


class HttpRuntimeTransport:
    """Small HTTP implementation for the existing Agent API.

    The API currently exposes the Run and Task projections directly.  A
    deployment may provide ``read_runtime_state`` on a subclass/transport to
    add Objective, ResourceBinding, Execution, or Java truth projections; the
    adapter consumes those facts without knowing how they were obtained.
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        access_token: str = "",
        java_base_url: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        import httpx

        default_headers = dict(headers or {})
        if access_token:
            default_headers.setdefault("Authorization", f"Bearer {access_token}")
        self._client = httpx.AsyncClient(
            base_url=str(base_url).rstrip("/"),
            headers=default_headers,
            timeout=timeout_seconds,
        )
        self._java_base_url = str(java_base_url).rstrip("/")

    async def create_conversation(self, title: str) -> Mapping[str, Any]:
        response = await self._client.post(
            "/api/v1/agent/conversations",
            json={"title": title, "surface": "HOME"},
        )
        response.raise_for_status()
        return response.json()

    async def submit_message(
        self,
        conversation_id: str,
        content: str,
        *,
        timezone: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        response = await self._client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            json={"content": content, "client_timezone": timezone},
            headers={"Idempotency-Key": idempotency_key},
        )
        response.raise_for_status()
        return response.json()

    async def get_run(self, run_id: str) -> Mapping[str, Any]:
        response = await self._client.get(f"/api/v1/agent/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    async def list_tasks(self, conversation_id: str) -> Sequence[Mapping[str, Any]]:
        response = await self._client.get(
            f"/api/v1/agent/conversations/{conversation_id}/tasks"
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            return list(payload.get("items") or ())
        return list(payload or ())

    async def read_runtime_state(
        self,
        *,
        case: EvalCase,
        conversation_id: str,
        run_id: str,
        run: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Read optional existing projections exposed by the live stack.

        Fast-path reads may have no Execution at all.  Missing optional
        projections therefore remain empty facts instead of turning a valid
        Run into an adapter error.
        """
        execution_records: list[dict[str, Any]] = []
        execution_ids = _collect_execution_ids(run, tasks)
        for execution_id in execution_ids:
            response = await self._client.get(
                f"/api/v1/executions/{execution_id}"
            )
            if response.status_code == 200:
                execution_records.append(response.json())

        state: dict[str, Any] = {
            "execution_records": execution_records,
        }
        if self._java_base_url:
            java_truth: dict[str, Any] = {}
            drafts = await self._client.get(
                f"{self._java_base_url}/api/v1/agent/me/drafts"
            )
            if drafts.status_code == 200:
                java_truth["drafts"] = drafts.json()
            state["java_truth"] = java_truth
        return state

    async def aclose(self) -> None:
        await self._client.aclose()


class RuntimeEvaluationAdapter:
    """Drive real Runtime state for one case at a time."""

    TERMINAL_STATUSES = frozenset(
        {
            "COMPLETED",
            "PARTIAL_SUCCESS",
            "FAILED",
            "CANCELLED",
            "WAITING_USER",
            "WAITING_HUMAN",
            "WAITING_APPROVAL",
            "WAITING_EXTERNAL",
            "PAUSED",
        }
    )

    def __init__(
        self,
        transport: RuntimeEvaluationTransport | Any,
        *,
        poll_interval_seconds: float = 0.5,
        timeout_seconds: float = 120.0,
        timezone: str = "Asia/Shanghai",
        title_prefix: str = "evaluation",
        state_reader: Any | None = None,
    ) -> None:
        self.transport = transport
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.timezone = timezone
        self.title_prefix = title_prefix
        self.state_reader = state_reader

    async def run_case(self, case: EvalCase) -> dict[str, Any]:
        started = time.monotonic()
        conversation_payload = await _maybe_await(
            self.transport.create_conversation(
                f"{self.title_prefix}:{case.case_id}"
            )
        )
        conversation = _as_mapping(conversation_payload)
        conversation_id = _first_text(
            conversation,
            "conversation_id",
            "id",
        )
        if not conversation_id:
            raise RuntimeError("Runtime adapter received no conversation_id.")

        turns = _case_turns(case)
        if not turns:
            raise RuntimeError(f"Evaluation case {case.case_id} has no user message.")

        submitted: list[dict[str, Any]] = []
        last_run: dict[str, Any] = {}
        for index, content in enumerate(turns, start=1):
            accepted_payload = await _maybe_await(
                self.transport.submit_message(
                    conversation_id,
                    content,
                    timezone=self.timezone,
                    idempotency_key=(
                        f"eval-{case.case_id}-{index}-{uuid.uuid4().hex}"
                    ),
                )
            )
            accepted = _as_mapping(accepted_payload)
            run_id = _first_text(accepted, "run_id", "id")
            if not run_id:
                raise RuntimeError(
                    f"Runtime adapter received no run_id for {case.case_id} turn {index}."
                )
            last_run = await self._wait_for_terminal(run_id)
            submitted.append(
                {
                    "turn": index,
                    "content": content,
                    "accepted": accepted,
                    "run": last_run,
                }
            )

        tasks_payload = await _maybe_await(
            self.transport.list_tasks(conversation_id)
        )
        tasks = [
            _as_mapping(item)
            for item in (tasks_payload or ())
            if isinstance(item, Mapping) or hasattr(item, "model_dump")
        ]
        state = await self._read_runtime_state(
            case=case,
            conversation_id=conversation_id,
            run_id=_first_text(last_run, "run_id", "id"),
            run=last_run,
            tasks=tasks,
        )
        return _project_runtime_facts(
            case=case,
            conversation_id=conversation_id,
            run=last_run,
            tasks=tasks,
            submitted=submitted,
            state=state,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    async def evaluate(
        self,
        cases: Sequence[EvalCase],
        *,
        badcase_store: Any | None = None,
        run_id: str = "runtime-production",
    ) -> EvaluationReport:
        runner = EvaluationRunner(runtime=self, badcase_store=badcase_store)
        return await runner.run_cases(cases, run_id=run_id)

    def evaluate_sync(
        self,
        cases: Sequence[EvalCase],
        *,
        badcase_store: Any | None = None,
        run_id: str = "runtime-production",
    ) -> EvaluationReport:
        return asyncio.run(
            self.evaluate(cases, badcase_store=badcase_store, run_id=run_id)
        )

    async def _wait_for_terminal(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            payload = await _maybe_await(self.transport.get_run(run_id))
            run = _as_mapping(payload)
            status = _status(run)
            if status in self.TERMINAL_STATUSES:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Runtime run {run_id} did not reach terminal state; status={status!r}."
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def _read_runtime_state(
        self,
        *,
        case: EvalCase,
        conversation_id: str,
        run_id: str,
        run: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        reader = self.state_reader
        if reader is None:
            reader = getattr(self.transport, "read_runtime_state", None)
        if not callable(reader):
            return {}
        value = reader(
            case=case,
            conversation_id=conversation_id,
            run_id=run_id,
            run=dict(run),
            tasks=list(tasks),
        )
        value = await _maybe_await(value)
        return _as_mapping(value)


def _project_runtime_facts(
    *,
    case: EvalCase,
    conversation_id: str,
    run: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    submitted: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    duration_ms: float,
) -> dict[str, Any]:
    """Project returned facts without inventing missing Runtime state."""

    partial = _mapping_from(run, "partial_results")
    merged = _merge_mappings(partial, state)
    refs = _collect_resource_refs(run, partial, state, tasks)
    resource_types = sorted(
        {
            str(ref.get("resource_kind") or ref.get("kind") or "").upper()
            for ref in refs
            if str(ref.get("resource_kind") or ref.get("kind") or "").strip()
        }
    )
    objectives = _collect_objectives(run, partial, state, tasks)
    objective_count = _first_number(
        merged,
        "objective_count",
        "objectives_count",
    )
    if objective_count is None and objectives:
        objective_count = len(objectives)

    task_statuses = [
        _status(task)
        for task in tasks
        if _status(task)
    ]
    run_status = _status(run)
    task_state = _project_task_state(task_statuses, run_status)
    approval = _first_value(merged, run, "approval", "approval_status")
    schedule = _first_value(merged, run, "schedule", "schedule_state")
    side_effects = _as_list(_first_value(merged, run, "side_effects"))
    artifacts = _as_list(_first_value(merged, run, "artifacts"))
    execution_ids = _collect_execution_ids(run, partial, state, tasks)
    duplicate_write_count = _first_number(
        merged,
        "duplicate_write_count",
        "duplicate_execution_count",
    )
    duplicate_side_effect = bool(
        _first_value(merged, run, "duplicate_side_effect", "duplicate_write")
    )
    if duplicate_write_count is None:
        duplicate_write_count = 1.0 if duplicate_side_effect else 0.0

    ownership_conflicts = _ownership_conflicts(refs)
    performance = _mapping_from(run, "performance")
    timing = _mapping_from(run, "timing")
    budget = _mapping_from(run, "budget")
    total_latency = _first_number(
        performance,
        "total_latency_ms",
    )
    if total_latency is None:
        total_latency = _first_number(timing, "total_ms")
    if total_latency is None:
        total_latency = duration_ms

    trace = _mapping_from(run, "trace")
    if not trace:
        trace = {
            "conversation_id": conversation_id,
            "task_id": _first_text(tasks[0], "task_id", "id") if tasks else "",
            "events": _as_list(_first_value(run, "events")),
        }

    actual: dict[str, Any] = {
        "status": run_status,
        "terminal_status": run_status,
        "task_state": task_state,
        "objective_count": objective_count,
        "objectives": objectives,
        "resource_types": resource_types,
        "resource_kinds": resource_types,
        "resource_refs": refs,
        "resource_bindings": refs,
        "objective_ownership": {
            str(ref.get("resource_id")): str(ref.get("objective_id"))
            for ref in refs
            if ref.get("resource_id") and ref.get("objective_id")
        },
        "ownership_conflicts": ownership_conflicts,
        "execution_ids": execution_ids,
        "execution_count": len(execution_ids),
        "tasks": list(tasks),
        "run": dict(run),
        "submitted_turns": list(submitted),
        "approval": approval,
        "schedule": schedule,
        "artifacts": artifacts,
        "side_effects": side_effects,
        "duplicate_write_count": duplicate_write_count,
        "duplicate_side_effect": duplicate_side_effect,
        "latency_ms": total_latency,
        "llm_calls": _first_number(budget, "model_calls") or 0.0,
        "tool_call_count": _first_number(budget, "tool_calls") or 0.0,
        "trace": trace,
        "conversation_id": conversation_id,
        "run_id": _first_text(run, "run_id", "id"),
    }
    if "java_truth" in merged:
        actual["java_truth"] = merged["java_truth"]
    if "execution_records" in merged:
        actual["executions"] = merged["execution_records"]
    return actual


def _case_turns(case: EvalCase) -> list[str]:
    turns: list[str] = []
    for turn in case.conversation_turns or ():
        if not isinstance(turn, Mapping):
            continue
        content = str(turn.get("content") or "").strip()
        if content and str(turn.get("role") or "user").lower() == "user":
            turns.append(content)
    message = str(case.user_message or "").strip()
    if message and (not turns or turns[-1] != message):
        turns.append(message)
    return turns


def _project_task_state(statuses: Sequence[str], run_status: str) -> str:
    if statuses:
        normalized = {str(value).upper() for value in statuses}
        if normalized <= {"COMPLETED", "SUCCESS"}:
            return "COMPLETED"
        if normalized & {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}:
            return next(
                value
                for value in statuses
                if value in {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}
            )
        if normalized & {"FAILED", "CANCELLED"}:
            return next(value for value in statuses if value in {"FAILED", "CANCELLED"})
        if normalized & {"RUNNING", "PENDING", "ACCEPTED"}:
            return "RUNNING"
    return run_status


def _collect_resource_refs(*containers: Any) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for container in containers:
        for item in _iter_candidate_refs(container):
            ref = _normalize_resource_ref(item)
            resource_id = str(ref.get("resource_id") or "").strip()
            resource_kind = str(ref.get("resource_kind") or "").strip().upper()
            if not resource_id or not resource_kind:
                continue
            ref["resource_id"] = resource_id
            ref["resource_kind"] = resource_kind
            unique.setdefault((resource_id, resource_kind), ref)
    return list(unique.values())


def _iter_candidate_refs(value: Any):
    if isinstance(value, Mapping):
        for key in (
            "resource_refs",
            "resource_bindings",
            "resource_index",
            "resources",
            "artifacts",
            "resource",
        ):
            if key in value:
                yield from _iter_candidate_refs(value[key])
        if _looks_like_resource(value):
            yield value
        for key in ("tasks", "objectives", "goals", "execution_refs", "partial_results"):
            if key in value:
                yield from _iter_candidate_refs(value[key])
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _iter_candidate_refs(item)


def _looks_like_resource(value: Mapping[str, Any]) -> bool:
    return bool(
        value.get("resource_id")
        or value.get("draft_id")
        or value.get("schedule_id")
        or value.get("post_id")
        or value.get("resource_kind")
    )


def _normalize_resource_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    resource_id = (
        value.get("resource_id")
        or value.get("draft_id")
        or value.get("schedule_id")
        or value.get("post_id")
        or value.get("id")
        or ""
    )
    kind = (
        value.get("resource_kind")
        or value.get("kind")
        or value.get("type")
        or ("DRAFT" if value.get("draft_id") else "")
        or ("SCHEDULE" if value.get("schedule_id") else "")
        or ("POST" if value.get("post_id") else "")
    )
    return {
        **dict(value),
        "resource_id": str(resource_id),
        "resource_kind": str(kind).upper(),
    }


def _ownership_conflicts(refs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    owners: dict[tuple[str, str], set[str]] = {}
    for ref in refs:
        resource_id = str(ref.get("resource_id") or "")
        resource_kind = str(ref.get("resource_kind") or "").upper()
        objective_id = str(ref.get("objective_id") or "")
        if resource_id and resource_kind and objective_id:
            owners.setdefault((resource_id, resource_kind), set()).add(objective_id)
    return [
        {
            "resource_id": resource_id,
            "resource_kind": resource_kind,
            "objective_ids": sorted(objective_ids),
        }
        for (resource_id, resource_kind), objective_ids in owners.items()
        if len(objective_ids) > 1
    ]


def _collect_objectives(*containers: Any) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for container in containers:
        for item in _iter_named_items(container, "objectives"):
            if not isinstance(item, Mapping):
                continue
            objective_id = str(item.get("objective_id") or item.get("id") or "")
            if objective_id:
                seen.setdefault(objective_id, dict(item))
    return list(seen.values())


def _iter_named_items(value: Any, key: str):
    if isinstance(value, Mapping):
        if key in value:
            nested = value[key]
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                yield from nested
            elif nested:
                yield nested
        for nested_key in ("tasks", "partial_results", "run", "state"):
            if nested_key in value:
                yield from _iter_named_items(value[nested_key], key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _iter_named_items(item, key)


def _collect_execution_ids(*containers: Any) -> list[str]:
    values: list[str] = []
    for container in containers:
        _collect_named_values(container, {"execution_id", "execution_ids"}, values)
    return list(dict.fromkeys(value for value in values if value))


def _collect_named_values(value: Any, keys: set[str], output: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in keys:
                if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                    output.extend(str(value) for value in item if value)
                elif item:
                    output.append(str(item))
            else:
                _collect_named_values(item, keys, output)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _collect_named_values(item, keys, output)


def _mapping_from(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    nested = value.get(key)
    if isinstance(nested, Mapping):
        return dict(nested)
    return {}


def _merge_mappings(*values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        result.update(dict(value or {}))
    return result


def _first_value(*values: Any) -> Any:
    containers = [value for value in values if isinstance(value, Mapping)]
    keys = [value for value in values if isinstance(value, str)]
    for container in containers:
        for key in keys:
            candidate = container.get(key)
            if candidate is not None:
                return candidate
    return None


def _first_text(container: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = container.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _first_number(container: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = container.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _status(value: Mapping[str, Any]) -> str:
    return str(value.get("status") or value.get("state") or "").upper()


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "HttpRuntimeTransport",
    "RuntimeEvaluationAdapter",
    "RuntimeEvaluationTransport",
]
