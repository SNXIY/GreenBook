"""Test-only live Agent API launcher with an explicit MCP fault plan.

This module is intentionally under ``tests/``.  Normal ``start-agent.ps1``
never imports it and therefore never installs the monkeypatch.  The harness
starts the same FastAPI composition root on the canonical port with the same
PostgreSQL queue and in-process consumer; the only extra behavior is a
deterministic, file-configured boundary immediately before/after the existing
MCP tool call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "packages/agent_core",
    "packages/contracts",
    "packages/java_client",
    "packages/security",
    "services/greenbook_mcp",
    "apps/agent_api",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

# Keep the production launcher topology: one API process owns the queue
# consumer locally when dispatch is queue-backed.
os.environ.setdefault("GREENBOOK_AGENT_PROCESS_ROLE", "all")
os.environ.setdefault("GREENBOOK_AGENT_IN_PROCESS_WORKER", "true")
os.environ.setdefault("GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER", "true")

from greenbook_mcp_server.server import GreenBookMCPServer


class _FaultPlan:
    """Reload a test plan between cases without retaining test state."""

    def __init__(self) -> None:
        raw_path = os.getenv("GREENBOOK_TEST_FAULT_PLAN_FILE", "").strip()
        self.path = Path(raw_path) if raw_path else None
        self._digest = ""
        self._rules: list[dict[str, Any]] = []
        self._counts: dict[tuple[str, str], int] = {}
        self._used: set[tuple[str, str, int]] = set()

    def _reload(self) -> None:
        if self.path is None or not self.path.exists():
            digest = ""
            payload: dict[str, Any] = {}
        else:
            raw = self.path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                loaded = {}
            payload = loaded if isinstance(loaded, dict) else {}
        if digest == self._digest:
            return
        self._digest = digest
        self._rules = [
            dict(rule)
            for rule in payload.get("rules", [])
            if isinstance(rule, dict)
        ]
        self._counts.clear()
        self._used.clear()

    def match(self, *, run_id: str, tool_name: str) -> tuple[dict[str, Any], int] | None:
        self._reload()
        key = (run_id, tool_name)
        call_number = self._counts.get(key, 0) + 1
        self._counts[key] = call_number
        for rule in self._rules:
            configured_run = str(rule.get("run_id") or "*")
            configured_tool = str(rule.get("tool_name") or "*")
            if configured_run not in {"*", run_id}:
                continue
            if configured_tool not in {"*", tool_name}:
                continue
            numbers = rule.get("call_numbers", rule.get("call_number"))
            if numbers is None:
                numbers = [1]
            if isinstance(numbers, int):
                numbers = [numbers]
            if call_number not in {int(value) for value in numbers}:
                continue
            marker = (run_id, tool_name, call_number)
            if bool(rule.get("once", True)) and marker in self._used:
                continue
            self._used.add(marker)
            return rule, call_number
        return None

    def record(self, payload: dict[str, Any]) -> None:
        raw_path = os.getenv("GREENBOOK_TEST_FAULT_EVIDENCE_FILE", "").strip()
        if not raw_path:
            return
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


_PLAN = _FaultPlan()
_ORIGINAL_EXECUTE_TOOL = GreenBookMCPServer.execute_tool


def _failure_result(rule: dict[str, Any], *, trace_id: Any) -> dict[str, Any]:
    side_effect_started = bool(rule.get("side_effect_started", False))
    request_sent = rule.get("request_sent", False)
    return {
        "ok": False,
        "code": str(rule.get("error_code") or "TEST_INJECTED_FAILURE"),
        "message": "Test-only deterministic fault injection",
        # Keep the injected failure indistinguishable from a normal,
        # user-facing transient save failure.  Test labels remain in the
        # fault evidence only; they must never shape the visible conversation.
        "user_message": "这次保存没有完成，请稍后重试。",
        "retryable": bool(rule.get("retryable", False)),
        "request_sent": request_sent,
        "state": {
            "phase": "TEST_FAULT_INJECTION",
            "downstream_called": bool(rule.get("downstream_called", False)),
            "side_effect_started": side_effect_started,
            "safe_to_retry": not side_effect_started,
            "error_category": str(rule.get("error_category") or "TEST_FAILURE"),
        },
        "trace_id": trace_id,
    }


def _unknown_result(result: Any, *, trace_id: Any) -> Any:
    """Drop only the acknowledgement while preserving write evidence."""

    if not isinstance(result, dict):
        return result
    output = copy.deepcopy(result)
    receipt = output.get("operation_receipt")
    if isinstance(receipt, dict):
        receipt["result_known"] = False
        receipt["status"] = "RESULT_UNKNOWN"
        output["operation_receipt"] = receipt
    output.update(
        {
            "ok": False,
            "code": "RESULT_UNKNOWN",
            "message": "Test-only acknowledgement loss after Java write",
            "user_message": "Test-only acknowledgement loss after Java write",
            "retryable": False,
            "request_sent": None,
            "state": {
                "phase": "TEST_FAULT_RESPONSE_LOST",
                "downstream_called": True,
                "side_effect_started": True,
                "safe_to_retry": False,
                "result_known": False,
            },
            "trace_id": trace_id,
        }
    )
    return output


async def _execute_tool_with_test_fault(self: GreenBookMCPServer, tool_name: str, **kwargs: Any) -> Any:
    run_id = str(kwargs.get("agent_run_id") or "")
    matched = _PLAN.match(run_id=run_id, tool_name=tool_name)
    if matched is None:
        return await _ORIGINAL_EXECUTE_TOOL(self, tool_name, **kwargs)

    rule, call_number = matched
    base_record = {
        "run_id": run_id,
        "tool_name": tool_name,
        "call_number": call_number,
        "mode": str(rule.get("mode") or "FAIL_BEFORE"),
        "error_category": str(rule.get("error_category") or "TEST_FAILURE"),
        "side_effect_started": bool(rule.get("side_effect_started", False)),
        "tool_call_id": kwargs.get("tool_call_id"),
    }
    mode = str(rule.get("mode") or "FAIL_BEFORE").upper()
    if mode == "FAIL_BEFORE":
        result = _failure_result(rule, trace_id=kwargs.get("trace_id"))
        _PLAN.record({**base_record, "phase": "before_tool", "result": result})
        return result
    if mode == "ACK_LOSS":
        result = await _ORIGINAL_EXECUTE_TOOL(self, tool_name, **kwargs)
        unknown = _unknown_result(result, trace_id=kwargs.get("trace_id"))
        _PLAN.record({**base_record, "phase": "after_tool", "result": unknown})
        return unknown
    raise RuntimeError(f"Unsupported test fault mode: {mode}")


GreenBookMCPServer.execute_tool = _execute_tool_with_test_fault


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("GREENBOOK_AGENT_API_PORT", "8094")))
    args = parser.parse_args()
    os.environ["GREENBOOK_AGENT_API_PORT"] = str(args.port)
    uvicorn.run(
        "apps.agent_api.greenbook_agent_api.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        reload=False,
    )
