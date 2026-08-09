"""RuntimeRouter — decide whether a turn should use Legacy or Runtime path.

Phase 5.0: routing logic extracted from AssistantService.
"""

from __future__ import annotations

from enum import StrEnum

from ..models.runtime_context import RuntimeContext


class ExecutionPath(StrEnum):
    LEGACY = "legacy"
    RUNTIME = "runtime"


# ── Phase 5.0 supported scenarios ────────────────────────────────────

_SUPPORTED_SCENARIOS: set[tuple[str, str]] = {
    ("CREATE_CONTENT", "NEW_TASK"),
    ("IMPROVE_CONTENT", "MODIFY_TASK"),
    ("ANALYZE_COMMUNITY", "NEW_TASK"),
}


class RuntimeRouter:
    """Pure-function router: mode + task_intent → ExecutionPath.

    Does NOT depend on agent.py, MCP, or any external service.
    """

    def __init__(self, mode: str = "on") -> None:
        self._mode = mode  # off | dual | on

    # ── main entry ───────────────────────────────────────────────

    def route(self, ctx: RuntimeContext | None = None) -> ExecutionPath:
        """Return the execution path for *ctx*.

        When *ctx* is None (e.g. simple query without task_intent),
        always returns LEGACY.
        """
        if self._mode == "off":
            return ExecutionPath.LEGACY
        if self._mode in {"on", "dual"}:
            return ExecutionPath.RUNTIME
        # Legacy is available only through an explicit ``off`` mode.
        return ExecutionPath.LEGACY

    @staticmethod
    def _route_dual(ctx: RuntimeContext | None) -> ExecutionPath:
        """Compatibility alias retained for callers that used dual mode."""
        return ExecutionPath.RUNTIME

    # ── helpers for tests ────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @staticmethod
    def supported_scenarios() -> set[tuple[str, str]]:
        return set(_SUPPORTED_SCENARIOS)
