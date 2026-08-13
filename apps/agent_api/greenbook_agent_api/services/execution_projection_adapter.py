"""Translate Runtime results into the Agent product response contract."""

from __future__ import annotations

from ..models.runtime_result import RuntimeResult
from .execution_presenter import AgentResponse, ExecutionResultPresenter


class ExecutionProjectionAdapter:
    """Keep Runtime state models out of the Agent presentation boundary."""

    def __init__(self, presenter: ExecutionResultPresenter | None = None) -> None:
        self._presenter = presenter or ExecutionResultPresenter()

    def project(self, result: RuntimeResult) -> AgentResponse:
        return self._presenter.present(result)


__all__ = ["ExecutionProjectionAdapter"]
