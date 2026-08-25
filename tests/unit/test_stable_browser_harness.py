from __future__ import annotations

import asyncio
import json

import pytest

from scripts.dev.overnight_stable_baseline_browser import (
    Browser,
    _run_id,
    load_utf8_cases,
    newest_run,
)


def test_browser_case_file_uses_strict_utf8(tmp_path):
    path = tmp_path / "cases.json"
    path.write_bytes(
        json.dumps(
            [{"name": "utf8", "turns": [{"text": "删除这篇帖子"}]}],
            ensure_ascii=False,
        ).encode("utf-8")
    )

    cases = load_utf8_cases(path)

    assert cases[0]["turns"][0]["text"] == "删除这篇帖子"


def test_browser_case_file_rejects_replacement_character(tmp_path):
    path = tmp_path / "damaged.json"
    path.write_text(
        json.dumps([{"name": "damaged", "turns": [{"text": "删除�帖子"}]}], ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="replacement character"):
        load_utf8_cases(path)


def test_browser_evaluate_reconnects_after_navigation(monkeypatch):
    browser = Browser("ws://old-target")
    attempts = []
    reconnects = []

    async def command(method, params):
        attempts.append((method, params))
        if len(attempts) == 1:
            raise RuntimeError("CDP Runtime.evaluate: {'message': 'Inspected target navigated or closed'}")
        return {"result": {"value": "ready"}}

    async def close():
        reconnects.append("close")

    async def connect():
        reconnects.append("connect")

    browser.command = command
    browser.close = close
    browser.connect = connect
    monkeypatch.setattr(
        "scripts.dev.overnight_stable_baseline_browser.find_page",
        lambda: "ws://new-target",
    )

    value = asyncio.run(browser.evaluate("location.pathname"))

    assert value == "ready"
    assert browser.ws_url == "ws://new-target"
    assert reconnects == ["close", "connect"]
    assert len(attempts) == 2


def test_newest_run_ignores_non_run_projection_rows() -> None:
    durable = {
        "run_id": "durable-run",
        "created_at": "2026-08-24T10:00:00+00:00",
    }
    non_run = {
        "created_at": "2026-08-24T10:01:00+00:00",
        "status": "RUNNING",
    }

    assert _run_id({"payload": {"run_id": "nested-run"}}) == "nested-run"
    nested = {
        "payload": {
            "run_id": "nested-run",
            "created_at": "2026-08-24T10:02:00+00:00",
        },
    }

    assert newest_run([durable, non_run, nested]) == nested
