from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorDecisionStatus,
    CreatorRunStatus,
    CreatorTaskKind,
    CreatorTaskStatus,
)
from app.creator.runtime.models import ArtifactKind
from app.creator.studio.models import (
    CreatorBranch,
    CreatorDeliveryChannel,
    CreatorMaterialKind,
    CreatorSuggestion,
    CreatorSuggestionKind,
)


class CreatorApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreatorApiPrincipal(CreatorApiModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    roles: frozenset[str] = frozenset()


class CreatorSourceDraftRequest(CreatorApiModel):
    title: str = Field(min_length=1, max_length=512)
    body_markdown: str = Field(min_length=1, max_length=500_000)


class CreatorTaskConstraintsRequest(CreatorApiModel):
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    format: Literal["ARTICLE", "POST", "THREAD"] = "ARTICLE"
    target_length: int = Field(default=1800, ge=300, le=20_000)
    interaction_mode: Literal["GUIDED", "ADAPTIVE", "AUTO"] = "GUIDED"
    audience: str = Field(default="", max_length=500)
    reader_takeaway: str = Field(default="", max_length=1_000)
    tone: Literal["PRACTICAL", "PROFESSIONAL", "CONVERSATIONAL", "SHARP"] = "PRACTICAL"
    key_points: tuple[str, ...] = Field(default=(), max_length=20)
    reference_notes: str = Field(default="", max_length=12_000)
    draft: CreatorSourceDraftRequest | None = None

    def runtime_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "language": self.language,
            "format": self.format,
            "target_length": self.target_length,
            "approval_mode": self.interaction_mode,
            "tone": self.tone,
        }
        if self.audience:
            values["audience"] = self.audience
        if self.reader_takeaway:
            values["reader_takeaway"] = self.reader_takeaway
        if self.key_points:
            values["key_points"] = self.key_points
        if self.reference_notes:
            values["reference_notes"] = self.reference_notes
        if self.draft is not None:
            values["draft"] = self.draft.model_dump(mode="json")
        return values


