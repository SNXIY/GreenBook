from __future__ import annotations

import hashlib
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis

from app.creator.memory.errors import (
    CreatorMemoryConflictError,
    CreatorMemoryIntegrityError,
)
from app.creator.memory.models import CreatorTaskMemory


_UPSERT_SCRIPT = """
local current_raw = redis.call('GET', KEYS[1])
local current_version = 0
if current_raw then
    local current = cjson.decode(current_raw)
    current_version = tonumber(current['version']) or 0
end
local expected = ARGV[1]
if expected ~= '*' and current_version ~= tonumber(expected) then
    return {0, tostring(current_version)}
end
local incoming = cjson.decode(ARGV[2])
incoming['version'] = current_version + 1
local encoded = cjson.encode(incoming)
redis.call('SET', KEYS[1], encoded, 'EX', tonumber(ARGV[3]))
return {1, encoded}
"""


class RedisCreatorShortTermMemoryStore:
    backend_name = "redis"

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        ttl_seconds: int,
        socket_timeout_seconds: float = 2.0,
        client: Any | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Creator short memory TTL must be greater than zero")
        if client is None and not redis_url:
            raise ValueError("redis_url is required when no Redis client is injected")
        self._ttl_seconds = ttl_seconds
        self._client = client or Redis.from_url(
            str(redis_url),
            decode_responses=True,
            socket_timeout=socket_timeout_seconds,
            socket_connect_timeout=socket_timeout_seconds,
        )
        self._owns_client = client is None

    async def get(
        self,
        *,
        tenant_id: str,
        task_id: str,
    ) -> CreatorTaskMemory | None:
        raw = await self._client.get(_key(tenant_id, task_id))
        if raw is None:
            return None
        try:
            memory = CreatorTaskMemory.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError) as exc:
            raise CreatorMemoryIntegrityError(
                "Redis creator task memory is malformed",
                details={"tenant_id": tenant_id, "task_id": task_id},
            ) from exc
        if memory.tenant_id != tenant_id or memory.task_id != task_id:
            raise CreatorMemoryIntegrityError(
                "Redis creator task memory scope does not match its key",
                details={"tenant_id": tenant_id, "task_id": task_id},
            )
        return memory

    async def put(
        self,
        memory: CreatorTaskMemory,
        *,
        expected_version: int | None,
    ) -> CreatorTaskMemory:
        expected = "*" if expected_version is None else str(expected_version)
        raw_result = await self._client.eval(
            _UPSERT_SCRIPT,
            1,
            _key(memory.tenant_id, memory.task_id),
            expected,
            memory.model_dump_json(),
            str(self._ttl_seconds),
        )
        if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 2:
            raise CreatorMemoryIntegrityError(
                "Redis creator task memory returned an invalid write result"
            )
        accepted = int(raw_result[0])
        value = raw_result[1]
        if accepted != 1:
            raise CreatorMemoryConflictError(
                "Creator task memory changed concurrently",
                details={
                    "tenant_id": memory.tenant_id,
                    "task_id": memory.task_id,
                    "expected_version": expected_version,
                    "actual_version": int(value),
                },
            )
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return CreatorTaskMemory.model_validate_json(value)

    async def delete(self, *, tenant_id: str, task_id: str) -> None:
        await self._client.delete(_key(tenant_id, task_id))

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _key(tenant_id: str, task_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}:{task_id}".encode("utf-8")).hexdigest()
    return f"mindflow:creator:task-memory:{digest}"
