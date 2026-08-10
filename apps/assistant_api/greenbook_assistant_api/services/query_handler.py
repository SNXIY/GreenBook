"""Read-only Query handling for the conversation goal graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from greenbook_assistant_core.task.intent_models import ActionType, IntentSpec
from greenbook_assistant_core.task.models import ArtifactRef


class QueryHandlerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueryRequest:
    message: str
    intent: IntentSpec
    conversation_id: str
    run_id: str
    trace_id: str
    mcp: Any = None
    auth: Any = None
    session: Any = None


@dataclass(slots=True)
class QueryResult:
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)


class QueryHandler(Protocol):
    async def handle(self, request: QueryRequest) -> QueryResult:
        """Execute a read-only query and return result/artifact handles."""


class ReadOnlyQueryHandler:
    """QueryAgent boundary with an allow-list of read-only operations.

    The current MCP server exposes ``community.search_public_posts`` as the
    canonical read tool.  The semantic operations search/analyze/list are
    mapped to that read source and never to create/update/publish tools.  A
    future MCP transport can provide specialized aliases without changing
    this boundary.
    """

    _READ_TOOL = "community.search_public_posts"
    _OPERATIONS = {
        ActionType.SEARCH: "community.search_posts",
        ActionType.ANALYZE: "community.analyze_posts",
        ActionType.QUERY: "community.list_posts",
    }
    _READ_ACTIONS = frozenset({ActionType.QUERY, ActionType.SEARCH, ActionType.ANALYZE})
    _WRITE_ACTIONS = frozenset({
        ActionType.CREATE, ActionType.UPDATE, ActionType.DELETE,
        ActionType.PUBLISH, ActionType.UPDATE_OR_CREATE,
    })

    async def handle(self, request: QueryRequest) -> QueryResult:
        actions = {action.action for action in request.intent.actions}
        if actions & self._WRITE_ACTIONS or not actions <= self._READ_ACTIONS:
            raise QueryHandlerError("QUERY_HANDLER_WRITE_ACTION_REJECTED")
        if request.mcp is None:
            return QueryResult(
                content=request.intent.goal or "只读查询已完成。",
                data={},
            )

        raw = await request.mcp.execute_tool(
            self._READ_TOOL,
            auth=request.auth,
            session=request.session,
            trace_id=request.trace_id,
            agent_run_id=request.run_id,
            query=request.message,
        )
        if not isinstance(raw, dict):
            raise QueryHandlerError("QUERY_HANDLER_INVALID_RESULT")
        if not raw.get("ok", False):
            raise QueryHandlerError(str(raw.get("code") or "QUERY_FAILED"))
        data = raw.get("data")
        if not isinstance(data, dict):
            data = {"result": data}
        artifact = ArtifactRef(
            task_id=f"query:{request.run_id}",
            artifact_type="QUERY_RESULT",
            summary=request.intent.goal,
        )
        return QueryResult(
            content=request.intent.goal or "只读查询已完成。",
            data={
                "read_only": True,
                "operation": self._operation(request.intent),
                "tool": self._READ_TOOL,
                **data,
            },
            artifacts=[artifact],
        )

    @classmethod
    def _operation(cls, intent: IntentSpec) -> str:
        for action in intent.actions:
            operation = cls._OPERATIONS.get(action.action)
            if operation:
                return operation
        return "community.list_posts"


__all__ = [
    "QueryHandler",
    "QueryHandlerError",
    "QueryRequest",
    "QueryResult",
    "ReadOnlyQueryHandler",
]
