from __future__ import annotations

import unittest

import httpx
from fastapi import FastAPI

from app.api.routes import router
from app.creator.api.composition import CreatorApiRuntime


class _CreatorDatabase:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def ping(self) -> None:
        if self._error is not None:
            raise self._error


def _creator_runtime(database: _CreatorDatabase) -> CreatorApiRuntime:
    return CreatorApiRuntime(
        workspace=None,  # type: ignore[arg-type]
        query=None,  # type: ignore[arg-type]
        dispatcher=None,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        execution_mode="outbox-worker",
        sse_poll_seconds=1,
        sse_heartbeat_seconds=15,
        sse_send_timeout_seconds=15,
    )


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.app = app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://health.test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_liveness_does_not_depend_on_datastores(self) -> None:
        response = await self.client.get("/actuator/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "UP"})

    async def test_readiness_checks_only_creator_database(self) -> None:
        self.app.state.creator_api = _creator_runtime(_CreatorDatabase())

        response = await self.client.get("/actuator/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "UP",
                "checks": {"creator_database": "UP"},
            },
        )

    async def test_readiness_is_down_when_creator_runtime_is_missing(self) -> None:
        response = await self.client.get("/actuator/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["checks"],
            {"creator_database": "DOWN"},
        )

    async def test_readiness_hides_database_errors(self) -> None:
        self.app.state.creator_api = _creator_runtime(
            _CreatorDatabase(RuntimeError("secret connection details"))
        )

        response = await self.client.get("/actuator/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "DOWN")
        self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
