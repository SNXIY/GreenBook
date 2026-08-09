"""Phase 4.2 tests for ArtifactStore."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_core.artifact.models import Artifact
from greenbook_assistant_core.artifact.repository import ArtifactRepository
from greenbook_assistant_core.artifact.store import ArtifactStore
from greenbook_assistant_core.execution.invocation import ExecutionResult
from greenbook_assistant_core.execution.models import ArtifactHandle, StepExecution, StepStatus


@pytest.fixture(autouse=True)
def _clear() -> None:
    ArtifactRepository.clear()


@pytest.fixture
def repo() -> ArtifactRepository:
    return ArtifactRepository()


@pytest.fixture
def store(repo: ArtifactRepository) -> ArtifactStore:
    return ArtifactStore(repo)


# ── helpers ──────────────────────────────────────────────────────

def _search_result() -> ExecutionResult:
    return ExecutionResult.success(
        capability="SEARCH_COMMUNITY",
        tool_name="community.search_public_posts",
        tool_result={"data": {"items": [{"post_id": "p1", "title": "Java 101"}], "total": 1}},
        artifact=ArtifactHandle(artifact_type="SEARCH_RESULT", summary="Java 101"),
    )


def _analysis_report() -> ExecutionResult:
    return ExecutionResult.success(
        capability="ANALYZE_CONTENT_PATTERNS",
        tool_name="(llm)",
        tool_result={"llm_step": True},
        artifact=ArtifactHandle(artifact_type="ANALYSIS_REPORT", summary="Java posts are popular"),
    )


def _content_draft(draft_id: str = "d1") -> ExecutionResult:
    return ExecutionResult.success(
        capability="GENERATE_CONTENT",
        tool_name="content.create_draft",
        tool_result={"data": {"draft_id": draft_id, "title": "Java Guide"}},
        artifact=ArtifactHandle(
            artifact_type="DRAFT", resource_id=draft_id, summary="Java Guide",
        ),
    )


def _failed_result() -> ExecutionResult:
    return ExecutionResult.from_tool_error(
        capability="SEARCH_COMMUNITY",
        tool_name="community.search_public_posts",
        error_code="TIMEOUT",
        error_message="Search timed out",
        retryable=True,
    )


# ── Scenario 1: SEARCH produces SEARCH_RESULT Artifact ────────────

def test_search_produces_artifact(store: ArtifactStore) -> None:
    a = store.create_from_result(
        _search_result(),
        task_id="t1", execution_id="e1", step_id="s1",
    )
    assert a is not None
    assert a.artifact_type == "SEARCH_RESULT"
    assert a.task_id == "t1"
    assert a.execution_id == "e1"
    assert a.step_id == "s1"
    assert a.summary == "Java 101"
    assert a.resource_kind == "POST"
    assert "tool_name" in a.metadata
    assert a.metadata["tool_name"] == "community.search_public_posts"


def test_search_artifact_persisted_and_queryable(store: ArtifactStore) -> None:
    a = store.create_from_result(_search_result(), task_id="t1", execution_id="e1", step_id="s1")
    assert a is not None

    found = store.find_by_execution("e1")
    assert len(found) == 1
    assert found[0].artifact_type == "SEARCH_RESULT"

    by_task = store.find_by_task("t1")
    assert len(by_task) == 1

    by_step = store.find_by_step("s1")
    assert by_step is not None
    assert by_step.artifact_type == "SEARCH_RESULT"


# ── Scenario 2: ANALYZE resolves SEARCH_RESULT → produces ANALYSIS_REPORT ──

def test_analyze_resolves_search_result(store: ArtifactStore) -> None:
    # Step 1: SEARCH
    store.create_from_result(_search_result(), task_id="t1", execution_id="e1", step_id="s1")

    # Step 2: ANALYZE — needs SEARCH_RESULT as input
    step2 = StepExecution(
        step_id="s2", capability="ANALYZE_CONTENT_PATTERNS", ordinal=2,
        input_artifact_types=["SEARCH_RESULT"],
        output_artifact_type="ANALYSIS_REPORT",
        depends_on=["s1"],
        status=StepStatus.PENDING,
    )
    inputs = store.resolve_inputs(step2, "e1")
    assert len(inputs) == 1
    assert inputs[0].artifact_type == "SEARCH_RESULT"
    assert inputs[0].step_id == "s1"

    # Step 2 produces ANALYSIS_REPORT
    a2 = store.create_from_result(_analysis_report(), task_id="t1", execution_id="e1", step_id="s2")
    assert a2 is not None
    assert a2.artifact_type == "ANALYSIS_REPORT"
    assert a2.resource_kind == "ARTIFACT"


# ── Scenario 3: CREATE resolves ANALYSIS_REPORT → produces DRAFT ──

def test_create_resolves_analysis_and_produces_draft(store: ArtifactStore) -> None:
    # Full pipeline: SEARCH → ANALYZE → CREATE
    store.create_from_result(_search_result(), task_id="t1", execution_id="e1", step_id="s1")
    store.create_from_result(_analysis_report(), task_id="t1", execution_id="e1", step_id="s2")

    # Step 3: CREATE — needs ANALYSIS_REPORT
    step3 = StepExecution(
        step_id="s3", capability="GENERATE_CONTENT", ordinal=3,
        input_artifact_types=["ANALYSIS_REPORT"],
        output_artifact_type="DRAFT",
        depends_on=["s2"],
        status=StepStatus.PENDING,
    )
    inputs = store.resolve_inputs(step3, "e1")
    assert len(inputs) == 1
    assert inputs[0].artifact_type == "ANALYSIS_REPORT"

    # Step 3 produces DRAFT
    a3 = store.create_from_result(_content_draft("draft-99"), task_id="t1", execution_id="e1", step_id="s3")
    assert a3 is not None
    assert a3.artifact_type == "DRAFT"
    assert a3.resource_id == "draft-99"
    assert a3.resource_kind == "DRAFT"

    # Verify all 3 artifacts
    all_artifacts = store.find_by_execution("e1")
    types = {a.artifact_type for a in all_artifacts}
    assert types == {"SEARCH_RESULT", "ANALYSIS_REPORT", "DRAFT"}


# ── Scenario 4: failed execution produces no artifact ────────────

def test_failed_step_produces_no_artifact(store: ArtifactStore) -> None:
    a = store.create_from_result(
        _failed_result(),
        task_id="t1", execution_id="e1", step_id="s1",
    )
    assert a is None
    assert store.count("e1") == 0


def test_ok_without_artifact_handle_produces_none(store: ArtifactStore) -> None:
    result = ExecutionResult.success(
        capability="GET_DRAFT",
        tool_name="content.get_draft",
        tool_result={"data": {}},
        artifact=None,  # no artifact
    )
    a = store.create_from_result(result, task_id="t1", execution_id="e1", step_id="s1")
    assert a is None


# ── edge cases ────────────────────────────────────────────────────

def test_resolve_multiple_input_types(store: ArtifactStore) -> None:
    # IMPROVE_WITH_RESEARCH: step 3 needs ANALYSIS_REPORT + DRAFT
    store.create_from_result(_search_result(), task_id="t1", execution_id="e1", step_id="s1")
    store.create_from_result(_analysis_report(), task_id="t1", execution_id="e1", step_id="s2")

    step3 = StepExecution(
        step_id="s3", capability="IMPROVE_CONTENT", ordinal=3,
        input_artifact_types=["ANALYSIS_REPORT", "DRAFT"],
        output_artifact_type="DRAFT",
        depends_on=["s2"],
        status=StepStatus.PENDING,
    )
    # Only ANALYSIS_REPORT exists — DRAFT is missing (would come from another execution)
    inputs = store.resolve_inputs(step3, "e1")
    assert len(inputs) == 1  # only ANALYSIS_REPORT found


def test_resolve_returns_most_recent(store: ArtifactStore) -> None:
    """When multiple artifacts of the same type exist, return the newest.

    We manually set created_at to guarantee ordering.
    """
    from greenbook_assistant_core.artifact.models import Artifact as A

    repo = store._repo  # type: ignore[union-attr]
    repo.save(A(
        artifact_id="a1", task_id="t1", execution_id="e1", step_id="s1",
        artifact_type="SEARCH_RESULT", summary="first",
        created_at="2026-01-01T00:00:00Z",
    ))
    repo.save(A(
        artifact_id="a2", task_id="t1", execution_id="e1", step_id="s1b",
        artifact_type="SEARCH_RESULT", summary="second",
        created_at="2026-01-02T00:00:00Z",
    ))

    found = store.resolve_for_step_type("e1", "SEARCH_RESULT")
    assert found is not None
    assert found.summary == "second"


def test_store_empty_returns_empty(store: ArtifactStore) -> None:
    assert store.find_by_execution("nonexistent") == []
    assert store.find_by_task("nonexistent") == []
    assert store.find_by_step("nonexistent") is None
    assert store.count("nonexistent") == 0


def test_delete_by_execution(store: ArtifactStore) -> None:
    store.create_from_result(_search_result(), task_id="t1", execution_id="e1", step_id="s1")
    store.create_from_result(_content_draft(), task_id="t1", execution_id="e2", step_id="s3")

    assert store.count("e1") == 1
    assert store.count("e2") == 1

    repo = store._repo  # type: ignore[union-attr]
    repo.delete_by_execution("e1")

    assert store.count("e1") == 0
    assert store.count("e2") == 1  # e2 unaffected
