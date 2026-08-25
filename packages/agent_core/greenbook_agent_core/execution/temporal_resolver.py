"""Resolve user supplied natural-language scheduling times.

The Runtime must carry a canonical timestamp in the plan.  Keeping this
adapter in ``execution`` makes the temporal boundary explicit while reusing
the existing, deterministic parser used by the HTTP compatibility path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..time_parser import parse_natural_schedule_time


@dataclass(frozen=True, slots=True)
class TemporalResolution:
    """Canonical temporal fact shared by semantic and execution boundaries."""

    intent: str = "NONE"  # NONE | NOW | FUTURE
    resolved: bool = False
    run_at: str | None = None
    source_text: str = ""
    timezone: str = "Asia/Shanghai"
    unresolved_reason: str = ""

    @property
    def temporal_kind(self) -> str:
        if self.intent == "NOW":
            return "NOW"
        if self.intent == "FUTURE":
            return "FUTURE" if self.resolved else "UNRESOLVED"
        return "NONE"


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
        immediate: bool = False,
    ) -> str | None:
        """Return a UTC ISO timestamp or ``None`` when no time is present.

        Constraint values are tried first because an Goal compilation may have
        already isolated the temporal phrase.  The complete user goal is then
        tried as a fallback for the deterministic L1 path.
        """

        return self.resolve_result(
            text,
            constraints=constraints,
            timezone=timezone,
            now=now,
            immediate=immediate,
        ).run_at

    def resolve_result(
        self,
        text: str,
        *,
        constraints: Iterable[Any] = (),
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
        immediate: bool = False,
    ) -> TemporalResolution:
        """Return a resolved or explicitly unresolved temporal fact.

        A non-empty temporal input is a future requirement unless it is an
        explicit immediate-time value.  This keeps ``None`` from being
        mistaken for ``NO TEMPORAL REQUIREMENT`` and lets the semantic layer
        fail closed when parsing cannot produce a canonical instant.
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
                return TemporalResolution(
                    intent="FUTURE",
                    resolved=True,
                    run_at=resolved,
                    source_text=candidate,
                    timezone=timezone,
                )
            if immediate and _is_current_day_expression(candidate):
                return TemporalResolution(
                    intent="NOW",
                    resolved=True,
                    source_text=candidate,
                    timezone=timezone,
                )
            if _is_now_expression(candidate):
                return TemporalResolution(
                    intent="NOW",
                    resolved=True,
                    source_text=candidate,
                    timezone=timezone,
                )
        if candidates:
            return TemporalResolution(
                intent="FUTURE",
                resolved=False,
                source_text=candidates[0],
                timezone=timezone,
                unresolved_reason="temporal_expression_unresolved",
            )
        return TemporalResolution(timezone=timezone)


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


def _is_now_expression(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    normalized = normalized.strip(".,!?;:，。！？；：")
    return normalized in {
        "now",
        "immediately",
        "right now",
        "现在",
        "立即",
        "马上",
        "立刻",
    }


def _is_current_day_expression(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    normalized = normalized.strip(".,!?;:，。！？；：")
    return normalized in {"今天", "今日", "today"}


__all__ = ["TemporalResolution", "TemporalResolver"]
