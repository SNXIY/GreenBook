"""Shared event contracts for the GreenBook Agent Runtime.

``BusinessEvent`` is the durable Kafka-style envelope. The ``EVENT_*``
constants are the canonical ``event_type`` vocabulary pushed over the Run SSE
stream (``GET /api/v1/agent/runs/{run_id}/stream``) and consumed by the
frontend (``AgentPanel``). Producers live in ``agent_core`` (AgentLoop
activity callbacks) and the Agent API (run lifecycle / mid-turn follow-up);
consumers live in the API projection and the web UI. Keeping every name here
prevents producer/consumer drift — the exact payload shapes are documented in
``docs/architecture/SERVICE_COMMUNICATION.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class BusinessEvent(BaseModel):
    """Async business event emitted to Kafka."""

    event_type: str
    event_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Run business-activity vocabulary (single source of truth) ──────────────
# The first meaningful activity is pushed before any Execution exists so the
# frontend never depends on execution_id.

# The agent understood the request; shown before execution starts so a wrong
# understanding can be stopped early. payload: {summary, tasks[]}.
EVENT_UNDERSTANDING = "UNDERSTANDING"

# A semantic action was decided. payload: {semantic_action, goal_id, task_id}
# plus {phase: STARTED|SUCCEEDED|FAILED} on the projected terminal copy.
EVENT_SEMANTIC_ACTION = "SEMANTIC_ACTION_SELECTED"

# Internal adapter event: a semantic action finished. The API projects it into
# EVENT_SEMANTIC_ACTION(phase=...) plus an optional EVENT_PARTIAL_RESULT.
EVENT_ACTION_COMPLETED = "ACTION_COMPLETED"

# A safe business summary for a finished action ("找到 N 篇" / "草稿已生成" /
# "已安排发布时间"). payload: {title, count?, run_at?, goal_id, task_id}.
EVENT_PARTIAL_RESULT = "PARTIAL_RESULT"

# A mid-turn user message was queued behind this Run (nanobot-style injection).
# payload: {run_id, follow_up_run_id, message}.
EVENT_FOLLOW_UP_QUEUED = "FOLLOW_UP_QUEUED"

# ── Run lifecycle (produced by the AgentRunner / API reconciliation)

EVENT_REASONING_STARTED = "REASONING_STARTED"
EVENT_TOOL_STARTED = "TOOL_STARTED"
EVENT_OBSERVATION = "OBSERVATION_RECEIVED"
EVENT_WAITING_APPROVAL = "WAITING_APPROVAL"
EVENT_RUN_COMPLETED = "RUN_COMPLETED"
EVENT_RUN_FAILED = "RUN_FAILED"

__all__ = [
    "BusinessEvent",
    "EVENT_ACTION_COMPLETED",
    "EVENT_FOLLOW_UP_QUEUED",
    "EVENT_OBSERVATION",
    "EVENT_PARTIAL_RESULT",
    "EVENT_REASONING_STARTED",
    "EVENT_RUN_COMPLETED",
    "EVENT_RUN_FAILED",
    "EVENT_SEMANTIC_ACTION",
    "EVENT_TOOL_STARTED",
    "EVENT_UNDERSTANDING",
    "EVENT_WAITING_APPROVAL",
]
