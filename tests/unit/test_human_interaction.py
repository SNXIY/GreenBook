"""Phase 6.5 tests for HumanInteraction infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from greenbook_assistant_core.execution.models import ExecutionStatus
from greenbook_assistant_core.human.manager import HumanInteractionManager
from greenbook_assistant_core.human.models import (
    HumanInteractionRequest,
    HumanInteractionResponse,
    InteractionStatus,
    InteractionType,
)
from greenbook_assistant_core.human.store import InteractionStore


# ── Models ──────────────────────────────────────────────────────

def test_request_defaults() -> None:
    req = HumanInteractionRequest(
        execution_id="e1", type=InteractionType.APPROVAL,
        question="确认发布?",
    )
    assert req.interaction_id
    assert req.status == InteractionStatus.PENDING
    assert req.type == InteractionType.APPROVAL


def test_response_defaults() -> None:
    resp = HumanInteractionResponse(
        interaction_id="i1", decision="ACCEPT",
    )
    assert resp.decision == "ACCEPT"


def test_interaction_type_values() -> None:
    assert InteractionType.APPROVAL.value == "APPROVAL"
    assert InteractionType.CLARIFICATION.value == "CLARIFICATION"
    assert InteractionType.INPUT.value == "INPUT"


def test_waiting_human_status_exists() -> None:
    assert ExecutionStatus.WAITING_HUMAN.value == "WAITING_HUMAN"
    # Legacy still exists
    assert ExecutionStatus.WAITING_APPROVAL.value == "WAITING_APPROVAL"


# ── Store ────────────────────────────────────────────────────────

def test_store_save_and_find() -> None:
    store = InteractionStore()
    req = HumanInteractionRequest(execution_id="e1")
    store.save(req)
    assert store.find_by_id(req.interaction_id) is not None


def test_store_find_by_execution() -> None:
    store = InteractionStore()
    req1 = HumanInteractionRequest(execution_id="e1")
    req2 = HumanInteractionRequest(execution_id="e1")
    req3 = HumanInteractionRequest(execution_id="e2")
    store.save(req1)
    store.save(req2)
    store.save(req3)
    assert len(store.find_by_execution("e1")) == 2
    assert len(store.find_by_execution("e2")) == 1


def test_store_update_on_response() -> None:
    store = InteractionStore()
    req = HumanInteractionRequest(execution_id="e1")
    store.save(req)

    resp = HumanInteractionResponse(
        interaction_id=req.interaction_id, decision="SELECT",
        selected_value="task-a",
    )
    updated = store.update(req.interaction_id, resp)
    assert updated is not None
    assert updated.status == InteractionStatus.RESPONDED
    assert updated.context["decision"] == "SELECT"
    assert updated.context["selected_value"] == "task-a"


def test_store_expire() -> None:
    store = InteractionStore()
    req = HumanInteractionRequest(execution_id="e1")
    store.save(req)
    expired = store.expire(req.interaction_id)
    assert expired is not None
    assert expired.status == InteractionStatus.EXPIRED


def test_store_clear() -> None:
    store = InteractionStore()
    store.save(HumanInteractionRequest(execution_id="e1"))
    assert store.count() == 1
    store.clear()
    assert store.count() == 0


# ── Manager — pause ─────────────────────────────────────────────

def test_manager_pause() -> None:
    mgr = HumanInteractionManager()
    req = mgr.pause(
        execution_id="e1", type=InteractionType.APPROVAL,
        question="确认发布?",
    )
    assert req.status == InteractionStatus.PENDING
    assert mgr.store.count() == 1


def test_manager_pause_with_options() -> None:
    mgr = HumanInteractionManager()
    req = mgr.pause(
        execution_id="e1", type=InteractionType.CLARIFICATION,
        question="请选择任务:", options=[
            {"value": "task-a", "label": "Java文章"},
            {"value": "task-b", "label": "Python文章"},
        ],
    )
    assert len(req.options) == 2


# ── Manager — resume ────────────────────────────────────────────

def test_manager_resume() -> None:
    mgr = HumanInteractionManager()
    req = mgr.pause(execution_id="e1", type=InteractionType.APPROVAL)

    resp = HumanInteractionResponse(
        interaction_id=req.interaction_id, decision="ACCEPT",
    )
    resumed = mgr.resume(req.interaction_id, resp)
    assert resumed is not None
    assert resumed.status == InteractionStatus.RESPONDED


def test_manager_resume_not_found() -> None:
    mgr = HumanInteractionManager()
    resp = HumanInteractionResponse(interaction_id="nonexistent")
    assert mgr.resume("nonexistent", resp) is None


def test_manager_resume_already_responded() -> None:
    mgr = HumanInteractionManager()
    req = mgr.pause(execution_id="e1")
    resp = HumanInteractionResponse(interaction_id=req.interaction_id)
    mgr.resume(req.interaction_id, resp)
    # Second resume should fail
    assert mgr.resume(req.interaction_id, resp) is None


# ── Manager — expiry ────────────────────────────────────────────

def test_manager_expire_stale() -> None:
    mgr = HumanInteractionManager()
    req = mgr.pause(execution_id="e1")

    # Force expire by setting expires_at in the past
    stored = mgr.store.find_by_id(req.interaction_id)
    assert stored is not None
    stored.expires_at = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    mgr.store.save(stored)

    expired = mgr.expire_stale()
    assert req.interaction_id in expired
    assert mgr.store.find_by_id(req.interaction_id).status == InteractionStatus.EXPIRED


def test_manager_expire_stale_none_pending() -> None:
    mgr = HumanInteractionManager()
    expired = mgr.expire_stale()
    assert expired == []
