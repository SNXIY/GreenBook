from __future__ import annotations

import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_FIXED_WINDOW = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
local ttl = redis.call('TTL', KEYS[1])
if current > tonumber(ARGV[1]) then
  return {0, current, ttl}
end
return {1, current, ttl}
"""


class DistributedLimitExceeded(RuntimeError):
    def __init__(self, *, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"分布式模型调用限流触发（{scope}），"
            f"约 {max(1, retry_after_seconds)} 秒后重试"
        )
        self.scope = scope
        self.retry_after_seconds = max(1, retry_after_seconds)


class DistributedRateLimiter:
    def __init__(
        self,
        *,
        redis_url: str,
        enabled: bool,
        required: bool,
        global_requests_per_minute: int,
        user_requests_per_minute: int,
    ) -> None:
        self.enabled = enabled
        self.required = required
        self.global_limit = global_requests_per_minute
        self.user_limit = user_requests_per_minute
        self.redis = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2.0,
        )
        self.available = False

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            await self.redis.ping()
            self.available = True
        except Exception:
            if self.required:
                raise
            logger.exception(
                "Assistant distributed limiter unavailable; "
                "continuing with database concurrency limits"
            )

    async def close(self) -> None:
        await self.redis.aclose()

    async def consume_model_call(self, *, user_id: str) -> None:
        if not self.enabled or not self.available:
            return
        await self._consume(
            key="assistant:limit:model:global",
            limit=self.global_limit,
            scope="global",
        )
        await self._consume(
            key=f"assistant:limit:model:user:{user_id}",
            limit=self.user_limit,
            scope=f"user:{user_id}",
        )

    async def _consume(self, *, key: str, limit: int, scope: str) -> None:
        try:
            allowed, _, ttl = await self.redis.eval(
                _FIXED_WINDOW,
                1,
                key,
                limit,
                60,
            )
        except Exception:
            if self.required:
                raise
            self.available = False
            logger.exception(
                "Assistant distributed limiter failed open; "
                "database concurrency limits remain active"
            )
            return
        if int(allowed) != 1:
            raise DistributedLimitExceeded(
                scope=scope,
                retry_after_seconds=int(ttl),
            )
