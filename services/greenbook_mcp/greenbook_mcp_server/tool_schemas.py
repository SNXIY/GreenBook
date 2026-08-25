"""Single-source argument models for write tools exposed by the MCP adapter."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class CreateDraftArguments(BaseModel):
    """Arguments for creating a draft.

    ``instruction`` is the canonical semantic input consumed by the
    assistant-first direct generator (host LLM). Keeping this model next to
    the handler contract prevents drift from recurring.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=256, description="Draft title")
    instruction: str = Field(
        min_length=1,
        max_length=12000,
        description="Content brief and generation instructions",
    )
    references: list[dict[str, Any]] | None = Field(
        default=None,
        description="Trusted reference posts from the current conversation",
    )
    summary: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional short summary for the draft",
    )




class SearchPublicPostsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, description="Search keywords or topic")
    sort: str = Field(default="latest", description="Sort order")
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class GetPostArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_id: str = Field(min_length=1)


class ListOwnPostsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class GetDraftArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("draft_id", "draftId"),
    )


class ListDraftsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UpdateDraftArguments(BaseModel):
    """Partial draft mutation; omitted fields are preserved by Java."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("draft_id", "draftId"),
    )
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("content", "body", "body_markdown"),
        description="Replacement body. Omit to preserve the existing body.",
    )

    @model_validator(mode="after")
    def require_a_mutation(self) -> "UpdateDraftArguments":
        if self.title is None and self.content is None:
            raise ValueError("at least one of title or content is required")
        return self


class DeleteDraftArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("draft_id", "draftId"),
    )


class DeletePostArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_id: str = Field(min_length=1, validation_alias=AliasChoices("post_id", "postId"))


class ScheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("draft_id", "draftId"),
    )
    run_at: str = Field(min_length=1, description="ISO-8601 publication time")
    timezone: str = Field(default="Asia/Shanghai", min_length=1)
    requires_approval: bool = Field(
        default=False,
        description="Require explicit user confirmation before creating the schedule",
    )


class GetScheduleStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schedule_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("schedule_id", "scheduleId"),
    )


class CancelScheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schedule_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("schedule_id", "scheduleId"),
    )


class PublishNowArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("draft_id", "draftId"),
    )


class ListCommentsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_id: str = Field(min_length=1)
    cursor: str | None = Field(default=None, min_length=1)
    size: int = Field(default=20, ge=1, le=100)


class SendReplyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_id: str = Field(min_length=1)
    parent_comment_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=12000)


class GetPostPerformanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    post_id: str = Field(min_length=1)


class GetAccountSummaryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UpdateScheduleArguments(BaseModel):
    """Arguments for updating an existing scheduled publication."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schedule_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("schedule_id", "scheduleId"),
        description="The exact schedule ID to update",
    )
    run_at: str = Field(
        min_length=1,
        validation_alias=AliasChoices("run_at", "publish_at"),
        description="New ISO-8601 publication time or a deterministic relative expression",
    )
    timezone: str = Field(default="Asia/Shanghai", min_length=1)
    temporal_base: str = Field(
        default="CURRENT_TIME",
        description=(
            "CURRENT_TIME for 'ten minutes from now', "
            "EXISTING_SCHEDULE_TIME for 'ten minutes later than the original plan', "
            "or EXPLICIT_DATETIME"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_legacy_time_aliases(cls, value: Any) -> Any:
        """Accept the historical alias only when it is unambiguous.

        ``run_at`` is the only canonical field exposed to the model.  Older
        planners emitted ``publish_at``; accepting that one known alias at
        the validation boundary lets those calls be normalized without
        weakening ``extra=\"forbid\"``.  Supplying both names with different
        values is a malformed request and must not be guessed at.
        """

        if isinstance(value, dict):
            run_at = value.get("run_at")
            publish_at = value.get("publish_at")
            if (
                run_at is not None
                and publish_at is not None
                and str(run_at).strip() != str(publish_at).strip()
            ):
                raise ValueError("run_at and publish_at conflict")
        return value


def openai_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Return an OpenAI function parameter schema from the Pydantic model."""

    schema = model.model_json_schema(by_alias=True)
    schema.pop("title", None)
    return schema
