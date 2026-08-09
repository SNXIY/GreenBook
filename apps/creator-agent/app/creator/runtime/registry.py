from __future__ import annotations

from collections.abc import Iterable

from app.creator.runtime.models import AgentCapability, AgentDescriptor
from app.creator.runtime.ports import CreatorSpecialistAgent


class AgentRegistryError(ValueError):
    pass


class CreatorAgentRegistry:
    def __init__(self, agents: Iterable[CreatorSpecialistAgent] = ()) -> None:
        self._agents_by_name: dict[str, CreatorSpecialistAgent] = {}
        self._agents_by_capability: dict[AgentCapability, CreatorSpecialistAgent] = {}
        for agent in agents:
            self.register(agent)

    def register(self, agent: CreatorSpecialistAgent) -> None:
        descriptor = agent.descriptor
        if descriptor.name in self._agents_by_name:
            raise AgentRegistryError(
                f"Agent name {descriptor.name!r} is already registered"
            )
        if not descriptor.capabilities:
            raise AgentRegistryError(
                f"Agent {descriptor.name!r} must declare at least one capability"
            )
        conflicts = descriptor.capabilities & self._agents_by_capability.keys()
        if conflicts:
            rendered = ", ".join(sorted(capability.value for capability in conflicts))
            raise AgentRegistryError(f"Capabilities already have owners: {rendered}")
        self._agents_by_name[descriptor.name] = agent
        for capability in descriptor.capabilities:
            self._agents_by_capability[capability] = agent

    def resolve(self, capability: AgentCapability) -> CreatorSpecialistAgent:
        try:
            return self._agents_by_capability[capability]
        except KeyError as exc:
            raise AgentRegistryError(
                f"No specialist is registered for {capability.value}"
            ) from exc

    def assert_available(self, capabilities: Iterable[AgentCapability]) -> None:
        missing = {
            capability
            for capability in capabilities
            if capability not in self._agents_by_capability
        }
        if missing:
            rendered = ", ".join(sorted(capability.value for capability in missing))
            raise AgentRegistryError(f"Missing specialist capabilities: {rendered}")

    @property
    def descriptors(self) -> tuple[AgentDescriptor, ...]:
        return tuple(
            agent.descriptor
            for agent in sorted(
                self._agents_by_name.values(),
                key=lambda item: item.descriptor.name,
            )
        )

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset(self._agents_by_capability)
