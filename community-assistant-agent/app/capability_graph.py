from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str
    description: str
    implies: frozenset[str] = frozenset()
    aliases: frozenset[str] = frozenset()


class CapabilityGraph:
    """Versioned capability ontology shared by routing and plan compilation."""

    def __init__(
        self,
        *,
        version: str,
        capabilities: Iterable[CapabilityDescriptor],
    ) -> None:
        materialized = list(capabilities)
        self.version = version
        self._items = {item.name: item for item in materialized}
        if (
            not version
            or len(self._items) != len(materialized)
            or any(not item.name or not item.description for item in materialized)
        ):
            raise ValueError("Capability Graph version and names must be unique")
        unknown = {
            implied
            for item in materialized
            for implied in item.implies
            if implied not in self._items
        }
        if unknown:
            raise ValueError(f"Capability Graph has unknown implications: {unknown}")
        aliases = {
            alias: item.name
            for item in materialized
            for alias in item.aliases
        }
        if len(aliases) != sum(len(item.aliases) for item in materialized):
            raise ValueError("Capability Graph aliases must be unique")
        collisions = set(aliases) & set(self._items)
        if collisions:
            raise ValueError(
                f"Capability aliases collide with canonical names: {collisions}"
            )
        self._aliases = aliases
        self._validate_acyclic()

    def _validate_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Capability Graph contains a cycle at {name}")
            visiting.add(name)
            for implied in self._items[name].implies:
                visit(implied)
            visiting.remove(name)
            visited.add(name)

        for name in self._items:
            visit(name)

    @classmethod
    def from_manifest(cls, path: str | Path) -> "CapabilityGraph":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        items = payload.get("capabilities") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Capability Graph manifest requires capabilities")
        return cls(
            version=str(payload.get("version") or "").strip(),
            capabilities=[
                CapabilityDescriptor(
                    name=str(item.get("name") or "").strip(),
                    description=str(item.get("description") or "").strip(),
                    implies=frozenset(str(value) for value in item.get("implies", [])),
                    aliases=frozenset(str(value) for value in item.get("aliases", [])),
                )
                for item in items
                if isinstance(item, dict)
            ],
        )

    def expand(self, capabilities: Iterable[str]) -> set[str]:
        expanded = {self.canonicalize(value) for value in capabilities}
        frontier = list(expanded)
        while frontier:
            capability = frontier.pop()
            item = self._items.get(capability)
            if item is None:
                continue
            for implied in item.implies:
                if implied not in expanded:
                    expanded.add(implied)
                    frontier.append(implied)
        return expanded

    def covers(self, owned: Iterable[str], required: str | None) -> bool:
        return required is None or self.canonicalize(required) in self.expand(owned)

    def knows(self, capability: str) -> bool:
        return self.canonicalize(capability) in self._items

    def canonicalize(self, capability: str) -> str:
        """Map common model vocabulary to the stable runtime capability name."""
        normalized = capability.strip().lower()
        return self._aliases.get(normalized, normalized)

    def normalize(self, capabilities: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(self.canonicalize(value) for value in capabilities))

    def catalog_prompt(self) -> str:
        return "\n".join(
            f"- {item.name}: {item.description}; implies={sorted(item.implies)}; "
            f"aliases={sorted(item.aliases)}"
            for item in sorted(self._items.values(), key=lambda value: value.name)
        )

    def signature(self) -> str:
        payload = {
            "version": self.version,
            "capabilities": [
                {
                    "name": item.name,
                    "description": item.description,
                    "implies": sorted(item.implies),
                    "aliases": sorted(item.aliases),
                }
                for item in sorted(self._items.values(), key=lambda value: value.name)
            ],
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


capability_graph = CapabilityGraph.from_manifest(
    Path(__file__).with_name("capability_graph.json")
)
