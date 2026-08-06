"""Single-source argument models for write tools exposed by the MCP adapter."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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
        description="New UTC ISO-8601 publication time",
    )


def openai_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Return an OpenAI function parameter schema from the Pydantic model."""

    schema = model.model_json_schema(by_alias=True)
    schema.pop("title", None)
    return schema
