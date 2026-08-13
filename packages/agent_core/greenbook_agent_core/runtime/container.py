"""The Agent Runtime composition root.

The container owns shared Runtime dependencies and contains no business
orchestration logic. API and Worker processes each build one container, while
using the same persistence selection rules and durable ArtifactStore type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from greenbook_security.policy import SecurityPolicy

from ..artifact.registry import ArtifactRegistry
from ..artifact.schema import ArtifactSchemaRegistry
from ..artifact.store import ArtifactStorePort
from ..capability.registry import CapabilityRegistry
from ..execution.event_store import ExecutionEventStore
from ..execution.persistence_provider import RuntimePersistence, RuntimePersistenceFactory
from ..execution.repository import ExecutionRepository
from ..execution.state_manager import ExecutionStateManager


class RuntimeToolRegistry:
    """Core-package fallback for tests that do not load the MCP service."""

    def list_tools(self) -> list[Any]:
        return []

    def get_tool(self, name: str) -> Any:
        raise KeyError(name)


@dataclass(slots=True)
class RuntimeContainer:
    """All registries and persistence dependencies for one Runtime process."""

    persistence: RuntimePersistence
    capability_registry: CapabilityRegistry
    tool_registry: Any
    artifact_schema_registry: ArtifactSchemaRegistry
    artifact_store: ArtifactStorePort
    artifact_registry: ArtifactRegistry
    security_policy: SecurityPolicy
    execution_state_manager: ExecutionStateManager

    @property
    def execution_repository(self) -> ExecutionRepository | Any:
        return self.persistence.execution_repository

    @property
    def event_store(self) -> ExecutionEventStore | Any:
        return self.persistence.execution_event_store

    @classmethod
    def from_env(
        cls,
        *,
        tool_registry: Any | None = None,
        security_policy: SecurityPolicy | None = None,
        **persistence_options: Any,
    ) -> RuntimeContainer:
        """Build one container from the canonical Runtime persistence config."""

        persistence = RuntimePersistenceFactory.from_env(**persistence_options)
        return cls.from_persistence(
            persistence,
            tool_registry=tool_registry,
            security_policy=security_policy,
        )

    @classmethod
    def from_persistence(
        cls,
        persistence: RuntimePersistence,
        *,
        capability_registry: CapabilityRegistry | None = None,
        tool_registry: Any | None = None,
        artifact_schema_registry: ArtifactSchemaRegistry | None = None,
        security_policy: SecurityPolicy | None = None,
    ) -> RuntimeContainer:
        """Compose all Runtime dependencies around one persistence profile."""

        schemas = artifact_schema_registry or ArtifactSchemaRegistry()
        artifact_store = persistence.artifact_store
        artifact_registry = ArtifactRegistry(
            artifact_store,
            schema_registry=schemas,
        )
        return cls(
            persistence=persistence,
            capability_registry=capability_registry or CapabilityRegistry(),
            tool_registry=tool_registry or RuntimeToolRegistry(),
            artifact_schema_registry=schemas,
            artifact_store=artifact_store,
            artifact_registry=artifact_registry,
            security_policy=security_policy or SecurityPolicy(),
            execution_state_manager=ExecutionStateManager(
                repository=persistence.execution_repository,
                event_store=persistence.execution_event_store,
            ),
        )

    @classmethod
    def for_testing(cls) -> RuntimeContainer:
        """Create an explicit memory container for unit tests and local probes."""

        return cls.from_env(storage=RuntimePersistenceFactory.MEMORY)

    def close(self) -> None:
        """Close persistence resources owned by this container."""

        self.persistence.close()


__all__ = ["RuntimeContainer", "RuntimeToolRegistry"]
