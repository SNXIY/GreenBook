from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain import TargetBinding, TargetContext


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceEntity(WorkspaceModel):
    """A typed, authorized reference candidate from an immutable Artifact."""

    ref: str
    kind: str
    entity_id: str
    label: str
    status: str
    source_run_id: str
    source_artifact_id: str
    goal_id: str | None = None
    content_sha256: str | None = None
    content_artifact_id: str | None = None
    content_artifact_version: int | None = None
    related_refs: list[str] = Field(default_factory=list, max_length=12)
    actionable: bool = False
    created_at: str


class WorkspaceGoal(WorkspaceModel):
    ref: str
    description: str
    status: str
    source_run_id: str = ""
    intent: str | None = None
    updated_at: str
    goal_id: str | None = None


class ConversationWorkspace(WorkspaceModel):
    """Compact materialized view of the conversation's event/artifact history.

    Messages are natural-language context. This object is the control-plane
    context: goals and candidate entity identifiers originate from persisted
    Run and Artifact rows and can therefore be validated before a side effect.
    ``target_context`` is the bound business context; ``entities`` are only
    candidates and ``focus_refs`` is only a ranking hint.
    """

    schema_version: Literal["conversation-workspace-v1"] = (
        "conversation-workspace-v1"
    )
    conversation_id: str
    revision: int = 0
    active_goal_ref: str | None = None
    active_target: TargetBinding | None = None
    target_context: TargetContext = Field(default_factory=TargetContext)
    focus_refs: list[str] = Field(default_factory=list, max_length=12)
    focus_goal_refs: list[str] = Field(default_factory=list, max_length=8)
    open_loops: list[str] = Field(default_factory=list, max_length=12)
    recent_goals: list[WorkspaceGoal] = Field(default_factory=list, max_length=12)
    entities: list[WorkspaceEntity] = Field(default_factory=list, max_length=30)
    last_failure: dict[str, str] | None = None
    materialized_at: str

    def entity(self, ref: str) -> WorkspaceEntity | None:
        return next((item for item in self.entities if item.ref == ref), None)

    def selected(self, refs: Iterable[str]) -> list[WorkspaceEntity]:
        allowed = set(refs)
        return [item for item in self.entities if item.ref in allowed]

    def latest_actionable(self, kind: str | None = None) -> WorkspaceEntity | None:
        return next(
            (
                item
                for item in self.entities
                if item.actionable and (kind is None or item.kind == kind)
            ),
            None,
        )

    def model_context(self) -> dict[str, Any]:
        """Return only bounded fields that are useful to a model."""

        return {
            "schema_version": self.schema_version,
            "active_goal_ref": self.active_goal_ref,
            "target_context": self.target_context.model_dump(mode="json"),
            "focus_refs": self.focus_refs,
            "focus_goal_refs": self.focus_goal_refs,
            "open_loops": self.open_loops,
            "recent_goals": [item.model_dump(mode="json") for item in self.recent_goals],
            "entities": [item.model_dump(mode="json") for item in self.entities],
            "entities_by_goal": _entities_by_goal(self.entities),
            "last_failure": self.last_failure,
            "reference_policy": (
                "referenced_entities may contain only an entity ref listed above; "
                "goal refs must use goal:<ConversationGoal.id>; "
                "identifiers are context candidates, not authorization"
            ),
        }


_ENTITY_SPECS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], str, bool]] = {
    "CONTENT_DRAFT": (
        "DRAFT",
        ("draft_id", "draftId"),
        ("title",),
        "READY",
        True,
    ),
    "POST_CONTENT": (
        "POST",
        ("post_id", "postId", "id"),
        ("title",),
        "AVAILABLE",
        True,
    ),
    "POST_SUMMARY": (
        "POST",
        ("post_id", "postId"),
        ("title",),
        "AVAILABLE",
        True,
    ),
    "PUBLICATION_RECEIPT": (
        "POST",
        ("post_id", "postId", "id"),
        ("title",),
        "PUBLISHED",
        True,
    ),
    "SCHEDULE_RECEIPT": (
        "SCHEDULE",
        ("action_id", "actionId", "schedule_id", "scheduleId", "draft_id"),
        ("title", "run_at", "runAt"),
        "SCHEDULED",
        True,
    ),
    "COMMENT_RECEIPT": (
        "COMMENT",
        ("comment_id", "commentId", "id"),
        ("content",),
        "POSTED",
        True,
    ),
    "DELETION_RECEIPT": (
        "POST",
        ("post_id", "postId", "id"),
        ("title",),
        "DELETED",
        False,
    ),
    "ENGAGEMENT_ANALYSIS": (
        "ANALYSIS",
        (),
        ("summary", "title"),
        "COMPLETED",
        False,
    ),
    "TOPIC_ANALYSIS": (
        "ANALYSIS",
        (),
        ("summary", "title"),
        "COMPLETED",
        False,
    ),
    "USER_SET": (
        "USER_SET",
        (),
        ("summary", "title"),
        "COMPLETED",
        False,
    ),
    "POST_SEARCH_RESULTS": (
        "SEARCH_RESULT",
        (),
        ("query", "summary"),
        "COMPLETED",
        False,
    ),
}


