import pytest

from app.rate_limit import DistributedLimitExceeded, DistributedRateLimiter


class FakeRedis:
    def __init__(self, results: list[list[int]]) -> None:
        self.results = list(results)
        self.keys: list[str] = []

    async def eval(self, _script, _key_count, key, _limit, _window):
        self.keys.append(key)
        return self.results.pop(0)


def limiter_with(fake: FakeRedis) -> DistributedRateLimiter:
    limiter = DistributedRateLimiter(
        redis_url="redis://127.0.0.1:6379/0",
        enabled=True,
        required=True,
        global_requests_per_minute=60,
        user_requests_per_minute=12,
    )
    limiter.redis = fake
    limiter.available = True
    return limiter


@pytest.mark.asyncio
async def test_distributed_limiter_applies_global_and_user_windows() -> None:
    fake = FakeRedis([[1, 1, 60], [1, 1, 60]])
    limiter = limiter_with(fake)

    await limiter.consume_model_call(user_id="user-7")

    assert fake.keys == [
        "assistant:limit:model:global",
        "assistant:limit:model:user:user-7",
    ]


@pytest.mark.asyncio
async def test_distributed_limiter_exposes_retry_window() -> None:
    limiter = limiter_with(FakeRedis([[0, 61, 23]]))

    with pytest.raises(DistributedLimitExceeded) as raised:
        await limiter.consume_model_call(user_id="user-7")

    assert raised.value.scope == "global"
    assert raised.value.retry_after_seconds == 23
