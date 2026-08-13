"""Phase 4.4 tests for AgentTrace and TraceCollector."""

from __future__ import annotations

import pytest
from greenbook_agent_core.artifact.models import Artifact
from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.models import (
    ArtifactHandle,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.observability.collector import TraceCollector
from greenbook_agent_core.observability.models import EventType
from greenbook_agent_core.observability.trace import AgentTrace


@pytest.fixture
def collector() -> TraceCollector:
    return TraceCollector()


@pytest.fixture
def trace(collector: TraceCollector) -> AgentTrace:
    return AgentTrace(
        collector, trace_id="t-001", execution_id="e-001",
        task_id="task-1", user_id="u1",
    )


def _step(capability: str = "SEARCH_COMMUNITY", step_id: str = "s1",
          ordinal: int = 1) -> StepExecution:
    return StepExecution(
        step_id=step_id, capability=capability, ordinal=ordinal,
        status=StepStatus.PENDING,
    )


# ── Scenario 1: complete CREATE flow trace ───────────────────────

def test_complete_create_flow_timeline(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-1", execution_id="e-1", task_id="task-1")

    t.task_created(goal="Create Java article", category="CREATE_CONTENT")
    t.plan_created(plan_source="GOAL_RUNTIME", step_count=3)
    t.execution_started()

    # Step 1: SEARCH
    s1 = _step("SEARCH_COMMUNITY", "s1", 1)
    t.step_started(s1)
    t.tool_invoked(s1, "community.search_public_posts")
    t.tool_completed(s1, ExecutionResult.success(
        capability="SEARCH_COMMUNITY", tool_name="community.search_public_posts",
        tool_result={},
        artifact=ArtifactHandle(artifact_type="SEARCH_RESULT", summary="Results"),
    ))
    t.artifact_created(s1, Artifact(
        artifact_id="a1", task_id="task-1", execution_id="e-1", step_id="s1",
        artifact_type="SEARCH_RESULT", summary="Results",
    ))
    t.step_completed(s1)

    # Step 2: ANALYZE (LLM)
    s2 = _step("ANALYZE_CONTENT_PATTERNS", "s2", 2)
    t.step_started(s2)
    t.step_completed(s2)
    t.artifact_created(s2, Artifact(
        artifact_id="a2", task_id="task-1", execution_id="e-1", step_id="s2",
        artifact_type="ANALYSIS_REPORT", summary="Analysis",
    ))

    # Step 3: CREATE
    s3 = _step("GENERATE_CONTENT", "s3", 3)
    t.step_started(s3)
    t.tool_invoked(s3, "content.create_draft")
    t.tool_completed(s3, ExecutionResult.success(
        capability="GENERATE_CONTENT", tool_name="content.create_draft",
        tool_result={},
        artifact=ArtifactHandle(artifact_type="DRAFT", resource_id="d99", summary="Draft"),
    ))
    t.artifact_created(s3, Artifact(
        artifact_id="a3", task_id="task-1", execution_id="e-1", step_id="s3",
        artifact_type="DRAFT", resource_id="d99", summary="Draft",
    ))
    t.step_completed(s3)

    t.execution_completed()

    # Verify timeline
    timeline = collector.timeline("t-1")
    assert len(timeline) >= 12

    types = [e.event_type for e in timeline]
    assert EventType.TASK_CREATED in types
    assert EventType.PLAN_CREATED in types
    assert EventType.EXECUTION_STARTED in types
    assert EventType.STEP_STARTED in types
    assert EventType.TOOL_INVOKED in types
    assert EventType.TOOL_COMPLETED in types
    assert EventType.ARTIFACT_CREATED in types
    assert EventType.STEP_COMPLETED in types
    assert EventType.EXECUTION_COMPLETED in types

    # Events should be in chronological order
    for i in range(1, len(timeline)):
        assert timeline[i].timestamp >= timeline[i-1].timestamp


# ── Scenario 2: tool failure has failure event ────────────────────

def test_tool_failure_event(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-2", execution_id="e-2")
    s1 = _step("SEARCH_COMMUNITY", "s1", 1)

    t.execution_started()
    t.step_started(s1)
    t.tool_invoked(s1, "community.search_public_posts")
    t.tool_failed(s1, "community.search_public_posts", "Java backend unavailable")
    t.step_failed(s1, "Java backend unavailable")
    t.execution_failed("Search step failed")

    timeline = collector.timeline("t-2")
    types = [e.event_type for e in timeline]
    assert EventType.TOOL_FAILED in types
    assert EventType.STEP_FAILED in types
    assert EventType.EXECUTION_FAILED in types


# ── Scenario 3: artifact creation event links to step ────────────

def test_artifact_creation_links_to_step(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-3", execution_id="e-3")
    s1 = _step("GENERATE_CONTENT", "s1", 1)

    t.artifact_created(s1, Artifact(
        artifact_id="a-draft", task_id="task-1", execution_id="e-3",
        step_id="s1", artifact_type="DRAFT", resource_id="draft-1",
        summary="Java Guide",
    ))

    events = collector.timeline("t-3")
    assert len(events) == 1
    evt = events[0]
    assert evt.event_type == EventType.ARTIFACT_CREATED
    assert evt.step_id == "s1"
    assert evt.artifact_type == "DRAFT"
    assert evt.payload["resource_id"] == "draft-1"
    assert evt.payload["summary"] == "Java Guide"


# ── Scenario 4: query by trace_id returns correct order ──────────

def test_query_by_trace_returns_correct_order(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-4", execution_id="e-4")

    t.execution_started()
    t.step_started(_step("SEARCH_COMMUNITY", "s1", 1))
    t.step_completed(_step("SEARCH_COMMUNITY", "s1", 1))
    t.execution_completed()

    # Query by trace
    trace = collector.find_trace("t-4")
    assert trace is not None
    assert trace.event_count == 4

    # Query by execution
    events = collector.find_by_execution("e-4")
    assert len(events) == 4

    # Non-existent trace
    assert collector.find_trace("no-such") is None
    assert collector.timeline("no-such") == []


# ── edge cases ────────────────────────────────────────────────────

def test_approval_required_event(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-5", execution_id="e-5")
    s = _step("PUBLISH_NOW", "s1", 1)
    t.approval_required(s)

    events = collector.timeline("t-5")
    assert events[0].event_type == EventType.APPROVAL_REQUIRED


def test_multiple_executions_same_collector(collector: TraceCollector) -> None:
    t1 = AgentTrace(collector, trace_id="t-a", execution_id="e-a")
    t2 = AgentTrace(collector, trace_id="t-b", execution_id="e-b")

    t1.execution_started()
    t2.execution_started()
    t1.execution_completed()

    assert collector.count() == 2
    assert len(collector.find_by_execution("e-a")) == 2
    assert len(collector.find_by_execution("e-b")) == 1


def test_trace_has_metadata() -> None:
    collector = TraceCollector()
    collector.create_trace(
        trace_id="t-meta", task_id="task-1",
        execution_id="e-1", user_id="u99",
    )
    trace = collector.find_trace("t-meta")
    assert trace is not None
    assert trace.task_id == "task-1"
    assert trace.execution_id == "e-1"
    assert trace.user_id == "u99"
    assert trace.created_at != ""


def test_clear_collector(collector: TraceCollector) -> None:
    t = AgentTrace(collector, trace_id="t-c", execution_id="e-c")
    t.execution_started()
    assert collector.count() == 1

    collector.clear()
    assert collector.count() == 0