def materialize_conversation_workspace(
    *,
    conversation_id: str,
    runs: Iterable[dict[str, Any]],
    artifacts: Iterable[dict[str, Any]],
    goals: Iterable[dict[str, Any]] | None = None,
    materialized_at: datetime | None = None,
) -> ConversationWorkspace:
    """Reduce persisted runs and immutable artifacts into a bounded workspace."""

    run_rows = sorted(
        (dict(item) for item in runs),
        key=lambda item: _timestamp(item.get("updated_at") or item.get("created_at")),
        reverse=True,
    )[:12]
    run_by_id = {str(item.get("id")): item for item in run_rows if item.get("id")}
    run_goal_id = {
        str(item["id"]): str(item.get("goal_id") or "")
        for item in run_rows
        if item.get("id")
    }

    goal_rows = [dict(item) for item in (goals or [])]
    if goal_rows:
        workspace_goals = [
            WorkspaceGoal(
                ref=f"goal:{row['id']}",
                description=_label(
                    row.get("summary") or row.get("intent"),
                    "社区任务",
                    240,
                ),
                status=str(row.get("status") or "ACTIVE"),
                source_run_id=str(row.get("source_run_id") or ""),
                intent=(str(row["intent"]) if row.get("intent") else None),
                updated_at=_iso(row.get("updated_at") or row.get("created_at")),
                goal_id=str(row["id"]),
            )
            for row in sorted(
                goal_rows,
                key=lambda item: _timestamp(
                    item.get("updated_at") or item.get("created_at")
                ),
                reverse=True,
            )
            if row.get("id")
        ][:12]
    else:
        # Derive durable goal refs from run.goal_id when explicit goal rows
        # were not supplied. Never invent goal:<run_id> when goal_id exists.
        seen_goal_ids: set[str] = set()
        workspace_goals = []
        for row in run_rows:
            gid = str(row.get("goal_id") or "")
            if not gid or gid in seen_goal_ids:
                continue
            seen_goal_ids.add(gid)
            workspace_goals.append(
                WorkspaceGoal(
                    ref=f"goal:{gid}",
                    description=_label(row.get("prompt"), "社区任务", 240),
                    status=str(row.get("status") or "UNKNOWN"),
                    source_run_id=str(row["id"]),
                    intent=(str(row["intent"]) if row.get("intent") else None),
                    updated_at=_iso(row.get("updated_at") or row.get("created_at")),
                    goal_id=gid,
                )
            )
            if len(workspace_goals) >= 12:
                break
        if not workspace_goals:
            # Legacy fallback for fixtures that omit goal_id entirely.
            workspace_goals = [
                WorkspaceGoal(
                    ref=f"goal:{row['id']}",
                    description=_label(row.get("prompt"), "社区任务", 240),
                    status=str(row.get("status") or "UNKNOWN"),
                    source_run_id=str(row["id"]),
                    intent=(str(row["intent"]) if row.get("intent") else None),
                    updated_at=_iso(row.get("updated_at") or row.get("created_at")),
                    goal_id=str(row["id"]),
                )
                for row in run_rows
                if row.get("id")
            ]

    entity_rows: list[WorkspaceEntity] = []
    seen_refs: set[str] = set()
    artifact_rows = sorted(
        (dict(item) for item in artifacts),
        key=lambda item: _timestamp(item.get("created_at")),
        reverse=True,
    )
    for row in artifact_rows:
        artifact_type = str(row.get("artifact_type") or "")
        spec = _ENTITY_SPECS.get(artifact_type)
        content = row.get("content")
        if spec is None or not isinstance(content, dict):
            continue
        kind, id_fields, label_fields, default_status, default_actionable = spec
        entity_id = _first(content, id_fields) or str(row.get("id") or "")
        if not entity_id:
            continue
        ref = f"{kind.lower()}:{entity_id}"
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        label = _first(content, label_fields)
        status = str(content.get("status") or default_status).upper()
        actionable = default_actionable and status not in {
            "PUBLISHED",
            "CANCELLED",
            "COMPLETED",
            "RUNNING",
            "DELETED",
            "FAILED",
            "SUPERSEDED",
        }
        source_run_id = str(row.get("run_id") or "")
        entity_goal_id = str(
            row.get("goal_id") or run_goal_id.get(source_run_id) or ""
        ) or None
        entity_rows.append(
            WorkspaceEntity(
                ref=ref,
                kind=kind,
                entity_id=entity_id,
                label=_label(label, f"{kind} {entity_id}", 120),
                status=status,
                source_run_id=source_run_id,
                source_artifact_id=str(row.get("id") or ""),
                goal_id=entity_goal_id,
                content_sha256=(
                    str(_first(content, ("content_sha256", "contentSha256")))
                    or None
                ),
                content_artifact_id=(
                    str(_first(content, ("content_artifact_id",))) or None
                ),
                content_artifact_version=(
                    int(content.get("content_artifact_version"))
                    if content.get("content_artifact_version") is not None
                    else None
                ),
                actionable=actionable,
                created_at=_iso(row.get("created_at")),
            )
        )
        if len(entity_rows) >= 30:
            break

    _close_consumed_drafts(entity_rows, artifact_rows)
    _link_related_entities(entity_rows, artifact_rows)
    _decorate_schedule_labels(entity_rows)
    focus = [item.ref for item in entity_rows if item.actionable][:12]
    open_loops = [
        item.ref
        for item in entity_rows
        if item.kind == "DRAFT" and item.status == "READY"
    ][:12]
    failed = next(
        (row for row in run_rows if str(row.get("status")) == "FAILED"),
        None,
    )
    active_goal = _active_goal(workspace_goals, entity_rows, run_by_id, run_goal_id)
    now = materialized_at or datetime.now(timezone.utc)
    failed_goal_ref = None
    if failed is not None:
        failed_gid = str(failed.get("goal_id") or run_goal_id.get(str(failed.get("id") or ""), "") or failed.get("id") or "")
        failed_goal_ref = f"goal:{failed_gid}" if failed_gid else None
    focus_goal_refs = [
        item.ref
        for item in workspace_goals
        if item.status
        in {
            "ACTIVE",
            "WAITING_CLARIFICATION",
            "WAITING_APPROVAL",
            "PAUSED",
            "QUEUED",
            "RUNNING",
        }
    ][:8]
    if active_goal and active_goal not in focus_goal_refs:
        focus_goal_refs = [active_goal, *focus_goal_refs][:8]
    return ConversationWorkspace(
        conversation_id=conversation_id,
        revision=len(run_rows) + len(artifact_rows) + len(workspace_goals),
        active_goal_ref=active_goal,
        focus_refs=focus,
        focus_goal_refs=focus_goal_refs,
        open_loops=open_loops,
        recent_goals=workspace_goals,
        entities=entity_rows,
        last_failure=(
            {
                "goal_ref": failed_goal_ref or f"goal:{failed['id']}",
                "message": _label(failed.get("error"), "任务执行失败", 500),
            }
            if failed is not None
            else None
        ),
        materialized_at=now.isoformat(),
    )


