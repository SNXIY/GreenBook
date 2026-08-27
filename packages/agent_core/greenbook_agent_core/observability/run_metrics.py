"""Small per-Run counters shared by the existing Agent boundaries.

This is intentionally an in-process accumulator, not a tracing system.  The
durable Run projection snapshots it at each result boundary.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterator

_active_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "greenbook_active_run_id", default=""
)
_active_llm_category: contextvars.ContextVar[str] = contextvars.ContextVar(
    "greenbook_active_llm_category", default="SEMANTIC"
)
_lock = threading.RLock()
_values: dict[str, dict[str, Any]] = {}

_STAGE_PAIRS: dict[str, tuple[str, str]] = {
    "queue_wait_ms": ("api_received", "runner_started"),
    "context_ms": ("context_start", "context_ready"),
    "context_conversation_load_ms": ("context_start", "context_conversation_loaded"),
    "context_history_ms": ("context_conversation_loaded", "context_history_ready"),
    "context_tasks_load_ms": ("context_history_ready", "context_tasks_loaded"),
    "context_task_projection_ms": ("context_tasks_loaded", "context_task_projection_ready"),
    "context_parallel_wait_ms": ("context_parallel_start", "context_parallel_ready"),
    "context_executions_load_ms": ("context_parallel_start", "context_executions_ready"),
    "context_preferences_load_ms": ("context_parallel_start", "context_preferences_ready"),
    "context_memory_recall_ms": ("context_parallel_start", "context_memory_ready"),
    "memory_retrieval_ms": ("memory_retrieval_start", "memory_retrieval_ready"),
    "memory_repository_search_ms": ("memory_retrieval_start", "memory_candidates_ready"),
    "memory_ranking_filter_ms": ("memory_candidates_ready", "memory_ranking_ready"),
    "memory_touch_ms": ("memory_touch_start", "memory_touch_ready"),
    "memory_format_ms": ("memory_format_start", "memory_format_ready"),
    "context_prompt_assembly_ms": ("context_parallel_ready", "context_prompt_ready"),
    "semantic_ms": ("semantic_start", "semantic_resolved"),
    "route_ms": ("semantic_resolved", "route_decided"),
    "execution_submit_ms": ("execution_submit_start", "execution_submitted"),
    "actionloop_pre_submit_ms": ("route_decided", "execution_submit_start"),
    "actionloop_task_prepare_ms": ("actionloop_task_prepare_start", "actionloop_task_ready"),
    "actionloop_state_prep_ms": ("actionloop_entry", "actionloop_first_decision_start"),
    "actionloop_first_llm_ms": ("actionloop_first_llm_start", "actionloop_first_llm_end"),
    "actionloop_last_validation_ms": ("actionloop_last_llm_end", "actionloop_last_decision_validated"),
    "actionloop_decision_to_write_ms": ("actionloop_decision_ready", "actionloop_write_dispatch_ready"),
    "actionloop_write_dispatch_ms": ("actionloop_write_dispatch_ready", "execution_submit_start"),
    "execution_to_java_ms": ("execution_submitted", "java_start"),
    "projection_after_java_ms": ("java_end", "projection_persisted"),
    "worker_claim_ms": ("worker_claimed", "worker_started"),
    "observation_ms": ("observation_start", "observation_finished"),
    "continuation_wait_ms": ("projection_persisted", "continuation_start"),
    "continuation_ms": ("continuation_start", "continuation_finished"),
    "projection_ms": ("projection_start", "projection_persisted"),
    "final_response_ms": ("final_response_start", "final_response_finished"),
    "run_terminal_ms": ("run_terminal_start", "run_terminal"),
}


def _run_id(run_id: str | None = None) -> str:
    return str(run_id or _active_run_id.get() or "").strip()


def _bucket(run_id: str) -> dict[str, Any] | None:
    if not run_id:
        return None
    with _lock:
        return _values.setdefault(run_id, {
            "llm_calls": 0, "llm_latency_ms": 0,
            "semantic_llm_calls": 0, "semantic_llm_latency_ms": 0,
            "semantic_input_tokens": 0, "semantic_output_tokens": 0,
            "creator_llm_calls": 0, "creator_latency_ms": 0,
            "creator_input_tokens": 0, "creator_output_tokens": 0,
            "tool_calls": 0, "tool_latency_ms": 0,
            "java_calls": 0, "java_latency_ms": 0,
            "input_tokens": 0, "output_tokens": 0,
            "actionloop_iterations": 0,
            "actionloop_llm_calls": 0, "actionloop_llm_latency_ms": 0,
            "actionloop_input_tokens": 0, "actionloop_output_tokens": 0,
            "llm_events": [],
            "memory_retrieval": {},
            "final_response_latency_ms": 0,
            "stage_timestamps": {},
        })


@contextmanager
def run_scope(run_id: str) -> Iterator[None]:
    token = _active_run_id.set(str(run_id or ""))
    try:
        yield
    finally:
        _active_run_id.reset(token)


@contextmanager
def llm_category_scope(category: str) -> Iterator[None]:
    """Classify a model call at an existing Run boundary."""
    token = _active_llm_category.set(str(category or "SEMANTIC").upper())
    try:
        yield
    finally:
        _active_llm_category.reset(token)


def record_llm(response: Any, latency_ms: int, *, run_id: str | None = None, creator: bool = False) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is None:
        return
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage is not None else 0
    category = "CREATOR" if creator else _active_llm_category.get().upper()
    if category not in {"SEMANTIC", "ACTIONLOOP", "CREATOR"}:
        category = "SEMANTIC"
    category_prefix = category.lower()
    elapsed = max(0, int(latency_ms))
    with _lock:
        bucket["llm_calls"] += 1
        bucket["llm_latency_ms"] += elapsed
        bucket["input_tokens"] += prompt_tokens
        bucket["output_tokens"] += completion_tokens
        bucket[f"{category_prefix}_llm_calls"] += 1
        latency_key = "creator_latency_ms" if category == "CREATOR" else f"{category_prefix}_llm_latency_ms"
        bucket[latency_key] += elapsed
        bucket[f"{category_prefix}_input_tokens"] += prompt_tokens
        bucket[f"{category_prefix}_output_tokens"] += completion_tokens
        events = bucket.setdefault("llm_events", [])
        if len(events) < 100:
            events.append({
                "category": category,
                "latency_ms": elapsed,
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            })


def record_tool(latency_ms: int, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["tool_calls"] += 1
            bucket["tool_latency_ms"] += max(0, int(latency_ms))


def record_java(latency_ms: int, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["java_calls"] += 1
            bucket["java_latency_ms"] += max(0, int(latency_ms))


def record_actionloop(iterations: int, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["actionloop_iterations"] += max(0, int(iterations))


def record_actionloop_llm(latency_ms: int, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["actionloop_llm_calls"] += 1
            bucket["actionloop_llm_latency_ms"] += max(0, int(latency_ms))


def record_memory_retrieval(
    *,
    source: str,
    candidate_count: int,
    selected_count: int,
    memory_types: list[str],
    run_id: str | None = None,
) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["memory_retrieval"] = {
                "source": source,
                "candidate_count": max(0, int(candidate_count)),
                "selected_count": max(0, int(selected_count)),
                "memory_types": sorted(set(memory_types)),
            }


def record_final_response(latency_ms: int, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is not None:
        with _lock:
            bucket["final_response_latency_ms"] += max(0, int(latency_ms))


def record_final_response_once(latency_ms: int, *, run_id: str | None = None) -> bool:
    """Record the terminal presentation boundary once per Run.

    Run reads can be polled concurrently.  The terminal response is a single
    presentation boundary, so repeated reads must not accumulate read-time
    latency into the Run metric.
    """

    bucket = _bucket(_run_id(run_id))
    if bucket is None:
        return False
    from datetime import UTC, datetime

    with _lock:
        timestamps = bucket.setdefault("stage_timestamps", {})
        if "final_response_finished" in timestamps:
            return False
        bucket["final_response_latency_ms"] += max(0, int(latency_ms))
        timestamps["final_response_finished"] = datetime.now(UTC).isoformat()
        return True


def record_stage(stage: str, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is None or not stage:
        return
    from datetime import UTC, datetime

    with _lock:
        timestamps = bucket.setdefault("stage_timestamps", {})
        timestamps[str(stage)] = datetime.now(UTC).isoformat()


def record_stage_once(stage: str, *, run_id: str | None = None) -> None:
    bucket = _bucket(_run_id(run_id))
    if bucket is None or not stage:
        return
    from datetime import UTC, datetime

    with _lock:
        timestamps = bucket.setdefault("stage_timestamps", {})
        timestamps.setdefault(str(stage), datetime.now(UTC).isoformat())


def snapshot(run_id: str) -> dict[str, Any]:
    with _lock:
        value = dict(_values.get(str(run_id or ""), {}))
        timestamps = dict(value.get("stage_timestamps") or {})
        value["llm_events"] = list(value.get("llm_events") or [])
        value["memory_retrieval"] = dict(value.get("memory_retrieval") or {})
    durations: dict[str, int | None] = {}
    from datetime import datetime

    for name, (start_key, end_key) in _STAGE_PAIRS.items():
        start = timestamps.get(start_key)
        end = timestamps.get(end_key)
        if not start or not end:
            durations[name] = None
            continue
        try:
            delta = (
                datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            ).total_seconds() * 1000
            durations[name] = max(0, round(delta))
        except (TypeError, ValueError):
            durations[name] = None
    value["stage_durations_ms"] = durations
    return value


__all__ = [
    "llm_category_scope", "record_actionloop", "record_actionloop_llm", "record_final_response", "record_final_response_once", "record_java", "record_llm",
    "record_memory_retrieval", "record_tool", "run_scope", "snapshot",
    "record_stage", "record_stage_once",
]
