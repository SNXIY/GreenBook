from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.creator.application.ports import CreatorUnitOfWorkFactory
from app.creator.domain.errors import CreatorPersistenceConflictError
from app.creator.domain.models import CreatorOutboxMessage, OutboxStatus
from app.creator.worker.service import CreatorWorkerError

_REPLAY_NAMESPACE = uuid.UUID("d8d9d109-a453-4a25-8e31-54cd66443fb1")


@dataclass(frozen=True)
class CreatorOutboxReplayResult:
    source_message_id: str
    replay_message_id: str
    replayed: bool


async def replay_dead_outbox(
    *,
    uow_factory: CreatorUnitOfWorkFactory,
    message_id: str,
    operator_id: str,
    reason: str,
    now: datetime | None = None,
) -> CreatorOutboxReplayResult:
    source_id = _required_value(message_id, "message_id", max_length=64)
    operator = _required_value(operator_id, "operator_id", max_length=128)
    replay_reason = _required_value(reason, "reason", max_length=500)
    replay_id = str(
        uuid.uuid5(
            _REPLAY_NAMESPACE,
            f"{source_id}\n{operator}\n{replay_reason}",
        )
    )
    requested_at = now or datetime.now(UTC)

    try:
        async with uow_factory() as uow:
            source = await uow.outbox.get(source_id, for_update=True)
            if source is None:
                raise CreatorWorkerError(
                    f"Creator outbox message {source_id} was not found"
                )
            if source.status != OutboxStatus.DEAD:
                raise CreatorWorkerError(
                    f"Creator outbox message {source_id} is "
                    f"{source.status.value}, not DEAD"
                )
            existing = await uow.outbox.get(replay_id)
            if existing is not None:
                return CreatorOutboxReplayResult(
                    source_message_id=source_id,
                    replay_message_id=replay_id,
                    replayed=True,
                )
            await uow.outbox.add(
                CreatorOutboxMessage(
                    id=replay_id,
                    aggregate_type=source.aggregate_type,
                    aggregate_id=source.aggregate_id,
                    topic=source.topic,
                    payload={
                        **source.payload,
                        "_operator_replay": {
                            "source_message_id": source_id,
                            "operator_id": operator,
                            "reason": replay_reason,
                            "requested_at": requested_at.isoformat(),
                        },
                    },
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    available_at=requested_at,
                    created_at=requested_at,
                    updated_at=requested_at,
                )
            )
            await uow.commit()
    except CreatorPersistenceConflictError:
        async with uow_factory() as uow:
            existing = await uow.outbox.get(replay_id)
            if existing is None:
                raise
        return CreatorOutboxReplayResult(
            source_message_id=source_id,
            replay_message_id=replay_id,
            replayed=True,
        )
    return CreatorOutboxReplayResult(
        source_message_id=source_id,
        replay_message_id=replay_id,
        replayed=False,
    )


def _required_value(value: str, name: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise CreatorWorkerError(
            f"{name} must contain between 1 and {max_length} characters"
        )
    return normalized