def _close_consumed_drafts(
    entities: list[WorkspaceEntity],
    artifacts: list[dict[str, Any]],
) -> None:
    consumed: dict[str, str] = {}
    seen_schedule_actions: set[str] = set()
    for row in artifacts:
        kind = str(row.get("artifact_type") or "")
        if kind not in {"PUBLICATION_RECEIPT", "SCHEDULE_RECEIPT", "CONTENT_DRAFT"}:
            continue
        content = row.get("content")
        if not isinstance(content, dict):
            continue
        if kind == "CONTENT_DRAFT":
            superseded = _first(
                content, ("supersedes_draft_id", "supersedesDraftId")
            )
            if superseded:
                consumed.setdefault(str(superseded), "SUPERSEDED")
            continue
        if kind == "SCHEDULE_RECEIPT":
            action_id = _first(content, ("action_id", "actionId"))
            if action_id and action_id in seen_schedule_actions:
                continue
            if action_id:
                seen_schedule_actions.add(action_id)
            if str(content.get("status") or "SCHEDULED").upper() not in {
                "SCHEDULED",
                "RETRYING",
            }:
                continue
        draft_id = _first(content, ("draft_id", "draftId", "post_id", "postId"))
        if draft_id:
            consumed.setdefault(str(draft_id), (
                "PUBLISHED" if kind == "PUBLICATION_RECEIPT" else "SCHEDULED"
            ))
    for index, entity in enumerate(entities):
        status = consumed.get(entity.entity_id)
        if entity.kind == "DRAFT" and status:
            entities[index] = entity.model_copy(
                update={
                    "status": status,
                    "actionable": status not in {"PUBLISHED", "SUPERSEDED"},
                }
            )


