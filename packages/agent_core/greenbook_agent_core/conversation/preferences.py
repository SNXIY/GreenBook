"""Small user-preference port backed by the existing semantic memory store."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from greenbook_agent_core.command.models import Command
from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
from greenbook_agent_core.memory.manager import MemoryManager
from greenbook_agent_core.memory.models import MemoryQuery, MemoryStatus, MemoryType


class UserPreference(BaseModel):
    key: str
    value: str
    confidence: float = 0.0
    evidence_count: int = 1
    source: str = "SEMANTIC_MEMORY"
    metadata: dict[str, str] = Field(default_factory=dict)


class UserPreferenceProvider(Protocol):
    async def list_preferences(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
    ) -> list[UserPreference]: ...

    async def set_preference(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> UserPreference: ...

    async def observe(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        command: Command,
        timezone: str,
    ) -> None: ...


class MemoryUserPreferenceProvider:
    """Expose stable preferences from the existing semantic MemoryManager."""

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        *,
        minimum_observations: int = 2,
        temporal_resolver: TemporalResolver | None = None,
    ) -> None:
        self._memory = memory_manager or MemoryManager()
        self._minimum = max(1, minimum_observations)
        self._temporal = temporal_resolver or TemporalResolver()

    async def list_preferences(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
    ) -> list[UserPreference]:
        records = self._memory.recall(MemoryQuery(
            user_id=user_id,
            tenant_id=tenant_id,
            type=MemoryType.SEMANTIC,
            status=MemoryStatus.ACTIVE,
            limit=100,
        ))
        evidence = Counter(
            (
                str(record.metadata.get("preference_type") or ""),
                str(record.metadata.get("value") or ""),
            )
            for record in records
            if record.metadata.get("preference_type") and record.metadata.get("value")
        )
        preferences: list[UserPreference] = []
        for (key, value), count in evidence.items():
            explicit = any(
                record.metadata.get("preference_type") == key
                and record.metadata.get("value") == value
                and record.metadata.get("source") == "EXPLICIT"
                for record in records
            )
            if count < self._minimum and not explicit:
                continue
            preferences.append(UserPreference(
                key=key,
                value=value,
                confidence=min(0.55 + 0.15 * count, 0.95),
                evidence_count=count,
                source="EXPLICIT" if explicit else "OBSERVED",
            ))
        return sorted(
            preferences,
            key=lambda item: (item.key, -item.confidence, item.value),
        )

    async def set_preference(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> UserPreference:
        record = self._memory.remember_preference(
            user_id,
            key,
            value,
            confidence,
            tenant_id=tenant_id,
        )
        record.metadata["source"] = "EXPLICIT"
        return UserPreference(
            key=key,
            value=value,
            confidence=confidence,
            source="EXPLICIT",
        )

    async def observe(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        command: Command,
        timezone: str,
    ) -> None:
        operation = str(getattr(command, "operation", getattr(command, "type", "")))
        if operation not in {"CREATE", "MODIFY", "CREATE_CONTENT", "UPDATE_RUN_AT"}:
            return
        parameters = dict(getattr(command, "parameters", {}) or {})
        target = getattr(command, "target", None)
        schedule_semantics = (
            str(getattr(getattr(target, "kind", None), "value", "")) == "SCHEDULE"
            or "run_at" in parameters
            or "run_at_expression" in parameters
        )
        if not schedule_semantics:
            return
        expression = parameters.get("run_at_expression") or parameters.get("run_at")
        if not expression:
            return
        resolved = parameters.get("run_at") or self._temporal.resolve(
            expression,
            timezone=timezone,
        )
        if not resolved:
            return
        instant = datetime.fromisoformat(str(resolved).replace("Z", "+00:00"))
        try:
            local_timezone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            if timezone != "Asia/Shanghai":
                return
            # Match the deterministic scheduling parser on Windows hosts that
            # do not bundle the optional IANA tzdata package.
            local_timezone = dt_timezone(timedelta(hours=8), name=timezone)
        local = instant.astimezone(local_timezone)
        self._memory.remember_preference(
            user_id,
            "preferred_publish_time",
            local.strftime("%H:%M"),
            confidence=0.65,
            tenant_id=tenant_id,
        )


__all__ = [
    "MemoryUserPreferenceProvider",
    "UserPreference",
    "UserPreferenceProvider",
]