class CreatorSourceScopeRequest(CreatorApiModel):
    include_creator_profile: bool = True
    include_creator_history: bool = True
    include_community_posts: bool = True
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    content_types: tuple[str, ...] = Field(default=(), max_length=10)

    def runtime_values(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CreatorTaskCreateRequest(CreatorApiModel):
    kind: CreatorTaskKind = CreatorTaskKind.CREATE_CONTENT
    goal: str = Field(min_length=3, max_length=20_000)
    session_id: str | None = Field(default=None, max_length=128)
    constraints: CreatorTaskConstraintsRequest = Field(
        default_factory=CreatorTaskConstraintsRequest
    )
    source_scope: CreatorSourceScopeRequest = Field(
        default_factory=CreatorSourceScopeRequest
    )
    project_id: str | None = Field(default=None, max_length=64)
    material_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_task_input(self) -> "CreatorTaskCreateRequest":
        if (
            self.kind == CreatorTaskKind.IMPROVE_DRAFT
            and self.constraints.draft is None
        ):
            raise ValueError("IMPROVE_DRAFT requires constraints.draft")
        if (
            self.kind != CreatorTaskKind.IMPROVE_DRAFT
            and self.constraints.draft is not None
        ):
            raise ValueError("constraints.draft is only valid for IMPROVE_DRAFT")
        return self


class CreatorTaskVersionRequest(CreatorApiModel):
    expected_task_version: int = Field(ge=1)


class CreatorDecisionResponseRequest(CreatorApiModel):
    action: CreatorDecisionAction
    selected_option_id: str | None = Field(default=None, max_length=128)
    feedback: str | None = Field(default=None, max_length=4_000)
    edited_payload: dict[str, Any] | None = None
    expected_task_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "CreatorDecisionResponseRequest":
        if self.action == CreatorDecisionAction.SELECT and not self.selected_option_id:
            raise ValueError("SELECT requires selected_option_id")
        if (
            self.action == CreatorDecisionAction.APPROVE
            and self.selected_option_id is not None
        ):
            raise ValueError("APPROVE cannot include selected_option_id")
        if self.action == CreatorDecisionAction.REQUEST_CHANGES and not (
            self.feedback and self.feedback.strip()
        ):
            raise ValueError("REQUEST_CHANGES requires feedback")
        if self.action == CreatorDecisionAction.EDIT:
            if not isinstance(self.edited_payload, dict) or not self.edited_payload:
                raise ValueError("EDIT requires edited_payload")
        elif self.edited_payload is not None:
            raise ValueError("edited_payload is only valid for EDIT")
        return self


class CreatorDraftCreateRequest(CreatorApiModel):
    title: str = Field(min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    source_artifact_id: str | None = Field(default=None, max_length=128)


class CreatorDraftUpdateRequest(CreatorApiModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    source_artifact_id: str | None = Field(default=None, max_length=128)


class CreatorProjectCreateRequest(CreatorApiModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2_000)


class CreatorMaterialCreateRequest(CreatorApiModel):
    project_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    kind: CreatorMaterialKind = CreatorMaterialKind.NOTE
    content_text: str = Field(min_length=1, max_length=500_000)
    source_url: str | None = Field(default=None, max_length=2_000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)


class CreatorSuggestionCreateRequest(CreatorApiModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    expected_version: int = Field(ge=1)
    kind: CreatorSuggestionKind = CreatorSuggestionKind.CUSTOM
    instruction: str = Field(min_length=1, max_length=2_000)
    original_text: str = Field(min_length=1, max_length=120_000)
    prefix_context: str = Field(default="", max_length=500)
    suffix_context: str = Field(default="", max_length=500)


class CreatorSuggestionRejectRequest(CreatorApiModel):
    reason: str = Field(default="", max_length=2_000)


class CreatorSuggestionApplyResponse(CreatorApiModel):
    suggestion: CreatorSuggestion
    draft: "CreatorDraftView"


class CreatorBranchCreateRequest(CreatorApiModel):
    source_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=256)


class CreatorBranchCreateResponse(CreatorApiModel):
    branch: CreatorBranch
    draft: "CreatorDraftView"


class CreatorChannelVariantCreateRequest(CreatorApiModel):
    expected_version: int = Field(ge=1)
    channel: CreatorDeliveryChannel
    instruction: str = Field(default="", max_length=2_000)


class CreatorRatingRequest(CreatorApiModel):
    task_id: str = Field(min_length=1, max_length=64)
    draft_id: str | None = Field(default=None, max_length=64)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=2_000)


class CreatorTaskAcceptedResponse(CreatorApiModel):
    task_id: str
    run_id: str
    status: CreatorTaskStatus
    version: int
    events_url: str
    trace_id: str
    replayed: bool = False


class CreatorTaskMutationResponse(CreatorApiModel):
    task_id: str
    run_id: str
    task_status: CreatorTaskStatus
    run_status: CreatorRunStatus | None = None
    task_version: int
    final_artifact_id: str | None = None
    pending_decision_id: str | None = None
    applied_decision_id: str | None = None
    replayed: bool = False


class CreatorTaskListItem(CreatorApiModel):
    task_id: str
    run_id: str
    kind: CreatorTaskKind
    goal: str
    status: CreatorTaskStatus
    version: int
    pending_decision_id: str | None = None
    final_artifact_id: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class CreatorTaskPage(CreatorApiModel):
    items: tuple[CreatorTaskListItem, ...]
    next_cursor: str | None = None


class CreatorRunView(CreatorApiModel):
    run_id: str
    attempt: int
    status: CreatorRunStatus
    execution_attempts: int
    error_code: str | None = None
    retryable: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None


class CreatorArtifactSummary(CreatorApiModel):
    artifact_id: str
    kind: ArtifactKind
    producer: str
    revision: int
    confidence: float
    content_sha256: str
    created_at: datetime


class CreatorArtifactDetail(CreatorArtifactSummary):
    content: dict[str, Any]
    parent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreatorDecisionOptionView(CreatorApiModel):
    option_id: str
    title: str
    angle: str = ""
    audience_value: str = ""
    risk_note: str = ""
    recommended: bool = False
    recommendation: str = ""
    why_now: str = ""
    reader_question: str = ""
    differentiation: str = ""
    evidence_ids: tuple[str, ...] = ()
    comment_ids: tuple[str, ...] = ()


class CreatorDecisionView(CreatorApiModel):
    decision_id: str
    task_id: str
    run_id: str
    kind: CreatorDecisionKind
    prompt: str
    source_artifact_id: str
    allowed_actions: tuple[CreatorDecisionAction, ...]
    options: tuple[CreatorDecisionOptionView, ...] = ()
    source: CreatorArtifactDetail | None = None
    status: CreatorDecisionStatus
    version: int
    action: CreatorDecisionAction | None = None
    selected_option_id: str | None = None
    feedback: str | None = None
    created_at: datetime
    submitted_at: datetime | None = None
    applied_at: datetime | None = None


class CreatorTaskSnapshot(CreatorApiModel):
    task_id: str
    run_id: str
    kind: CreatorTaskKind
    goal: str
    constraints: dict[str, Any]
    source_scope: dict[str, Any]
    status: CreatorTaskStatus
    version: int
    trace_id: str
    pending_decision: CreatorDecisionView | None = None
    final_artifact_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    run: CreatorRunView
    artifacts: tuple[CreatorArtifactSummary, ...] = ()
    created_at: datetime
    updated_at: datetime


class CreatorEventEnvelope(CreatorApiModel):
    event_id: str
    sequence: int
    type: str
    task_id: str
    run_id: str
    timestamp: datetime
    trace_id: str
    schema_version: str = "1"
    payload: dict[str, Any] = Field(default_factory=dict)


class CreatorDraftVersionView(CreatorApiModel):
    draft_id: str
    version: int
    title: str
    content_markdown: str
    content_sha256: str
    source_artifact_id: str | None = None
    editor_type: str
    actor_id: str
    created_at: datetime


class CreatorDraftView(CreatorApiModel):
    draft_id: str
    task_id: str
    title: str
    current_version: int
    status: str
    version: CreatorDraftVersionView
    replayed: bool = False
    created_at: datetime
    updated_at: datetime


class CreatorDraftSummary(CreatorApiModel):
    draft_id: str
    task_id: str
    title: str
    current_version: int
    status: str
    updated_at: datetime


class CreatorPublicationHandoffRequest(CreatorApiModel):
    source_artifact_id: str | None = Field(default=None, max_length=128)


class CreatorPublicationHandoffView(CreatorApiModel):
    handoff_id: str
    task_id: str
    draft_id: str
    content_origin: str
    source_artifact_id: str
    source_artifact_revision: int
    source_content_sha256: str
    external_draft_id: str
    title: str
    status: str
    replayed: bool = False
    created_at: datetime


class CreatorApiStatusResponse(CreatorApiModel):
    status: Literal["READY"]
    tenant_id: str
    creator_id: str
    actor_id: str
    display_name: str
    execution_mode: str
    model_provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    sse_replay: bool = True
    human_decisions: tuple[CreatorDecisionKind, ...] = (
        CreatorDecisionKind.TOPIC_SELECTION,
        CreatorDecisionKind.OUTLINE_APPROVAL,
        CreatorDecisionKind.DRAFT_REVIEW,
    )


class CreatorLocalSessionResponse(CreatorApiStatusResponse):
    auth_scheme: Literal["Basic"] = "Basic"
    token: str = Field(min_length=1, max_length=8_192)


class CreatorCatalogOption(CreatorApiModel):
    value: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1_000)
    enabled: bool = True
    requires_draft: bool = False


class CreatorTargetLengthCatalog(CreatorApiModel):
    minimum: int = Field(ge=1)
    maximum: int = Field(ge=1)
    default: int = Field(ge=1)
    step: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> "CreatorTargetLengthCatalog":
        if self.minimum > self.default or self.default > self.maximum:
            raise ValueError("Target length default must be within the range")
        return self


class CreatorWorkflowStageCatalog(CreatorApiModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    artifact_kinds: tuple[ArtifactKind, ...] = ()
    decision_kind: CreatorDecisionKind | None = None
    task_kinds: tuple[CreatorTaskKind, ...] = ()


class CreatorBackendCatalog(CreatorApiModel):
    execution_mode: str = Field(min_length=1, max_length=64)
    model_provider: str = Field(min_length=1, max_length=64)
    community_provider: str = Field(min_length=1, max_length=64)


class CreatorWorkspaceCatalogResponse(CreatorApiModel):
    catalog_version: str = Field(min_length=1, max_length=64)
    task_kinds: tuple[CreatorCatalogOption, ...]
    formats: tuple[CreatorCatalogOption, ...]
    interaction_modes: tuple[CreatorCatalogOption, ...]
    task_statuses: tuple[CreatorCatalogOption, ...]
    artifact_kinds: tuple[CreatorCatalogOption, ...]
    workflow_stages: tuple[CreatorWorkflowStageCatalog, ...]
    target_length: CreatorTargetLengthCatalog
    poll_interval_ms: int = Field(ge=500, le=60_000)
    backend: CreatorBackendCatalog


class CreatorApiErrorBody(CreatorApiModel):
    code: str
    message: str
    retryable: bool
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class CreatorApiErrorEnvelope(CreatorApiModel):
    error: CreatorApiErrorBody
