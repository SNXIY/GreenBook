from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from moderation.schemas import ModerationAction, ModerationContentType, RiskType

SCHEMA_VERSION: Literal["1.0"] = "1.0"
ReviewerId = Annotated[
    str,
    Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$",
    ),
]
EvalTag = Annotated[str, Field(min_length=1, max_length=64)]
SyntheticSensitiveValue = Annotated[str, Field(min_length=3, max_length=256)]


class EvalDatasetSplit(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    DEVELOPMENT = "DEVELOPMENT"
    CALIBRATION = "CALIBRATION"
    TEST = "TEST"
    CHALLENGE = "CHALLENGE"


class EvalAnnotationStatus(StrEnum):
    UNLABELED = "UNLABELED"
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    ADJUDICATED = "ADJUDICATED"


class EvalCaseSource(StrEnum):
    POLICY_TEMPLATE = "POLICY_TEMPLATE"
    LLM_GENERATED = "LLM_GENERATED"
    CURATED_SEED = "CURATED_SEED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    REVIEW_OVERRIDE = "REVIEW_OVERRIDE"


class EvalPrivacyMode(StrEnum):
    NO_SENSITIVE_DATA = "NO_SENSITIVE_DATA"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
    PRODUCTION_REDACTED = "PRODUCTION_REDACTED"


class EvalEvidenceField(StrEnum):
    CONTENT = "CONTENT"
    PARENT_COMMENT = "PARENT_COMMENT"
    CONVERSATION_CONTEXT = "CONVERSATION_CONTEXT"
    AUTHOR_RECENT_CONTENT = "AUTHOR_RECENT_CONTENT"
    REPORT_REASON = "REPORT_REASON"


class StrictEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalEvidenceSpan(StrictEvalModel):
    field: EvalEvidenceField = EvalEvidenceField.CONTENT
    source_index: int | None = Field(default=None, ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_location(self) -> "EvalEvidenceSpan":
        indexed_fields = {
            EvalEvidenceField.CONVERSATION_CONTEXT,
            EvalEvidenceField.AUTHOR_RECENT_CONTENT,
            EvalEvidenceField.REPORT_REASON,
        }
        if self.end <= self.start:
            raise ValueError("evidence end must be greater than start")
        if self.field in indexed_fields and self.source_index is None:
            raise ValueError(f"source_index is required for {self.field}")
        if self.field not in indexed_fields and self.source_index is not None:
            raise ValueError(f"source_index is not allowed for {self.field}")
        return self


class EvalContext(StrictEvalModel):
    parent_comment: str | None = Field(default=None, max_length=20_000)
    conversation_context: list[str] = Field(default_factory=list, max_length=20)
    author_recent_contents: list[str] = Field(default_factory=list, max_length=20)
    report_reasons: list[str] = Field(default_factory=list, max_length=20)
    complete: bool = True


class EvalInput(StrictEvalModel):
    content: str = Field(min_length=1, max_length=20_000)
    content_type: ModerationContentType = ModerationContentType.TEXT
    platform: str = Field(default="default", min_length=1, max_length=64)
    context: EvalContext = Field(default_factory=EvalContext)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=30)


class EvalLabel(StrictEvalModel):
    primary_risk_type: RiskType
    risk_labels: list[RiskType] = Field(min_length=1, max_length=4)
    expected_action: ModerationAction
    acceptable_actions: list[ModerationAction] = Field(min_length=1, max_length=4)
    requires_human_review: bool = False
    policy_codes: list[str] = Field(min_length=1, max_length=20)
    evidence_spans: list[EvalEvidenceSpan] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_label_consistency(self) -> "EvalLabel":
        if len(set(self.risk_labels)) != len(self.risk_labels):
            raise ValueError("risk_labels must be unique")
        if self.primary_risk_type not in self.risk_labels:
            raise ValueError("primary_risk_type must be included in risk_labels")
        if self.primary_risk_type == RiskType.NORMAL and self.risk_labels != [RiskType.NORMAL]:
            raise ValueError("NORMAL cannot be combined with violation risk labels")
        if self.primary_risk_type != RiskType.NORMAL and RiskType.NORMAL in self.risk_labels:
            raise ValueError("NORMAL cannot be combined with violation risk labels")

        if len(set(self.acceptable_actions)) != len(self.acceptable_actions):
            raise ValueError("acceptable_actions must be unique")
        if self.expected_action not in self.acceptable_actions:
            raise ValueError("expected_action must be included in acceptable_actions")
        expects_review = self.expected_action == ModerationAction.HUMAN_REVIEW
        if self.requires_human_review != expects_review:
            raise ValueError(
                "requires_human_review must be true exactly when expected_action is HUMAN_REVIEW"
            )
        if not self.requires_human_review and ModerationAction.HUMAN_REVIEW in self.acceptable_actions:
            raise ValueError("HUMAN_REVIEW cannot be acceptable when review is not required")
        if self.primary_risk_type == RiskType.NORMAL:
            if self.expected_action != ModerationAction.PASS:
                raise ValueError("NORMAL labels must use expected_action PASS")
            if self.acceptable_actions != [ModerationAction.PASS]:
                raise ValueError("NORMAL labels can only accept PASS")
        elif ModerationAction.PASS in self.acceptable_actions:
            raise ValueError("violation labels cannot accept PASS")
        if len(set(self.policy_codes)) != len(self.policy_codes):
            raise ValueError("policy_codes must be unique")

        span_keys = {
            (span.field, span.source_index, span.start, span.end) for span in self.evidence_spans
        }
        if len(span_keys) != len(self.evidence_spans):
            raise ValueError("evidence_spans must not contain duplicate locations")
        return self


class EvalAnnotation(StrictEvalModel):
    status: EvalAnnotationStatus = EvalAnnotationStatus.UNLABELED
    source: EvalCaseSource
    reviewer_ids: list[ReviewerId] = Field(default_factory=list, max_length=10)
    adjudicator_id: ReviewerId | None = None
    agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_review_provenance(self) -> "EvalAnnotation":
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer_ids must be unique")
        if self.status in {
            EvalAnnotationStatus.UNLABELED,
            EvalAnnotationStatus.PROPOSED,
        }:
            if self.reviewer_ids or self.adjudicator_id is not None or self.agreement is not None:
                raise ValueError(f"{self.status} annotations cannot claim human review metadata")
        elif self.status == EvalAnnotationStatus.REVIEWED:
            if not self.reviewer_ids:
                raise ValueError("REVIEWED annotations require at least one reviewer_id")
            if self.adjudicator_id is not None:
                raise ValueError("REVIEWED annotations cannot have an adjudicator_id")
            if len(self.reviewer_ids) == 1 and self.agreement is not None:
                raise ValueError("single-reviewer annotations cannot report agreement")
            if len(self.reviewer_ids) > 1 and self.agreement is None:
                raise ValueError("multi-reviewer annotations require agreement")
        elif self.status == EvalAnnotationStatus.ADJUDICATED:
            if len(self.reviewer_ids) < 2:
                raise ValueError("ADJUDICATED annotations require at least two reviewer_ids")
            if self.adjudicator_id is None:
                raise ValueError("ADJUDICATED annotations require an adjudicator_id")
            if self.adjudicator_id in self.reviewer_ids:
                raise ValueError("adjudicator_id must be independent of reviewer_ids")
            if self.agreement is None:
                raise ValueError("ADJUDICATED annotations require agreement")
        return self


class EvalPrivacyDeclaration(StrictEvalModel):
    mode: EvalPrivacyMode = EvalPrivacyMode.NO_SENSITIVE_DATA
    synthetic_sensitive_values: list[SyntheticSensitiveValue] = Field(
        default_factory=list,
        max_length=20,
    )
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_declaration(self) -> "EvalPrivacyDeclaration":
        if len(set(self.synthetic_sensitive_values)) != len(self.synthetic_sensitive_values):
            raise ValueError("synthetic_sensitive_values must be unique")
        if self.mode == EvalPrivacyMode.SYNTHETIC_ONLY:
            if not self.synthetic_sensitive_values:
                raise ValueError("SYNTHETIC_ONLY requires synthetic_sensitive_values")
        elif self.synthetic_sensitive_values:
            raise ValueError("synthetic_sensitive_values are only allowed in SYNTHETIC_ONLY mode")
        return self


class EvalPolicyReference(StrictEvalModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_-]+$")
    version: str = Field(default="1", min_length=1, max_length=64)
    fingerprint_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class EvalPolicySnapshot(StrictEvalModel):
    snapshot_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]+$",
    )
    policies: list[EvalPolicyReference] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_policy_references(self) -> "EvalPolicySnapshot":
        codes = [policy.code for policy in self.policies]
        if len(set(codes)) != len(codes):
            raise ValueError("policy snapshot codes must be unique")
        return self


