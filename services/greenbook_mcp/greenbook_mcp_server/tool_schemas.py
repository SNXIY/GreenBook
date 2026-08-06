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
    expected_version: str | None = Field(
        default=None,
        description="Optional expected draft updatedAt for optimistic locking",
    )


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
