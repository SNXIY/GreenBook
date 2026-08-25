"""Translate Runtime results into the Agent product response contract."""

from __future__ import annotations

from .presenter import AgentResponse, ExecutionResultPresenter
from .runtime_result import RuntimeResult


class ExecutionProjectionAdapter:
    """Keep Runtime state models out of the Agent presentation boundary."""

    def __init__(self, presenter: ExecutionResultPresenter | None = None) -> None:
        self._presenter = presenter or ExecutionResultPresenter()

    def project(self, result: RuntimeResult) -> AgentResponse:
        return self._presenter.present(result)


__all__ = ["ExecutionProjectionAdapter"]
