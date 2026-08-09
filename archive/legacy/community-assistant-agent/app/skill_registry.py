from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

from app.domain import CommunityIntent


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    version: str
    description: str
    domains: frozenset[str]
    capabilities: frozenset[str]
    tools: frozenset[str]
    risk: str
    requires_approval: bool
    instructions: str
    source: str

    def matches(self, intent: CommunityIntent) -> bool:
        return (
            intent.domain in self.domains
            or bool(self.capabilities & set(intent.required_capabilities))
        )


class SkillRegistry:
    """Dynamically loads versioned community SKILL.md capability contracts."""

    def __init__(self, skills: Iterable[SkillDescriptor]) -> None:
        materialized = list(skills)
        self._skills = {skill.name: skill for skill in materialized}
        if len(materialized) != len(self._skills):
            raise ValueError("Skill Registry contains duplicate names")

    @classmethod
    def from_directory(cls, directory: str | Path) -> "SkillRegistry":
        root = Path(directory)
        skills = [
            _read_skill(path)
            for path in sorted(root.glob("*/SKILL.md"))
        ]
        if not skills:
            raise ValueError(f"No SKILL.md files found under {root}")
        return cls(skills)

    def for_intent(self, intent: CommunityIntent) -> tuple[SkillDescriptor, ...]:
        return tuple(
            skill
            for skill in sorted(self._skills.values(), key=lambda item: item.name)
            if skill.matches(intent)
        )

    def get(self, name: str) -> SkillDescriptor:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"Unknown skill: {name}") from exc

    def catalog_prompt(
        self, skills: Iterable[SkillDescriptor] | None = None
    ) -> str:
        selected = list(skills) if skills is not None else list(self._skills.values())
        return "\n\n".join(
            (
                f"Skill {item.name}@{item.version}: {item.description}\n"
                f"domains={sorted(item.domains)}; "
                f"capabilities={sorted(item.capabilities)}; "
                f"tools={sorted(item.tools)}; risk={item.risk}; "
                f"requires_approval={item.requires_approval}\n"
                f"{item.instructions}"
            )
            for item in sorted(selected, key=lambda value: value.name)
        )

    def public_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "description": item.description,
                "domains": sorted(item.domains),
                "capabilities": sorted(item.capabilities),
                "tools": sorted(item.tools),
                "risk": item.risk,
                "requires_approval": item.requires_approval,
                "source": item.source,
            }
            for item in sorted(self._skills.values(), key=lambda value: value.name)
        ]

    def signature(self) -> str:
        encoded = json.dumps(
            self.public_catalog(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _read_skill(path: Path) -> SkillDescriptor:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path} must start with JSON frontmatter")
    try:
        metadata_raw, instructions = raw[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError(f"{path} has invalid JSON frontmatter") from exc
    metadata = json.loads(metadata_raw)
    required = ("name", "version", "description", "domains", "capabilities", "tools")
    missing = [key for key in required if key not in metadata]
    if missing:
        raise ValueError(f"{path} is missing {missing}")
    return SkillDescriptor(
        name=str(metadata["name"]),
        version=str(metadata["version"]),
        description=str(metadata["description"]),
        domains=frozenset(str(value) for value in metadata["domains"]),
        capabilities=frozenset(str(value) for value in metadata["capabilities"]),
        tools=frozenset(str(value) for value in metadata["tools"]),
        risk=str(metadata.get("risk", "READ")),
        requires_approval=bool(metadata.get("requires_approval", False)),
        instructions=instructions.strip(),
        source=str(path),
    )


skill_registry = SkillRegistry.from_directory(
    Path(__file__).with_name("skills")
)
