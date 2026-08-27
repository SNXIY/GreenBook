"""Public-safe activity contracts for the GreenBook assistant UI.

These models deliberately describe *business facts* rather than Runtime
implementation details.  A ``UserActivityEvent`` is safe to replay to an
ordinary community user; queue leases, tool names, raw exceptions, prompts,
and transport details do not belong in this contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .tool_result import ResourceRef


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class UserActivityType(StrEnum):
    """Stable user-facing business activities, intentionally not tool names."""

    SEARCH_STARTED = "SEARCH_STARTED"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    SUMMARIZATION_STARTED = "SUMMARIZATION_STARTED"
    SUMMARIZATION_COMPLETED = "SUMMARIZATION_COMPLETED"

    DRAFT_LOOKUP_STARTED = "DRAFT_LOOKUP_STARTED"
    DRAFT_LOOKUP_COMPLETED = "DRAFT_LOOKUP_COMPLETED"
    DRAFT_CREATING = "DRAFT_CREATING"
    DRAFT_CREATED = "DRAFT_CREATED"
    DRAFT_UPDATING = "DRAFT_UPDATING"
    DRAFT_UPDATED = "DRAFT_UPDATED"
    DRAFT_DELETING = "DRAFT_DELETING"
    DRAFT_DELETED = "DRAFT_DELETED"
    POST_DELETING = "POST_DELETING"
    POST_DELETED = "POST_DELETED"

    SCHEDULE_LOOKUP_STARTED = "SCHEDULE_LOOKUP_STARTED"
    SCHEDULE_LOOKUP_COMPLETED = "SCHEDULE_LOOKUP_COMPLETED"
    SCHEDULE_CREATING = "SCHEDULE_CREATING"
    SCHEDULE_CREATED = "SCHEDULE_CREATED"
    SCHEDULE_UPDATING = "SCHEDULE_UPDATING"
    SCHEDULE_UPDATED = "SCHEDULE_UPDATED"
    SCHEDULE_CANCELLING = "SCHEDULE_CANCELLING"
    SCHEDULE_CANCELLED = "SCHEDULE_CANCELLED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"

    REPLYING = "REPLYING"
    REPLIED = "REPLIED"
    ANALYTICS_LOADING = "ANALYTICS_LOADING"
    ANALYTICS_COMPLETED = "ANALYTICS_COMPLETED"

    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NEEDS_SEMANTIC_CONFIRMATION = "NEEDS_SEMANTIC_CONFIRMATION"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"


class UserActivityStatus(StrEnum):
    """Truthful state of an individual user-visible business activity."""

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    WAITING_SEMANTIC_CONFIRMATION = "WAITING_SEMANTIC_CONFIRMATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    RECONCILING = "RECONCILING"


class SemanticConfirmationAction(StrEnum):
    """Typed control operation for a Task-level semantic confirmation."""

    CONFIRM = "CONFIRM"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"


class SemanticConfirmationControl(BaseModel):
    """Public control payload; it is never interpreted as a chat message."""

    model_config = ConfigDict(extra="forbid")

    action: SemanticConfirmationAction
    confirmation_id: str = Field(min_length=1, max_length=128)
    expected_task_version: int = Field(ge=1)
    expected_confirmation_version: int = Field(ge=1)
    # MODIFY only carries a typed, already-compiled patch when one is
    # available.  It is not a free-form message and is not sent to the
    # CommandInterpreter by the confirmation control path.
    modification: dict[str, Any] = Field(default_factory=dict)


class UserActivityMapping(BaseModel):
    """The product activity mapping declared by a semantic business action."""

    model_config = ConfigDict(frozen=True)

    semantic_action: str
    started_type: UserActivityType
    completed_type: UserActivityType
    started_display_key: str
    completed_display_key: str
    requires_verified_postcondition: bool = False


class UserActivityEvent(BaseModel):
    """Durable, replayable and public-safe event consumed by the frontend."""

    model_config = ConfigDict(extra="forbid")

    activity_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    run_id: str | None = None
    task_id: str | None = None
    objective_id: str | None = None
    resource_ref: ResourceRef | None = None
    activity_type: UserActivityType
    status: UserActivityStatus
    display_key: str
    safe_payload: dict[str, Any] = Field(default_factory=dict)
    # Assigned by the Activity store.  It is the SSE id and replay cursor.
    sequence: int = 0
    created_at: str = Field(default_factory=_now_iso)
    verified_at: str | None = None
    terminal: bool = False


# This is intentionally keyed by *SemanticAction*, not by tool name.  Every
# concrete tool continues to declare the semantic action it performs in the
# existing ToolContract catalog, which gives new tools one backend-owned
# activity mapping without teaching the frontend about the tool.
_MAPPINGS: dict[str, UserActivityMapping] = {
    "ANSWER_FROM_KNOWLEDGE": UserActivityMapping(
        semantic_action="ANSWER_FROM_KNOWLEDGE",
        started_type=UserActivityType.SEARCH_STARTED,
        completed_type=UserActivityType.SEARCH_COMPLETED,
        started_display_key="activity.search.started",
        completed_display_key="activity.search.completed",
    ),
    "SEARCH_POSTS": UserActivityMapping(
        semantic_action="SEARCH_POSTS",
        started_type=UserActivityType.SEARCH_STARTED,
        completed_type=UserActivityType.SEARCH_COMPLETED,
        started_display_key="activity.search.started",
        completed_display_key="activity.search.completed",
    ),
    "GET_POST": UserActivityMapping(
        semantic_action="GET_POST",
        started_type=UserActivityType.SEARCH_STARTED,
        completed_type=UserActivityType.SEARCH_COMPLETED,
        started_display_key="activity.search.started",
        completed_display_key="activity.search.completed",
    ),
    "LIST_OWN_POSTS": UserActivityMapping(
        semantic_action="LIST_OWN_POSTS",
        started_type=UserActivityType.SEARCH_STARTED,
        completed_type=UserActivityType.SEARCH_COMPLETED,
        started_display_key="activity.search.started",
        completed_display_key="activity.search.completed",
    ),
    "CREATE_DRAFT": UserActivityMapping(
        semantic_action="CREATE_DRAFT",
        started_type=UserActivityType.DRAFT_CREATING,
        completed_type=UserActivityType.DRAFT_CREATED,
        started_display_key="activity.draft.creating",
        completed_display_key="activity.draft.created",
        requires_verified_postcondition=True,
    ),
    "GET_DRAFT": UserActivityMapping(
        semantic_action="GET_DRAFT",
        started_type=UserActivityType.DRAFT_LOOKUP_STARTED,
        completed_type=UserActivityType.DRAFT_LOOKUP_COMPLETED,
        started_display_key="activity.draft.lookup_started",
        completed_display_key="activity.draft.lookup_completed",
    ),
    "LIST_DRAFTS": UserActivityMapping(
        semantic_action="LIST_DRAFTS",
        started_type=UserActivityType.DRAFT_LOOKUP_STARTED,
        completed_type=UserActivityType.DRAFT_LOOKUP_COMPLETED,
        started_display_key="activity.draft.lookup_started",
        completed_display_key="activity.draft.lookup_completed",
    ),
    "UPDATE_DRAFT": UserActivityMapping(
        semantic_action="UPDATE_DRAFT",
        started_type=UserActivityType.DRAFT_UPDATING,
        completed_type=UserActivityType.DRAFT_UPDATED,
        started_display_key="activity.draft.updating",
        completed_display_key="activity.draft.updated",
        requires_verified_postcondition=True,
    ),
    "DELETE_DRAFT": UserActivityMapping(
        semantic_action="DELETE_DRAFT",
        started_type=UserActivityType.DRAFT_DELETING,
        completed_type=UserActivityType.DRAFT_DELETED,
        started_display_key="activity.draft.deleting",
        completed_display_key="activity.draft.deleted",
        requires_verified_postcondition=True,
    ),
    "DELETE_POST": UserActivityMapping(
        semantic_action="DELETE_POST",
        started_type=UserActivityType.POST_DELETING,
        completed_type=UserActivityType.POST_DELETED,
        started_display_key="activity.post.deleting",
        completed_display_key="activity.post.deleted",
        requires_verified_postcondition=True,
    ),
    "CREATE_SCHEDULE": UserActivityMapping(
        semantic_action="CREATE_SCHEDULE",
        started_type=UserActivityType.SCHEDULE_CREATING,
        completed_type=UserActivityType.SCHEDULE_CREATED,
        started_display_key="activity.schedule.creating",
        completed_display_key="activity.schedule.created",
        requires_verified_postcondition=True,
    ),
    "GET_SCHEDULE": UserActivityMapping(
        semantic_action="GET_SCHEDULE",
        started_type=UserActivityType.SCHEDULE_LOOKUP_STARTED,
        completed_type=UserActivityType.SCHEDULE_LOOKUP_COMPLETED,
        started_display_key="activity.schedule.lookup_started",
        completed_display_key="activity.schedule.lookup_completed",
    ),
    "UPDATE_SCHEDULE": UserActivityMapping(
        semantic_action="UPDATE_SCHEDULE",
        started_type=UserActivityType.SCHEDULE_UPDATING,
        completed_type=UserActivityType.SCHEDULE_UPDATED,
        started_display_key="activity.schedule.updating",
        completed_display_key="activity.schedule.updated",
        requires_verified_postcondition=True,
    ),
    "CANCEL_SCHEDULE": UserActivityMapping(
        semantic_action="CANCEL_SCHEDULE",
        started_type=UserActivityType.SCHEDULE_CANCELLING,
        completed_type=UserActivityType.SCHEDULE_CANCELLED,
        started_display_key="activity.schedule.cancelling",
        completed_display_key="activity.schedule.cancelled",
        requires_verified_postcondition=True,
    ),
    "PUBLISH_NOW": UserActivityMapping(
        semantic_action="PUBLISH_NOW",
        started_type=UserActivityType.PUBLISHING,
        completed_type=UserActivityType.PUBLISHED,
        started_display_key="activity.publish.publishing",
        completed_display_key="activity.publish.published",
        requires_verified_postcondition=True,
    ),
    "LIST_COMMENTS": UserActivityMapping(
        semantic_action="LIST_COMMENTS",
        started_type=UserActivityType.SEARCH_STARTED,
        completed_type=UserActivityType.SEARCH_COMPLETED,
        started_display_key="activity.search.started",
        completed_display_key="activity.search.completed",
    ),
    "REPLY_COMMENT": UserActivityMapping(
        semantic_action="REPLY_COMMENT",
        started_type=UserActivityType.REPLYING,
        completed_type=UserActivityType.REPLIED,
        started_display_key="activity.reply.replying",
        completed_display_key="activity.reply.replied",
        requires_verified_postcondition=True,
    ),
    "GET_POST_PERFORMANCE": UserActivityMapping(
        semantic_action="GET_POST_PERFORMANCE",
        started_type=UserActivityType.ANALYTICS_LOADING,
        completed_type=UserActivityType.ANALYTICS_COMPLETED,
        started_display_key="activity.analytics.loading",
        completed_display_key="activity.analytics.completed",
    ),
    "GET_ACCOUNT_SUMMARY": UserActivityMapping(
        semantic_action="GET_ACCOUNT_SUMMARY",
        started_type=UserActivityType.ANALYTICS_LOADING,
        completed_type=UserActivityType.ANALYTICS_COMPLETED,
        started_display_key="activity.analytics.loading",
        completed_display_key="activity.analytics.completed",
    ),
}


def activity_mapping_for_semantic_action(
    semantic_action: str | None,
) -> UserActivityMapping | None:
    """Return the backend-owned user activity projection for an action."""

    normalized = str(semantic_action or "").strip().upper().replace("-", "_")
    return _MAPPINGS.get(normalized)


__all__ = [
    "SemanticConfirmationAction",
    "SemanticConfirmationControl",
    "UserActivityEvent",
    "UserActivityMapping",
    "UserActivityStatus",
    "UserActivityType",
    "activity_mapping_for_semantic_action",
]
