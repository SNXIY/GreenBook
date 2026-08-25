"""Canonical durable memory contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, model_validator


class MemoryType(StrEnum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    # SEMANTIC is retained as the storage value for the pre-existing
    # preference provider; PREFERENCE is the canonical semantic label.
    PREFERENCE = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"


class MemoryStatus(StrEnum):
    """Lifecycle state for a reusable memory record."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUPERSEDED = "superseded"


class MemoryRecord(BaseModel):
    """A bounded, auditable long-term memory record.

    ``decision_summary``, ``strategy_summary``, ``outcome``, ``lessons`` and
    structured evidence are allowed.  Hidden chain-of-thought is not a field
    in this contract.
    """

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    tenant_id: str = ""
    status: MemoryStatus = MemoryStatus.ACTIVE
    conversation_id: str | None = None
    task_id: str | None = None
    memory_type: MemoryType = Field(
        default=MemoryType.EPISODIC,
        validation_alias=AliasChoices("memory_type", "type"),
    )
    content: str = ""
    structured_metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("structured_metadata", "metadata"),
    )
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_type: str = ""
    source_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    # PostgreSQL keeps this column nullable until a memory is retrieved for
    # the first time.  Keep that nullability at the contract boundary instead
    # of forcing the repository to invent an access timestamp.
    last_accessed_at: str | None = None
    access_count: int = Field(default=0, ge=0)
    expires_at: str | None = None
    # Kept readable for old records; the canonical retriever never invents
    # embeddings and does not use this value for lexical retrieval.
    embedding: list[float] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "memory_type" not in normalized and "type" in normalized:
            normalized["memory_type"] = normalized["type"]
        if "structured_metadata" not in normalized and "metadata" in normalized:
            normalized["structured_metadata"] = normalized["metadata"]
        # ``source_conversation_id`` is the explicit Preference Memory
        # contract name. ``conversation_id`` remains the compatibility field
        # used by pre-existing records; both refer to one provenance value.
        if (
            not normalized.get("conversation_id")
            and normalized.get("source_conversation_id")
        ):
            normalized["conversation_id"] = normalized["source_conversation_id"]
        if not normalized.get("status"):
            normalized["status"] = MemoryStatus.ACTIVE
        else:
            normalized["status"] = str(normalized["status"]).lower()
        return normalized

    @property
    def type(self) -> MemoryType:
        """Compatibility spelling for the old process-local API."""

        return self.memory_type

    @property
    def metadata(self) -> dict[str, Any]:
        """Compatibility view; storage remains ``structured_metadata``."""

        return self.structured_metadata

    @property
    def source_conversation_id(self) -> str | None:
        """Explicit provenance spelling for the Preference Memory contract."""

        return self.conversation_id


class MemoryQuery(BaseModel):
    user_id: str = ""
    tenant_id: str = ""
    conversation_id: str | None = None
    task_id: str | None = None
    type: MemoryType | None = None
    keywords: list[str] = Field(default_factory=list)
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    status: MemoryStatus | None = None
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=10, ge=1, le=1000)
    sort_by: str = "importance"


__all__ = ["MemoryQuery", "MemoryRecord", "MemoryStatus", "MemoryType"]
