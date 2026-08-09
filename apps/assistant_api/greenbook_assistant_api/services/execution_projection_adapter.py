"""Translate Runtime results into the Assistant product response contract."""

from __future__ import annotations

from ..models.runtime_result import RuntimeResult
from .execution_presenter import AssistantResponse, ExecutionResultPresenter


class ExecutionProjectionAdapter:
    """Keep Runtime state models out of the Assistant presentation boundary."""

    def __init__(self, presenter: ExecutionResultPresenter | None = None) -> None:
        self._presenter = presenter or ExecutionResultPresenter()

    def project(self, result: RuntimeResult) -> AssistantResponse:
        return self._presenter.present(result)


__all__ = ["ExecutionProjectionAdapter"]
