"""History compatibility for legacy run identifiers and execution references."""

from .execution_reference import ExecutionReference, build_execution_reference
from .run_execution_link import (
    DuplicateRunExecutionBindingError,
    RunExecutionAdapter,
    RunExecutionLink,
    RunExecutionLinkSource,
)
from .run_execution_repository import (
    InMemoryRunExecutionLinkRepository,
    PostgresRunExecutionLinkRepository,
    RunExecutionLinkRepository,
    SqlAlchemyRunExecutionLinkRepository,
)

__all__ = [
    "DuplicateRunExecutionBindingError",
    "RunExecutionAdapter",
    "RunExecutionLink",
    "RunExecutionLinkSource",
    "InMemoryRunExecutionLinkRepository",
    "PostgresRunExecutionLinkRepository",
    "RunExecutionLinkRepository",
    "SqlAlchemyRunExecutionLinkRepository",
    "ExecutionReference",
    "build_execution_reference",
]
