"""Versioned metadata schemas for Artifact producer/consumer contracts."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from .models import Artifact


class ArtifactSchemaValidationError(ValueError):
    pass


class ArtifactSchema(BaseModel):
    name: str
    artifact_type: str
    version: str = "1"
    required: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)

    def validate_artifact(self, artifact: Artifact) -> None:
        if artifact.artifact_type != self.artifact_type:
            raise ArtifactSchemaValidationError(
                f"ARTIFACT_TYPE_MISMATCH:{artifact.artifact_type}!={self.artifact_type}"
            )
        if artifact.metadata_schema and artifact.metadata_schema not in {
            self.name, f"{self.name}.v{self.version}",
        }:
            raise ArtifactSchemaValidationError(
                f"ARTIFACT_SCHEMA_VERSION_MISMATCH:{artifact.metadata_schema}!={self.name}"
            )
        missing = [field for field in self.required if field not in artifact.metadata]
        if missing:
            raise ArtifactSchemaValidationError(
                f"ARTIFACT_REQUIRED_FIELDS_MISSING:{','.join(missing)}"
            )


class ArtifactSchemaRegistry:
    """Registry for the small, explicit Artifact metadata schemas."""

    def __init__(self, schemas: Sequence[ArtifactSchema] | None = None) -> None:
        self._schemas = {schema.name: schema for schema in (schemas or default_schemas())}

    def register(self, schema: ArtifactSchema) -> ArtifactSchema:
        if schema.name in self._schemas:
            raise ArtifactSchemaValidationError(f"SCHEMA_ALREADY_REGISTERED:{schema.name}")
        self._schemas[schema.name] = schema.model_copy(deep=True)
        return schema.model_copy(deep=True)

    def get(self, name: str) -> ArtifactSchema | None:
        schema = self._schemas.get(name)
        return schema.model_copy(deep=True) if schema else None

    def validate(self, artifact: Artifact, schema_name: str | None = None) -> None:
        name = schema_name or artifact.metadata_schema
        if not name:
            raise ArtifactSchemaValidationError("ARTIFACT_SCHEMA_REQUIRED")
        schema = self.get(name)
        if schema is None:
            raise ArtifactSchemaValidationError(f"UNKNOWN_ARTIFACT_SCHEMA:{name}")
        schema.validate_artifact(artifact)

    def compatible(self, producer_schema: str, consumer_schema: str) -> bool:
        producer = self.get(producer_schema)
        consumer = self.get(consumer_schema)
        if producer is None or consumer is None:
            return False
        if producer.artifact_type != consumer.artifact_type:
            return False
        return set(consumer.required).issubset(set(producer.required))


def default_schemas() -> list[ArtifactSchema]:
    return [
        ArtifactSchema(
            name="POST_COLLECTION_SCHEMA", artifact_type="POST_COLLECTION",
            required=["posts"], properties={"posts": "array"},
        ),
        ArtifactSchema(
            name="POST_ANALYSIS_SCHEMA", artifact_type="POST_ANALYSIS",
            required=["posts", "summary", "statistics"],
            properties={"posts": "array", "summary": "string", "statistics": "object"},
        ),
        ArtifactSchema(
            name="CONTENT_DRAFT_SCHEMA", artifact_type="CONTENT_DRAFT",
            required=["title", "content", "summary"],
            properties={"title": "string", "content": "string", "summary": "string"},
        ),
        ArtifactSchema(
            name="PUBLISHED_POST_SCHEMA", artifact_type="PUBLISHED_POST",
            required=["post_id", "title"],
            properties={"post_id": "string", "title": "string"},
        ),
    ]


__all__ = [
    "ArtifactSchema",
    "ArtifactSchemaRegistry",
    "ArtifactSchemaValidationError",
    "default_schemas",
]