class ModerationEvalCase(StrictEvalModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    case_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]+$",
    )
    scenario_group_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]+$",
    )
    revision: int = Field(default=1, ge=1)
    split: EvalDatasetSplit = EvalDatasetSplit.UNASSIGNED
    input: EvalInput
    label: EvalLabel | None = None
    annotation: EvalAnnotation
    privacy: EvalPrivacyDeclaration = Field(default_factory=EvalPrivacyDeclaration)
    policy_snapshot: EvalPolicySnapshot
    tags: list[EvalTag] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_case(self) -> "ModerationEvalCase":
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        if self.annotation.status == EvalAnnotationStatus.UNLABELED:
            if self.label is not None:
                raise ValueError("UNLABELED cases cannot contain a label")
            return self
        if self.label is None:
            raise ValueError(f"{self.annotation.status} cases require a label")

        snapshot_codes = {policy.code for policy in self.policy_snapshot.policies}
        missing_codes = set(self.label.policy_codes) - snapshot_codes
        if missing_codes:
            raise ValueError(
                "label policy_codes are absent from policy_snapshot: "
                + ", ".join(sorted(missing_codes))
            )

        for span in self.label.evidence_spans:
            source = self._evidence_source(span)
            if span.end > len(source) or source[span.start : span.end] != span.text:
                raise ValueError(
                    "evidence span does not match its source at "
                    f"{span.field}[{span.source_index}] {span.start}:{span.end}"
                )
        return self

    def _evidence_source(self, span: EvalEvidenceSpan) -> str:
        context = self.input.context
        if span.field == EvalEvidenceField.CONTENT:
            return self.input.content
        if span.field == EvalEvidenceField.PARENT_COMMENT:
            if context.parent_comment is None:
                raise ValueError("PARENT_COMMENT evidence requires parent_comment")
            return context.parent_comment

        sources = {
            EvalEvidenceField.CONVERSATION_CONTEXT: context.conversation_context,
            EvalEvidenceField.AUTHOR_RECENT_CONTENT: context.author_recent_contents,
            EvalEvidenceField.REPORT_REASON: context.report_reasons,
        }[span.field]
        assert span.source_index is not None
        if span.source_index >= len(sources):
            raise ValueError(f"source_index is out of range for {span.field}")
        return sources[span.source_index]
