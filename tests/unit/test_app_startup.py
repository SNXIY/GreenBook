"""Application startup regression: the lifespan must initialize the LLM before
the MCP server so the assistant-first direct-draft path is wired (a previous
change referenced app.state.llm before it was assigned and startup crashed)."""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import pytest


def _run_startup() -> None:
    import apps.agent_api.greenbook_agent_api.main as main

    app = main.create_app()

    async def start() -> None:
        async with app.router.lifespan_context(app):
            mcp = app.state.mcp
            assert mcp.llm is not None, "MCP server must receive the host LLM"
            assert app.state.llm is not None
            assert app.state.model

    asyncio.run(start())


def _postgres_available() -> bool:
    import socket

    try:
        s = socket.create_connection(("127.0.0.1", 25432), timeout=2)
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.parametrize("key_name", ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"])
def test_app_lifespan_starts_with_llm_wired(key_name: str) -> None:
    if not _postgres_available():
        pytest.skip("PostgreSQL (127.0.0.1:25432) is required for this startup test")
    env = {
        key_name: "test-key",
        "GREENBOOK_AGENT_EXECUTION_DISPATCH": "direct",
        "GREENBOOK_AGENT_RUNTIME_STORAGE": "memory",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        _run_startup()
