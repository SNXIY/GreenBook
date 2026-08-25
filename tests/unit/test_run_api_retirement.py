"""Contract tests for the Legacy /runs history-only boundary."""

from greenbook_agent_api.api.routes import router as assistant_router
from greenbook_agent_api.api.runtime_routes import router as runtime_router


def _route_methods(path: str) -> set[str]:
    methods: set[str] = set()
    for route in assistant_router.routes:
        if getattr(route, "path", None) == path:
            methods.update(getattr(route, "methods", set()))
    return methods


def test_legacy_runs_keeps_history_lookup_only() -> None:
    assert _route_methods("/api/v1/agent/runs/{run_id}") == {"GET"}


def test_legacy_run_operations_are_retired() -> None:
    for path in (
        "/api/v1/agent/runs/{run_id}/cancel",
        "/api/v1/agent/runs/{run_id}/interrupt",
        "/api/v1/agent/runs/{run_id}/resume",
        "/api/v1/agent/runs/{run_id}/approve",
        "/api/v1/agent/runs/{run_id}/events",
        "/api/v1/agent/runs/{run_id}/events/stream",
    ):
        assert _route_methods(path) == set(), path


def test_legacy_approval_write_endpoints_are_retired() -> None:
    """Approval decisions go through the durable runtime boundary only:
    ``POST /runs/{run_id}/approvals/{approval_id}`` and
    ``POST /executions/{execution_id}/approve``.  The legacy in-memory
    ``/approvals/{id}/approve|reject`` store writes were removed in Phase 4."""
    for path in (
        "/api/v1/agent/approvals/{approval_id}/approve",
        "/api/v1/agent/approvals/{approval_id}/reject",
    ):
        assert _route_methods(path) == set(), path


def test_runtime_operations_are_canonical() -> None:
    paths = {
        route.path: set(getattr(route, "methods", set()))
        for route in runtime_router.routes
    }
    assert paths["/executions/{execution_id}/pause"] == {"POST"}
    assert paths["/executions/{execution_id}/resume"] == {"POST"}
    assert paths["/executions/{execution_id}/cancel"] == {"POST"}
    assert paths["/executions/{execution_id}/stream"] == {"GET"}
    assert _route_methods("/api/v1/agent/executions/{execution_id}/approve") == {"POST"}
