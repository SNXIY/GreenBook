import logging
from datetime import datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover - exercised only in minimal client installs
    Redis = None  # type: ignore[assignment,misc]


class RedisReviewQueueIndex:
    def __init__(self, url: str, key: str) -> None:
        self.url = url
        self.key = key
        self._client: Any = None

    async def start(self) -> None:
        if Redis is None:
            raise RuntimeError("redis is required when REDIS_URL is configured")
        self._client = Redis.from_url(self.url, decode_responses=True)
        await self._client.ping()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def enqueue(self, task_id: UUID, created_at: datetime) -> None:
        if self._client is None:
            return
        try:
            await self._client.zadd(self.key, {str(task_id): created_at.timestamp()})
        except Exception:
            logger.exception("Failed to add moderation task %s to Redis queue", task_id)

    async def remove(self, task_id: UUID) -> None:
        if self._client is None:
            return
        try:
            await self._client.zrem(self.key, str(task_id))
        except Exception:
            logger.exception("Failed to remove moderation task %s from Redis queue", task_id)
