"""Agent metadata registry and capability contract validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SideEffectLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    NONE = "NONE"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class AgentMetadata(BaseModel):
    name: str
    capabilities: list[str] = []
    input_artifacts: list[str] = []
    output_artifacts: list[str] = []
    side_effect_level: SideEffectLevel = SideEffectLevel.NONE
    version: str = "1"


class AgentResolutionError(ValueError):
    pass


class AgentRegistry:
    """Resolve semantic task/capability types to one declared Agent."""

    def __init__(self, agents: list[AgentMetadata] | None = None) -> None:
        self._metadata: dict[str, AgentMetadata] = {}
        self._implementations: dict[str, Any] = {}
        for metadata in agents or default_agent_metadata():
            self.register_agent(metadata)

    def register_agent(self, metadata: AgentMetadata, agent: Any = None) -> AgentMetadata:
        key = metadata.name.strip()
        if not key:
            raise AgentResolutionError("AGENT_NAME_REQUIRED")
        if key in self._metadata:
            raise AgentResolutionError("AGENT_ALREADY_REGISTERED")
        self._metadata[key] = metadata.model_copy(deep=True)
        if agent is not None:
            self._implementations[key] = agent
        return self._metadata[key].model_copy(deep=True)

    def resolve_agent(
        self,
        task_type: str,
        *,
        input_artifacts: list[str] | None = None,
        output_artifact: str = "",
    ) -> AgentMetadata:
        requested = _normalize(task_type)
        candidates = [
            metadata for metadata in self._metadata.values()
            if requested in {_normalize(capability) for capability in metadata.capabilities}
            or requested == _agent_type_for_name(metadata.name)
        ]
        if not candidates:
            raise AgentResolutionError(f"NO_AGENT_FOR_TASK_TYPE:{task_type}")
        inputs = set(input_artifacts or [])
        for metadata in candidates:
            if not _artifact_inputs_compatible(inputs, metadata.input_artifacts):
                continue
            if output_artifact and not _artifact_type_compatible(
                output_artifact, metadata.output_artifacts,
            ):
                continue
            return metadata.model_copy(deep=True)
        raise AgentResolutionError(f"AGENT_ARTIFACT_CONTRACT_MISMATCH:{task_type}")

    def get_agent(self, name: str) -> Any | None:
        return self._implementations.get(name)

    def list_agents(self) -> list[AgentMetadata]:
        return [metadata.model_copy(deep=True) for metadata in self._metadata.values()]

    def validate(self, metadata: AgentMetadata, *, input_artifacts: list[str], output_artifact: str) -> None:
        if not _artifact_inputs_compatible(set(input_artifacts), metadata.input_artifacts):
            raise AgentResolutionError(f"AGENT_INPUT_ARTIFACT_MISMATCH:{metadata.name}")
        if output_artifact and not _artifact_type_compatible(output_artifact, metadata.output_artifacts):
            raise AgentResolutionError(f"AGENT_OUTPUT_ARTIFACT_MISMATCH:{metadata.name}")


def default_agent_metadata() -> list[AgentMetadata]:
    return [
        AgentMetadata(
            name="SearchAgent",
            capabilities=["QUERY", "SEARCH", "ANALYZE_COMMUNITY", "SEARCH_COMMUNITY"],
            output_artifacts=["POST_COLLECTION"],
            side_effect_level=SideEffectLevel.READ_ONLY,
        ),
        AgentMetadata(
            name="AnalyticsAgent",
            capabilities=["ANALYZE", "ANALYZE_CONTENT_PATTERNS"],
            input_artifacts=["POST_COLLECTION", "SEARCH_RESULT"],
            output_artifacts=["POST_ANALYSIS", "ANALYSIS_REPORT"],
            side_effect_level=SideEffectLevel.READ_ONLY,
        ),
        AgentMetadata(
            name="CreatorAgent",
            capabilities=["CREATE", "CREATE_CONTENT", "GENERATE_CONTENT", "IMPROVE_CONTENT"],
            input_artifacts=["POST_ANALYSIS", "ANALYSIS_REPORT", "POST_COLLECTION", "SEARCH_RESULT"],
            output_artifacts=["CONTENT_DRAFT", "DRAFT"],
            side_effect_level=SideEffectLevel.NONE,
        ),
        AgentMetadata(
            name="PublishAgent",
            capabilities=["PUBLISH", "PUBLISH_CONTENT", "SCHEDULE_PUBLISH", "CANCEL_SCHEDULE"],
            input_artifacts=["CONTENT_DRAFT", "DRAFT"],
            output_artifacts=["PUBLISHED_POST", "SCHEDULE"],
            side_effect_level=SideEffectLevel.WRITE,
        ),
        AgentMetadata(
            name="QualityAgent",
            capabilities=["VALIDATE", "VALIDATE_QUALITY"],
            input_artifacts=["CONTENT_DRAFT", "DRAFT"],
            output_artifacts=["VALIDATION_REPORT"],
            side_effect_level=SideEffectLevel.NONE,
        ),
    ]


def _normalize(value: str) -> str:
    return str(value).strip().upper().replace("-", "_")


def _agent_type_for_name(name: str) -> str:
    return _normalize(name.removesuffix("Agent"))


def _artifact_inputs_compatible(actual: set[str], expected: list[str]) -> bool:
    if not expected:
        return not actual
    if not actual:
        return True
    return all(any(_artifact_type_compatible(item, [candidate]) for candidate in expected) for item in actual)


def _artifact_type_compatible(actual: str, expected: list[str]) -> bool:
    aliases = {
        "POST_COLLECTION": {"POST_COLLECTION", "SEARCH_RESULT"},
        "POST_ANALYSIS": {"POST_ANALYSIS", "ANALYSIS_REPORT"},
        "CONTENT_DRAFT": {"CONTENT_DRAFT", "DRAFT"},
        "PUBLISHED_POST": {"PUBLISHED_POST", "SCHEDULE", "PUBLICATION"},
    }
    actual_set = aliases.get(actual, {actual})
    return any(bool(actual_set & aliases.get(item, {item})) for item in expected)


__all__ = [
    "AgentMetadata",
    "AgentRegistry",
    "AgentResolutionError",
    "SideEffectLevel",
    "default_agent_metadata",
]
