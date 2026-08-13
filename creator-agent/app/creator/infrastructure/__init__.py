"""Infrastructure adapters with lazy public exports to avoid import cycles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.creator.infrastructure.database import CreatorDatabase


__all__ = ["CreatorDatabase"]


def __getattr__(name: str) -> Any:
    if name == "CreatorDatabase":
        from app.creator.infrastructure.database import CreatorDatabase

        return CreatorDatabase
    raise AttributeError(name)
