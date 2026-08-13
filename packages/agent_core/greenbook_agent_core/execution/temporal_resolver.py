"""Resolve user supplied natural-language scheduling times.

The Runtime must carry a canonical timestamp in the plan.  Keeping this
adapter in ``execution`` makes the temporal boundary explicit while reusing
the existing, deterministic parser used by the HTTP compatibility path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ..time_parser import parse_natural_schedule_time


class TemporalResolver:
    """Convert natural-language time constraints into canonical ``run_at``."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now

    def resolve(
        self,
        text: str,
        *,
        constraints: Iterable[Any] = (),
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
    ) -> str | None:
        """Return a UTC ISO timestamp or ``None`` when no time is present.

        Constraint values are tried first because an Goal compilation may have
        already isolated the temporal phrase.  The complete user goal is then
        tried as a fallback for the deterministic L1 path.
        """

        candidates: list[str] = []
        for item in constraints:
            value = _constraint_value(item)
            if value and value not in candidates:
                candidates.append(value)
        if text and text not in candidates:
            candidates.append(text)

        effective_now = now or self._now
        for candidate in candidates:
            resolved = parse_natural_schedule_time(
                candidate,
                timezone,
                now=effective_now,
            )
            if resolved:
                return resolved
        return None


def _constraint_value(item: Any) -> str:
    if isinstance(item, Mapping):
        item_type = str(item.get("type", "")).upper()
        if item_type not in {"", "TIME", "TEMPORAL", "SCHEDULE_TIME"}:
            return ""
        return str(
            item.get("value") or item.get("time") or item.get("run_at") or ""
        ).strip()
    item_type = str(getattr(item, "type", "")).upper()
    if item_type not in {"TIME", "TEMPORAL", "SCHEDULE_TIME"}:
        return ""
    return str(
        getattr(item, "value", "")
        or getattr(item, "time", "")
        or getattr(item, "run_at", "")
        or ""
    ).strip()


__all__ = ["TemporalResolver"]
