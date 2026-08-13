"""Single-source argument models for write tools exposed by the MCP adapter."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ReviseDraftArguments(BaseModel):
    """Arguments for the existing Creator-backed draft revision workflow.

    The public tool accepts a revision instruction.  The complete ``content``
    is produced by Creator and is mapped to ``AgentDraftUpdateRequest`` inside
    the handler; it is intentionally not a model-supplied tool argument.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str = Field(min_length=1, description="The exact draft ID to revise")
    revision_instruction: str = Field(
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("revision_instruction", "instruction"),
        description="What to change in the existing draft",
    )
    title: str | None = Field(
        default=None,
        max_length=256,
        description="Optional requested title change",
    )
    revision_scope: str = Field(
        default="FULL_REVISION",
        pattern="^(TITLE_ONLY|CONTENT_ONLY|STYLE_ONLY|STRUCTURE_ONLY|FULL_REVISION)$",
        description="The narrowest intended revision scope",
    )
    expected_version: str | None = Field(
        default=None,
        description="Optional expected draft updatedAt for optimistic locking",
    )


class CreateDraftArguments(BaseModel):
    """Arguments for creating a draft.

    ``instruction`` is the canonical semantic input consumed by Creator.
    The old Runtime capability metadata exposed ``content`` here even though
    the handler has always accepted an instruction; keeping this model next
    to the handler contract prevents that drift from recurring.
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
    strategy_task_id: str | None = Field(
        default=None,
        min_length=1,
        description="Creator strategy task that supplies the content brief",
    )
    strategy_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        description="Creator strategy artifact that supplies the content brief",
    )
    summary: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional short summary for the draft",
    )


class BuildStrategyArguments(BaseModel):
    """Arguments for the existing Creator content-strategy task contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(
        min_length=1,
        max_length=12000,
        description="The editorial or content-growth strategy brief",
    )
    references: list[dict[str, Any]] | None = Field(
        default=None,
        description="Trusted reference posts and analysis artifacts",
    )
    constraints: dict[str, Any] | None = Field(
        default=None,
        description="Structured audience, format, and evidence constraints",
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

    draft_id: str | None = Field(default=None, min_length=1)


class ListDraftsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(default=None, min_length=1)
    run_at: str = Field(min_length=1, description="ISO-8601 publication time")
    timezone: str = Field(default="Asia/Shanghai", min_length=1)
    requires_approval: bool = Field(
        default=False,
        description="Require explicit user confirmation before creating the schedule",
    )


class GetScheduleStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schedule_id: str | None = Field(default=None, min_length=1)


class CancelScheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schedule_id: str | None = Field(default=None, min_length=1)


class PublishNowArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str | None = Field(default=None, min_length=1)


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

    schedule_id: str = Field(min_length=1, description="The exact schedule ID to update")
    run_at: str = Field(
        min_length=1,
        validation_alias=AliasChoices("run_at", "publish_at"),
        description="New UTC ISO-8601 publication time",
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