def _link_related_entities(
    entities: list[WorkspaceEntity],
    artifacts: list[dict[str, Any]],
) -> None:
    by_ref = {item.ref: index for index, item in enumerate(entities)}
    relations: dict[str, set[str]] = {item.ref: set() for item in entities}
    seen_schedule_actions: set[str] = set()
    for row in artifacts:
        content = row.get("content")
        if not isinstance(content, dict):
            continue
        kind = str(row.get("artifact_type") or "")
        if kind == "SCHEDULE_RECEIPT":
            action_id = _first(content, ("action_id", "actionId"))
            if action_id in seen_schedule_actions:
                continue
            if action_id:
                seen_schedule_actions.add(action_id)
            draft_id = _first(content, ("draft_id", "draftId"))
            left, right = f"schedule:{action_id}", f"draft:{draft_id}"
        elif kind == "CONTENT_DRAFT":
            draft_id = _first(content, ("draft_id", "draftId"))
            previous_id = _first(
                content, ("supersedes_draft_id", "supersedesDraftId")
            )
            if not previous_id:
                continue
            left, right = f"draft:{draft_id}", f"draft:{previous_id}"
        else:
            continue
        if left in by_ref and right in by_ref:
            relations[left].add(right)
            relations[right].add(left)
    for ref, values in relations.items():
        if values:
            index = by_ref[ref]
            entities[index] = entities[index].model_copy(
                update={"related_refs": sorted(values)[:12]}
            )


def _decorate_schedule_labels(entities: list[WorkspaceEntity]) -> None:
    """Make schedule choices understandable by showing their draft title."""
    by_ref = {item.ref: item for item in entities}
    for index, entity in enumerate(entities):
        if entity.kind != "SCHEDULE":
            continue
        draft = next(
            (
                by_ref[ref]
                for ref in entity.related_refs
                if ref in by_ref and by_ref[ref].kind == "DRAFT"
            ),
            None,
        )
        if draft is None:
            continue
        run_at = entity.label
        entities[index] = entity.model_copy(
            update={"label": f"定时发布：{draft.label}（{run_at}）"}
        )


def _active_goal(
    goals: list[WorkspaceGoal],
    entities: list[WorkspaceEntity],
    run_by_id: dict[str, dict[str, Any]],
    run_goal_id: dict[str, str] | None = None,
) -> str | None:
    run_goal_id = run_goal_id or {}
    open_entity = next(
        (
            item
            for item in entities
            if item.actionable and item.status in {"READY", "SCHEDULED", "AVAILABLE"}
        ),
        None,
    )
    if open_entity is not None:
        if open_entity.goal_id:
            return f"goal:{open_entity.goal_id}"
        gid = run_goal_id.get(open_entity.source_run_id)
        if gid:
            return f"goal:{gid}"
    active = next(
        (
            goal
            for goal in goals
            if goal.status
            in {
                "ACTIVE",
                "WAITING_CLARIFICATION",
                "WAITING_APPROVAL",
                "PAUSED",
                "QUEUED",
                "RUNNING",
                "WAITING_DEPENDENCY",
            }
        ),
        None,
    )
    return active.ref if active is not None else (goals[0].ref if goals else None)


def _entities_by_goal(entities: list[WorkspaceEntity]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in entities:
        key = item.goal_id or "_unbound"
        grouped.setdefault(key, []).append(
            {
                "ref": item.ref,
                "kind": item.kind,
                "label": item.label,
                "status": item.status,
                "actionable": item.actionable,
            }
        )
    return grouped


def _first(content: dict[str, Any], fields: Iterable[str]) -> str:
    for field in fields:
        value = content.get(field)
        if value not in {None, ""}:
            return str(value)
    return ""


def _label(value: Any, fallback: str, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    return (cleaned or fallback)[:limit]


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()
