"""Ownership policy for Runtime execution resources.

The Runtime repository stores execution facts and links each execution to a
Task.  This policy follows that link to the persisted Task and compares the
authenticated user and tenant scope before an execution is exposed.  It is
kept at the API boundary so neither the execution state model nor the legacy
run storage needs to know about HTTP authorization.
"""

from __future__ import annotations

from typing import Any

from greenbook_contracts.identity import AuthContext

from .task_provider import TaskProvider


class ExecutionAuthorizer:
    """Fail-closed, async ownership authorizer for Runtime executions."""

    def __init__(self, *, task_provider: TaskProvider) -> None:
        self._task_provider = task_provider

    async def __call__(self, auth_context: AuthContext | Any, execution: Any) -> bool:
        """Return whether ``auth_context`` owns the execution's Task.

        Missing identity, tenant, task links, malformed values, and provider
        failures all deny access.  The authorizer never trusts a task id from
        the HTTP query/path; it only follows the task id on the Runtime
        ``PlanExecution`` loaded from the canonical execution repository.
        """

        user_id = getattr(auth_context, "user_id", None)
        tenant_id = getattr(auth_context, "tenant_id", None)
        task_id = getattr(execution, "task_id", None)
        if not all(
            isinstance(value, str) and value.strip()
            for value in (user_id, tenant_id, task_id)
        ):
            return False

        try:
            return await self._task_provider.authorize_task(
                task_id=task_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        except Exception:
            # Authorization must not turn a storage/provider failure into an
            # accidental allow.  The HTTP layer maps this to 403.
            return False


__all__ = ["ExecutionAuthorizer"]
