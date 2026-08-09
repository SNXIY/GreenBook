"""Goal-grouped conversation workspace helpers.

ConversationGoal.id is the only goal identity. Run ids may appear as
``source_run_id`` on entities, never as ``goal:<run_id>`` refs.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.conversation_workspace import (
    ConversationWorkspace,
    WorkspaceEntity,
    WorkspaceGoal,
    materialize_conversation_workspace,
)
from app.domain import ConversationGoal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


ACTIVE_GOAL_STATUSES = frozenset(
    {
        "ACTIVE",
        "WAITING_CLARIFICATION",
        "WAITING_APPROVAL",
        "PAUSED",
    }
)

RESOLUTION_GOAL_STATUSES = frozenset(
    {
        *ACTIVE_GOAL_STATUSES,
        "COMPLETED",
        "FAILED",
    }
)


def goals_for_resolution(
    goals: Iterable[ConversationGoal],
    *,
    include_completed: int = 4,
) -> list[ConversationGoal]:
    """Active goals plus a bounded window of recently completed ones."""

    items = list(goals)
    active = [g for g in items if g.status in ACTIVE_GOAL_STATUSES]
    completed = sorted(
        [g for g in items if g.status in {"COMPLETED", "FAILED", "CANCELLED"}],
        key=lambda g: g.updated_at.isoformat() if g.updated_at else "",
        reverse=True,
    )[:include_completed]
    # Preserve recency order across the union.
    by_id = {g.goal_id: g for g in [*active, *completed]}
    return sorted(
        by_id.values(),
        key=lambda g: g.updated_at.isoformat() if g.updated_at else "",
        reverse=True,
    )


def materialize_goal_workspace(
    *,
    conversation_id: str,
    goals: Iterable[dict[str, Any]],
    runs: Iterable[dict[str, Any]],
    artifacts: Iterable[dict[str, Any]],
    materialized_at: datetime | None = None,
) -> ConversationWorkspace:
    """Materialize workspace with durable ConversationGoal identities."""

    goal_rows = list(goals)
    run_rows = [dict(item) for item in runs]
    # Stamp run.goal_id onto artifacts via run map for entity grouping.
    run_goal = {
        str(item.get("id")): str(item.get("goal_id") or "")
        for item in run_rows
        if item.get("id")
    }
    stamped_artifacts = []
    for row in artifacts:
        payload = dict(row)
        gid = run_goal.get(str(payload.get("run_id") or ""), "")
        if gid and not payload.get("goal_id"):
            payload["goal_id"] = gid
        stamped_artifacts.append(payload)

    workspace = materialize_conversation_workspace(
        conversation_id=conversation_id,
        runs=run_rows,
        artifacts=stamped_artifacts,
        goals=goal_rows,
        materialized_at=materialized_at,
    )
    return workspace


def workspace_goals_from_records(
    records: Iterable[ConversationGoal | dict[str, Any]],
) -> list[WorkspaceGoal]:
    goals: list[WorkspaceGoal] = []
    for item in records:
        if isinstance(item, ConversationGoal):
            goals.append(
                WorkspaceGoal(
                    ref=f"goal:{item.goal_id}",
                    description=(item.summary or item.intent or "社区任务")[:240],
                    status=item.status,
                    source_run_id="",
                    intent=item.intent,
                    updated_at=(
                        item.updated_at.isoformat()
                        if item.updated_at
                        else _now_iso()
                    ),
                    goal_id=item.goal_id,
                )
            )
        else:
            goal_id = str(item.get("id") or item.get("goal_id") or "")
            if not goal_id:
                continue
            goals.append(
                WorkspaceGoal(
                    ref=f"goal:{goal_id}",
                    description=str(item.get("summary") or item.get("intent") or "社区任务")[
                        :240
                    ],
                    status=str(item.get("status") or "ACTIVE"),
                    source_run_id=str(item.get("source_run_id") or ""),
                    intent=(
                        str(item["intent"]) if item.get("intent") else None
                    ),
                    updated_at=str(item.get("updated_at") or _now_iso()),
                    goal_id=goal_id,
                )
            )
    return goals[:12]


def entities_for_goal(
    workspace: ConversationWorkspace,
    goal_id: str,
) -> list[WorkspaceEntity]:
    return [item for item in workspace.entities if item.goal_id == goal_id]


__all__ = [
    "ACTIVE_GOAL_STATUSES",
    "RESOLUTION_GOAL_STATUSES",
    "goals_for_resolution",
    "materialize_goal_workspace",
    "workspace_goals_from_records",
    "entities_for_goal",
]
