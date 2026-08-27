"""CREATE_SCHEDULE Objective-owned Draft authority (Phase 8.3) — T4c-real / T4i.

Within one Task multiple Objectives each own their own Draft.  A CREATE_SCHEDULE
must schedule the current Objective's OWN Draft, never a sibling Objective's
Draft, never the task-global latest/first Draft, and never a cross-Objective
draft_id the model happened to supply.
"""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.planning.contracts import PlanStep


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


def _schedule_step(draft_id: str | None = None) -> PlanStep:
    constraints: dict[str, Any] = {"run_at": "2026-08-17T12:00:00Z"}
    if draft_id:
        constraints["draft_id"] = draft_id
    return PlanStep(
        capability="SCHEDULE_PUBLISH",
        ordinal=1,
        description="schedule a draft",
        output_artifact_type="SCHEDULE",
        tool_name="publication.schedule",
        constraints=constraints,
    )


def _publish_now_step(draft_id: str | None = None) -> PlanStep:
    constraints: dict[str, Any] = {}
    if draft_id:
        constraints["draft_id"] = draft_id
    return PlanStep(
        capability="PUBLISH_NOW",
        ordinal=1,
        description="publish a draft now",
        output_artifact_type="POST",
        tool_name="publication.publish_now",
        constraints=constraints,
    )


def _capture_handler(captured: dict[str, Any]) -> Any:
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        captured["args"] = dict(tool_args)
        return {
            "ok": True,
            "code": "",
            "data": {
                "schedule_id": "sched-1",
                "draft_id": tool_args.get("draft_id"),
            },
        }
    return handler


@pytest.mark.asyncio
async def test_t4c_real_b_schedule_uses_b_own_draft(registry: CapabilityRegistry) -> None:
    """A -> DraftA, B -> DraftB; B CREATE_SCHEDULE -> DraftB, never DraftA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=["draft-B"],
    )
    result = await executor.execute_step(_schedule_step(draft_id="draft-A"))
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"
    assert captured["args"]["draft_id"] != "draft-A"


@pytest.mark.asyncio
async def test_publish_now_binds_objective_owned_draft(
    registry: CapabilityRegistry,
) -> None:
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=["draft-B"],
        active_draft_id="draft-A",
    )
    result = await executor.execute_step(_publish_now_step(draft_id="draft-A"))
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"


@pytest.mark.asyncio
async def test_publish_now_rejects_without_objective_owned_draft(
    registry: CapabilityRegistry,
) -> None:
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=[],
        active_draft_id="draft-A",
    )
    result = await executor.execute_step(_publish_now_step())
    assert result.ok is False
    assert result.error_code == "INVALID_RESOURCE_BINDING"
    assert "args" not in captured


@pytest.mark.asyncio
async def test_t4c_real_b_schedule_fills_missing_draft_from_owned(
    registry: CapabilityRegistry,
) -> None:
    """B CREATE_SCHEDULE with no draft_id -> bound to B's own DraftB."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=["draft-B"],
    )
    result = await executor.execute_step(_schedule_step())
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"


@pytest.mark.asyncio
async def test_t4i_b_schedule_normalizes_wrong_draft_to_unique_owned(
    registry: CapabilityRegistry,
) -> None:
    """Model wrongly supplies draft_id=DraftA; B owns unique DraftB -> DraftB."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=["draft-B"],
    )
    result = await executor.execute_step(_schedule_step(draft_id="draft-A"))
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"
    assert captured["args"]["draft_id"] != "draft-A"


@pytest.mark.asyncio
async def test_t4i_b_schedule_rejects_when_no_owned_draft(
    registry: CapabilityRegistry,
) -> None:
    """B owns no Draft -> controlled INVALID_RESOURCE_BINDING, no execution."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=[],
    )
    result = await executor.execute_step(_schedule_step(draft_id="draft-A"))
    assert result.ok is False
    assert result.error_code == "INVALID_RESOURCE_BINDING"
    assert "args" not in captured  # the tool was never invoked


@pytest.mark.asyncio
async def test_explicit_dependent_schedule_consumes_predecessor_draft(
    registry: CapabilityRegistry,
) -> None:
    """A dependent schedule may consume exactly one verified predecessor Draft."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="C",
        objective_draft_ids=[],
        objective_dependency_draft_ids=["draft-B"],
    )
    result = await executor.execute_step(_schedule_step())
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"


@pytest.mark.asyncio
async def test_dependent_schedule_rejects_ambiguous_predecessor_drafts(
    registry: CapabilityRegistry,
) -> None:
    """More than one dependency Draft remains a controlled no-side-effect reject."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="C",
        objective_draft_ids=[],
        objective_dependency_draft_ids=["draft-A", "draft-B"],
    )
    result = await executor.execute_step(_schedule_step())
    assert result.ok is False
    assert result.error_code == "INVALID_RESOURCE_BINDING"
    assert "args" not in captured


@pytest.mark.asyncio
async def test_t4_no_task_global_draft_fallback_for_business_objective(
    registry: CapabilityRegistry,
) -> None:
    """Business Objective B never falls back to task-global latest Draft A."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=["draft-B"],
        active_draft_id="draft-A",  # task-global latest Draft is A
    )
    result = await executor.execute_step(_schedule_step())
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-B"
    assert captured["args"]["draft_id"] != "draft-A"


@pytest.mark.asyncio
async def test_legacy_no_objective_keeps_task_scoped_fallback(
    registry: CapabilityRegistry,
) -> None:
    """Legacy path (no objective_id) keeps the task-scoped Draft fallback."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id=None,
        active_draft_id="draft-A",
    )
    result = await executor.execute_step(_schedule_step())
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-A"


