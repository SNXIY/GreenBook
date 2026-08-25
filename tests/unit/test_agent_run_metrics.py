from __future__ import annotations

from types import SimpleNamespace

from apps.agent_api.greenbook_agent_api.api.routes import RunResponse, _run_record_from_durable
from apps.agent_api.greenbook_agent_api.runner import AgentRun, performance_projection
from greenbook_agent_core.observability.run_metrics import (
    llm_category_scope,
    record_llm,
    record_stage,
    run_scope,
    snapshot,
)


def test_performance_projection_keeps_unknown_metrics_null() -> None:
    run = AgentRun(
        run_id="run-metrics",
        conversation_id="conversation-metrics",
        user_id="user-metrics",
        tenant_id="tenant-metrics",
        created_at="2026-08-19T00:00:00+00:00",
    )
    result = SimpleNamespace(
        tool_rounds=0,
        partial_results={"iterations": 3, "tool_calls": 2},
    )

    metrics = performance_projection(run, result)

    assert metrics["total_latency_ms"] is not None
    assert metrics["actionloop_iterations"] == 3
    assert metrics["actionloop_llm_calls"] is None
    assert metrics["actionloop_llm_latency_ms"] is None
    assert metrics["tool_calls"] == 2
    assert metrics["creator_llm_calls"] == 0
    assert metrics["llm_calls"] is None
    assert metrics["java_calls"] is None
    assert metrics["input_tokens"] is None
    assert metrics["output_tokens"] is None


def test_durable_run_projection_keeps_performance() -> None:
    run = AgentRun(
        run_id="run-performance-projection",
        conversation_id="conversation-metrics",
        user_id="user-metrics",
        tenant_id="tenant-metrics",
        payload={"performance": {"total_latency_ms": 123, "tool_calls": 2}},
    )

    record = _run_record_from_durable(run)

    assert record["performance"] == {"total_latency_ms": 123, "tool_calls": 2}


def test_run_response_accepts_nested_terminal_stage_metrics() -> None:
    response = RunResponse(
        run_id="run-terminal-metrics",
        conversation_id="conversation-metrics",
        status="COMPLETED",
        performance={
            "total_latency_ms": 123,
            "stage_timestamps": {"run_terminal": "2026-08-19T00:00:00+00:00"},
            "stage_durations_ms": {"context_ms": 10, "projection_ms": None},
        },
    )

    assert response.performance["stage_durations_ms"]["context_ms"] == 10


def test_performance_projection_exposes_zero_memory_for_explicit_skip() -> None:
    run_id = "run-memory-skipped"
    with run_scope(run_id):
        record_stage("memory_recall_skipped")
    run = AgentRun(
        run_id=run_id,
        conversation_id="conversation-metrics",
        user_id="user-metrics",
        tenant_id="tenant-metrics",
        created_at="2026-08-19T00:00:00+00:00",
    )

    metrics = performance_projection(run, SimpleNamespace(partial_results={}))

    assert metrics["memory_total_ms"] == 0
    assert metrics["memory_search_ms"] == 0
    assert metrics["memory_rank_ms"] == 0
    assert metrics["memory_touch_ms"] == 0
    assert metrics["memory_format_ms"] == 0


def test_performance_projection_preserves_memory_stage_values() -> None:
    run_id = "run-memory-values"
    with run_scope(run_id):
        for stage in (
            "memory_retrieval_start",
            "memory_candidates_ready",
            "memory_ranking_ready",
            "memory_touch_start",
            "memory_touch_ready",
            "memory_retrieval_ready",
            "memory_format_start",
            "memory_format_ready",
        ):
            record_stage(stage)
    run = AgentRun(
        run_id=run_id,
        conversation_id="conversation-metrics",
        user_id="user-metrics",
        tenant_id="tenant-metrics",
        created_at="2026-08-19T00:00:00+00:00",
    )

    metrics = performance_projection(run, SimpleNamespace(partial_results={}))

    assert metrics["memory_total_ms"] is not None
    assert metrics["memory_search_ms"] is not None
    assert metrics["memory_rank_ms"] is not None
    assert metrics["memory_touch_ms"] is not None
    assert metrics["memory_format_ms"] is not None


def test_stage_snapshot_derives_only_complete_duration_pairs() -> None:
    run_id = "run-stage-duration"
    record_stage("api_received", run_id=run_id)
    record_stage("runner_started", run_id=run_id)

    metrics = snapshot(run_id)

    assert metrics["stage_durations_ms"]["queue_wait_ms"] is not None
    assert metrics["stage_durations_ms"]["context_ms"] is None


def test_stage_snapshot_derives_execution_and_continuation_intervals() -> None:
    run_id = "run-stage-execution"
    for stage in (
        "route_decided",
        "execution_submit_start",
        "execution_submitted",
        "java_start",
        "java_end",
        "projection_persisted",
        "continuation_start",
    ):
        record_stage(stage, run_id=run_id)

    durations = snapshot(run_id)["stage_durations_ms"]

    assert durations["actionloop_pre_submit_ms"] is not None
    assert durations["execution_to_java_ms"] is not None
    assert durations["projection_after_java_ms"] is not None
    assert durations["continuation_wait_ms"] is not None


def test_llm_categories_aggregate_calls_latency_and_tokens() -> None:
    run_id = "run-llm-categories"
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    )

    with run_scope(run_id), llm_category_scope("SEMANTIC"):
        record_llm(response, 10)
    with run_scope(run_id), llm_category_scope("ACTIONLOOP"):
        record_llm(response, 20)
    with run_scope(run_id), llm_category_scope("CREATOR"):
        record_llm(response, 30)

    metrics = snapshot(run_id)

    assert metrics["llm_calls"] == 3
    assert metrics["llm_latency_ms"] == 60
    assert metrics["input_tokens"] == 33
    assert metrics["output_tokens"] == 21
    assert metrics["semantic_llm_calls"] == 1
    assert metrics["semantic_llm_latency_ms"] == 10
    assert metrics["actionloop_llm_calls"] == 1
    assert metrics["actionloop_llm_latency_ms"] == 20
    assert metrics["creator_llm_calls"] == 1
    assert metrics["creator_latency_ms"] == 30
    assert metrics["creator_input_tokens"] == 11
    assert metrics["creator_output_tokens"] == 7
