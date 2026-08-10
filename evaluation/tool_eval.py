"""Tool and Runtime outcome metrics from real execution observations."""

from __future__ import annotations

from typing import Any


def tool_metrics(observations: list[dict[str, Any]]) -> dict[str, float | int]:
    calls = [call for item in observations for call in item.get("tool_calls", [])]
    total = len(calls)
    success = sum(bool(call.get("success")) for call in calls)
    failures = sum(not bool(call.get("success")) for call in calls)
    timeouts = sum(str(call.get("status", "")).upper() == "TIMEOUT" for call in calls)
    executions = [item for item in observations if item.get("execution_status")]
    successful_executions = sum(
        str(item.get("execution_status")).upper() == "COMPLETED" for item in executions
    )
    steps = [step for item in observations for step in item.get("steps", [])]
    successful_steps = sum(str(step.get("status")).upper() == "COMPLETED" for step in steps)
    return {
        "tool_calls": total,
        "tool_success": success,
        "tool_failures": failures,
        "tool_timeouts": timeouts,
        "tool_success_rate": success / total if total else 0.0,
        "runtime_executions": len(executions),
        "runtime_success_rate": successful_executions / len(executions) if executions else 0.0,
        "steps": len(steps),
        "step_success_rate": successful_steps / len(steps) if steps else 0.0,
    }


def recovery_metrics(observations: list[dict[str, Any]]) -> dict[str, float | int]:
    attempts = sum(int(item.get("retry_attempts", 0)) for item in observations)
    retry_success = sum(bool(item.get("retry_succeeded")) for item in observations)
    restarts = sum(bool(item.get("worker_restart")) for item in observations)
    restart_recovered = sum(bool(item.get("worker_restart_recovered")) for item in observations)
    reconciliation = sum(bool(item.get("reconciliation_succeeded")) for item in observations)
    return {
        "retry_attempts": attempts,
        "retry_success": retry_success,
        "retry_success_rate": retry_success / attempts if attempts else 0.0,
        "reconciliation_succeeded": reconciliation,
        "worker_restarts": restarts,
        "worker_restart_recovery_rate": restart_recovered / restarts if restarts else 0.0,
    }