# ── W2 mutation: Objective-scoped MANAGE_DRAFT / MANAGE_SCHEDULE ──────────


def _update_draft_step(draft_id: str | None = None) -> PlanStep:
    constraints: dict[str, Any] = {"title": "new attractive title"}
    if draft_id:
        constraints["draft_id"] = draft_id
    return PlanStep(
        capability="MANAGE_DRAFT",
        ordinal=1,
        tool_name="content.update_draft",
        constraints=constraints,
    )


def _update_schedule_step(schedule_id: str | None = None) -> PlanStep:
    constraints: dict[str, Any] = {"run_at": "2026-08-18T08:00:00Z"}
    if schedule_id:
        constraints["schedule_id"] = schedule_id
    return PlanStep(
        capability="MANAGE_SCHEDULE",
        ordinal=1,
        tool_name="publication.update_schedule",
        constraints=constraints,
    )


@pytest.mark.asyncio
async def test_w2_update_draft_binds_objective_owned_draft(
    registry: CapabilityRegistry,
) -> None:
    """MANAGE_DRAFT resolves draft_id to the Objective's own DraftA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_draft_ids=["draft-A"],
    )
    result = await executor.execute_step(_update_draft_step())
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-A"
    assert captured["args"]["title"] == "new attractive title"


@pytest.mark.asyncio
async def test_w2_update_draft_normalizes_wrong_cross_objective_id(
    registry: CapabilityRegistry,
) -> None:
    """Model wrongly supplies DraftB -> normalized to Objective A's DraftA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_draft_ids=["draft-A"],
    )
    result = await executor.execute_step(_update_draft_step(draft_id="draft-B"))
    assert result.ok is True
    assert captured["args"]["draft_id"] == "draft-A"
    assert captured["args"]["draft_id"] != "draft-B"


@pytest.mark.asyncio
async def test_w2_update_schedule_binds_objective_owned_schedule(
    registry: CapabilityRegistry,
) -> None:
    """MANAGE_SCHEDULE resolves schedule_id to the Objective's own ScheduleA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_schedule_ids=["schedule-A"],
    )
    result = await executor.execute_step(_update_schedule_step())
    assert result.ok is True
    assert captured["args"]["schedule_id"] == "schedule-A"
    assert captured["args"]["run_at"] == "2026-08-18T08:00:00Z"


@pytest.mark.asyncio
async def test_w2_update_draft_rejects_when_no_owned_draft(
    registry: CapabilityRegistry,
) -> None:
    """No owned Draft -> controlled INVALID_RESOURCE_BINDING, no mutation."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="B",
        objective_draft_ids=[],
        active_draft_id="draft-A",  # task-global must NOT be used
    )
    result = await executor.execute_step(_update_draft_step())
    assert result.ok is False
    assert result.error_code == "INVALID_RESOURCE_BINDING"
    assert "args" not in captured


# ── W3: Objective-scoped CANCEL_SCHEDULE ──────────────────────────────


def _cancel_schedule_step(schedule_id: str | None = None) -> PlanStep:
    constraints: dict[str, Any] = {}
    if schedule_id:
        constraints["schedule_id"] = schedule_id
    return PlanStep(
        capability="CANCEL_SCHEDULE",
        ordinal=1,
        tool_name="publication.cancel_schedule",
        constraints=constraints,
    )


@pytest.mark.asyncio
async def test_w3_cancel_schedule_binds_objective_owned_schedule(
    registry: CapabilityRegistry,
) -> None:
    """CANCEL_SCHEDULE resolves schedule_id to the Objective's own ScheduleA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_schedule_ids=["schedule-A"],
    )
    result = await executor.execute_step(_cancel_schedule_step())
    assert result.ok is True
    assert captured["args"]["schedule_id"] == "schedule-A"


@pytest.mark.asyncio
async def test_w3_cancel_schedule_normalizes_wrong_cross_objective_id(
    registry: CapabilityRegistry,
) -> None:
    """Model wrongly supplies ScheduleB -> normalized to Objective A's ScheduleA."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_schedule_ids=["schedule-A"],
    )
    result = await executor.execute_step(_cancel_schedule_step(schedule_id="schedule-B"))
    assert result.ok is True
    assert captured["args"]["schedule_id"] == "schedule-A"
    assert captured["args"]["schedule_id"] != "schedule-B"


@pytest.mark.asyncio
async def test_w3_cancel_schedule_rejects_when_no_owned_schedule(
    registry: CapabilityRegistry,
) -> None:
    """No owned Schedule -> controlled INVALID_RESOURCE_BINDING, B not cancelled."""
    captured: dict[str, Any] = {}
    executor = CapabilityExecutor(
        registry,
        _capture_handler(captured),
        objective_id="A",
        objective_schedule_ids=[],
        active_schedule_id="schedule-B",  # task-global must NOT be used
    )
    result = await executor.execute_step(_cancel_schedule_step(schedule_id="schedule-B"))
    assert result.ok is False
    assert result.error_code == "INVALID_RESOURCE_BINDING"
    assert "args" not in captured
