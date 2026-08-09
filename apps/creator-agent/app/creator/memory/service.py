from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.creator.domain.models import CreatorRun, CreatorTask
from app.creator.memory.errors import (
    CreatorMemoryConflictError,
    CreatorMemoryIntegrityError,
    CreatorMemoryUnavailableError,
)
from app.creator.memory.angles import (
    UsedContentAngle,
    append_used_angle,
    extract_used_angles,
    normalize_angle_key,
)
from app.creator.memory.models import (
    CreatorHistoricalPost,
    CreatorLongTermProfile,
    CreatorMemoryBundle,
    CreatorMemoryQuery,
    CreatorMemorySourceReport,
    CreatorSemanticMemoryHit,
    CreatorTaskMemory,
    MemoryAvailability,
    MemorySourceStatus,
    MemoryTier,
)
from app.creator.memory.ports import (
    CreatorLongTermProfileStore,
    CreatorSemanticMemoryStore,
    CreatorShortTermMemoryStore,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LoadResult:
    value: Any
    report: CreatorMemorySourceReport
    limitation: str | None = None


class CreatorMemoryService:
    """Aggregates authorized memory without making any tier a hard dependency."""

    def __init__(
        self,
        *,
        short_term: CreatorShortTermMemoryStore | None,
        long_term: CreatorLongTermProfileStore | None,
        semantic: CreatorSemanticMemoryStore | None,
        semantic_top_k: int = 6,
        max_excerpt_chars: int = 1_200,
    ) -> None:
        if semantic_top_k <= 0:
            raise ValueError("semantic_top_k must be greater than zero")
        if max_excerpt_chars < 200:
            raise ValueError("max_excerpt_chars must be at least 200")
        self._short = short_term
        self._long = long_term
        self._semantic = semantic
        self._semantic_top_k = semantic_top_k
        self._max_excerpt_chars = max_excerpt_chars

    async def load(self, query: CreatorMemoryQuery) -> CreatorMemoryBundle:
        profile_authorized = query.include_profile and bool(
            query.source_scope.get("include_creator_profile", True)
        )
        history_authorized = query.include_semantic and bool(
            query.source_scope.get("include_creator_history", False)
        )
        task_authorized = query.include_task_state

        short_result, long_result, semantic_result = await asyncio.gather(
            self._load_short(query, authorized=task_authorized),
            self._load_long(query, authorized=profile_authorized),
            self._load_semantic(query, authorized=history_authorized),
        )
        profile = (
            long_result.value
            if isinstance(long_result.value, CreatorLongTermProfile)
            else None
        )
        task_state = (
            short_result.value
            if isinstance(short_result.value, CreatorTaskMemory)
            else None
        )
        semantic_hits = tuple(
            hit
            for hit in (semantic_result.value or ())
            if isinstance(hit, CreatorSemanticMemoryHit)
        )
        profile_availability = _availability_for_source(
            long_result.report,
            has_data=profile is not None,
        )
        history_availability = _availability_for_source(
            semantic_result.report,
            has_data=bool(semantic_hits),
        )
        expected: list[MemoryAvailability] = []
        if profile_authorized:
            expected.append(profile_availability)
        if history_authorized:
            expected.append(history_availability)
        overall = _combined_availability(expected)
        limitations = tuple(
            limitation
            for limitation in (
                short_result.limitation,
                long_result.limitation,
                semantic_result.limitation,
            )
            if limitation
        )
        return CreatorMemoryBundle(
            task_state=task_state,
            profile=profile,
            semantic_hits=semantic_hits,
            source_reports=(
                short_result.report,
                long_result.report,
                semantic_result.report,
            ),
            profile_availability=profile_availability,
            history_availability=history_availability,
            overall_availability=overall,
            limitations=limitations,
        )

    async def remember_task(self, task: CreatorTask, run: CreatorRun) -> None:
        if self._short is None:
            return
        for attempt in range(2):
            current = await self._short.get(
                tenant_id=task.tenant_id,
                task_id=task.id,
            )
            if current is not None and current.creator_id != task.creator_id:
                raise CreatorMemoryIntegrityError(
                    "Creator task memory owner changed",
                    details={"task_id": task.id},
                )
            expected_version = current.version if current is not None else 0
            memory = CreatorTaskMemory(
                tenant_id=task.tenant_id,
                creator_id=task.creator_id,
                task_id=task.id,
                run_id=run.id,
                session_id=task.session_id,
                goal=task.goal.text,
                constraints=task.goal.constraints,
                source_scope=task.goal.source_scope,
                task_status=task.status,
                run_status=run.status,
                task_version=task.version,
                run_version=run.version,
                run_attempt=run.attempt,
                execution_attempts=run.execution_attempts,
                checkpoint_id=run.checkpoint_id,
                pending_decision_id=task.pending_decision_id,
                final_artifact_id=task.final_artifact_id,
                trace_id=task.trace_id,
                version=expected_version + 1,
                updated_at=task.updated_at,
            )
            try:
                await self._short.put(
                    memory,
                    expected_version=expected_version,
                )
                return
            except CreatorMemoryConflictError:
                if attempt == 1:
                    raise

    async def put_profile(
        self,
        profile: CreatorLongTermProfile,
        *,
        expected_version: int | None,
    ) -> CreatorLongTermProfile:
        if self._long is None:
            raise CreatorMemoryUnavailableError(
                "Creator long-term profile memory is disabled"
            )
        return await self._long.put(
            profile,
            expected_version=expected_version,
        )

    async def remember_used_content_angle(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        title: str,
        angle: str,
        task_id: str = "",
        artifact_id: str = "",
    ) -> UsedContentAngle | None:
        if self._long is None:
            return None
        entry = UsedContentAngle(
            angle_key=normalize_angle_key(title, angle),
            title=title.strip()[:512],
            angle=angle.strip()[:2_000],
            task_id=task_id,
            artifact_id=artifact_id,
            used_at=datetime.now(timezone.utc).isoformat(),
        )
        if not entry.angle_key:
            return None
        for attempt in range(2):
            existing = await self._long.get(
                tenant_id=tenant_id,
                creator_id=creator_id,
            )
            if existing is None:
                profile = CreatorLongTermProfile(
                    tenant_id=tenant_id,
                    creator_id=creator_id,
                    inferred_preferences={
                        "used_content_angles": [entry.model_dump(mode="json")]
                    },
                )
                try:
                    await self._long.put(profile, expected_version=None)
                    return entry
                except CreatorMemoryConflictError:
                    if attempt == 1:
                        raise
                    continue
            if any(
                item.angle_key == entry.angle_key
                for item in extract_used_angles(existing)
            ):
                return entry
            updated = append_used_angle(existing, entry)
            try:
                await self._long.put(
                    updated,
                    expected_version=existing.version,
                )
                return entry
            except CreatorMemoryConflictError:
                if attempt == 1:
                    raise
        return entry

    async def index_post(self, post: CreatorHistoricalPost) -> int:
        if self._semantic is None:
            raise CreatorMemoryUnavailableError("Creator semantic memory is disabled")
        return await self._semantic.upsert_post(post)

    async def delete_post(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        post_id: str,
    ) -> None:
        if self._semantic is None:
            raise CreatorMemoryUnavailableError("Creator semantic memory is disabled")
        await self._semantic.delete_post(
            tenant_id=tenant_id,
            creator_id=creator_id,
            post_id=post_id,
        )

    async def _load_short(
        self,
        query: CreatorMemoryQuery,
        *,
        authorized: bool,
    ) -> _LoadResult:
        if not authorized:
            return _disabled(MemoryTier.SHORT, self._short, "not requested")
        if self._short is None:
            return _disabled(MemoryTier.SHORT, None, "store disabled")
        try:
            value = await self._short.get(
                tenant_id=query.tenant_id,
                task_id=query.task_id,
            )
            if value is not None and (
                value.creator_id != query.creator_id or value.run_id != query.run_id
            ):
                raise CreatorMemoryIntegrityError(
                    "Creator task memory scope does not match the query",
                    details={"task_id": query.task_id},
                )
            return _available_or_empty(
                MemoryTier.SHORT,
                self._short.backend_name,
                value,
            )
        except Exception as exc:
            return _degraded(
                MemoryTier.SHORT,
                self._short.backend_name,
                exc,
                "Current task memory is temporarily unavailable.",
                query=query,
            )

    async def _load_long(
        self,
        query: CreatorMemoryQuery,
        *,
        authorized: bool,
    ) -> _LoadResult:
        if not authorized:
            return _disabled(
                MemoryTier.LONG,
                self._long,
                "not authorized by source_scope",
            )
        if self._long is None:
            return _disabled(MemoryTier.LONG, None, "store disabled")
        try:
            value = await self._long.get(
                tenant_id=query.tenant_id,
                creator_id=query.creator_id,
            )
            return _available_or_empty(
                MemoryTier.LONG,
                self._long.backend_name,
                value,
            )
        except Exception as exc:
            return _degraded(
                MemoryTier.LONG,
                self._long.backend_name,
                exc,
                "Creator profile memory is temporarily unavailable.",
                query=query,
            )

    async def _load_semantic(
        self,
        query: CreatorMemoryQuery,
        *,
        authorized: bool,
    ) -> _LoadResult:
        if not authorized:
            return _disabled(
                MemoryTier.SEMANTIC,
                self._semantic,
                "not authorized by source_scope",
            )
        if self._semantic is None:
            return _disabled(MemoryTier.SEMANTIC, None, "store disabled")
        raw_tags = query.source_scope.get("tags") or ()
        tag_values = (
            raw_tags if isinstance(raw_tags, (list, tuple, set, frozenset)) else ()
        )
        tags = tuple(str(tag) for tag in tag_values if str(tag).strip())[:20]
        try:
            hits = await self._semantic.search(
                tenant_id=query.tenant_id,
                creator_id=query.creator_id,
                query=query.query,
                limit=min(query.semantic_top_k, self._semantic_top_k),
                tags=tags,
            )
            bounded = tuple(
                hit.model_copy(
                    update={
                        "excerpt": _clip(
                            hit.excerpt,
                            self._max_excerpt_chars,
                        )
                    }
                )
                for hit in hits
            )
            return _available_or_empty(
                MemoryTier.SEMANTIC,
                self._semantic.backend_name,
                bounded,
            )
        except Exception as exc:
            return _degraded(
                MemoryTier.SEMANTIC,
                self._semantic.backend_name,
                exc,
                "Historical post memory is temporarily unavailable.",
                query=query,
            )


def _disabled(
    tier: MemoryTier,
    store: Any,
    detail: str,
) -> _LoadResult:
    return _LoadResult(
        value=None,
        report=CreatorMemorySourceReport(
            tier=tier,
            status=MemorySourceStatus.DISABLED,
            backend=getattr(store, "backend_name", "disabled"),
            detail=detail,
        ),
    )


def _available_or_empty(
    tier: MemoryTier,
    backend: str,
    value: Any,
) -> _LoadResult:
    if isinstance(value, tuple):
        count = len(value)
    else:
        count = 1 if value is not None else 0
    return _LoadResult(
        value=value,
        report=CreatorMemorySourceReport(
            tier=tier,
            status=(
                MemorySourceStatus.AVAILABLE if count else MemorySourceStatus.EMPTY
            ),
            backend=backend,
            record_count=count,
        ),
    )


def _degraded(
    tier: MemoryTier,
    backend: str,
    exc: Exception,
    limitation: str,
    *,
    query: CreatorMemoryQuery,
) -> _LoadResult:
    logger.warning(
        "Creator memory tier degraded tier=%s backend=%s tenant_id=%s "
        "creator_id=%s task_id=%s error=%s",
        tier.value,
        backend,
        query.tenant_id,
        query.creator_id,
        query.task_id,
        type(exc).__name__,
    )
    return _LoadResult(
        value=None,
        report=CreatorMemorySourceReport(
            tier=tier,
            status=MemorySourceStatus.DEGRADED,
            backend=backend,
            detail=type(exc).__name__,
        ),
        limitation=limitation,
    )


def _availability_for_source(
    report: CreatorMemorySourceReport,
    *,
    has_data: bool,
) -> MemoryAvailability:
    if has_data:
        return MemoryAvailability.AVAILABLE
    if report.status == MemorySourceStatus.EMPTY:
        return MemoryAvailability.PARTIAL
    return MemoryAvailability.NOT_CONNECTED


def _combined_availability(
    sources: list[MemoryAvailability],
) -> MemoryAvailability:
    if not sources:
        return MemoryAvailability.NOT_CONNECTED
    if all(source == MemoryAvailability.AVAILABLE for source in sources):
        return MemoryAvailability.AVAILABLE
    if any(
        source in {MemoryAvailability.AVAILABLE, MemoryAvailability.PARTIAL}
        for source in sources
    ):
        return MemoryAvailability.PARTIAL
    return MemoryAvailability.NOT_CONNECTED


def _clip(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
