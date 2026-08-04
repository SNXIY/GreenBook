from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.llm import DeepSeekClient
from app.model_routing import ModelRouter
from app.tools import tool_registry


def settings(**overrides) -> Settings:
    return Settings(
        DEEPSEEK_API_KEY="test-key",
        service_shared_secret="test-secret",
        distributed_limits_enabled=False,
        **overrides,
    )


def test_operation_policy_routes_only_planning_to_thinking_pro() -> None:
    router = ModelRouter(settings())

    fast = router.candidates("adaptive.route")[0]
    planner = router.candidates("planner.plan")[0]
    verifier = router.candidates("verifier.verify")[0]

    assert (fast.model, fast.thinking) == ("deepseek-v4-flash", False)
    assert (planner.model, planner.thinking) == ("deepseek-v4-pro", True)
    assert (verifier.model, verifier.thinking) == ("deepseek-v4-pro", False)


def test_operation_policy_can_be_overridden_without_code_changes() -> None:
    router = ModelRouter(
        settings(model_route_overrides_json='{"answer.compose":"strong"}')
    )

    selected = router.candidates("answer.compose")[0]

    assert selected.tier == "strong"
    assert selected.thinking is True


def test_circuit_breaker_skips_unhealthy_primary_candidate() -> None:
    router = ModelRouter(settings(model_failure_threshold=2))
    primary = router.candidates("adaptive.route")[0]

    router.record_failure("adaptive.route", primary)
    router.record_failure("adaptive.route", primary)
    selected = router.candidates("adaptive.route")[0]

    assert selected.identity != primary.identity
    assert router.health()["cooldowns"]


@pytest.mark.asyncio
async def test_chat_falls_back_to_non_thinking_pro_on_transient_failure() -> None:
    client = DeepSeekClient(settings(), tool_registry)
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(503, request=request, json={"error": "busy"})
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    await client.http.aclose()
    client.http = httpx.AsyncClient(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client._chat(
            [{"role": "user", "content": "hello"}],
            temperature=0.0,
            operation="adaptive.route",
        )
    finally:
        await client.close()

    assert result == "ok"
    assert requests[0]["model"] == "deepseek-v4-flash"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[1]["model"] == "deepseek-v4-pro"
    assert requests[1]["thinking"] == {"type": "disabled"}
    assert client.model_router.health()["fallbacks"] == 1


def test_thinking_request_omits_ineffective_temperature() -> None:
    candidate = ModelRouter(settings()).candidates("planner.plan")[0]

    body = DeepSeekClient._request_body(
        candidate=candidate,
        messages=[{"role": "user", "content": "plan"}],
        temperature=0.1,
        json_mode=True,
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body
    assert body["response_format"] == {"type": "json_object"}
