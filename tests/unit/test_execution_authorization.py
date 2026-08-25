"""Ownership authorization tests for Runtime execution resources."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.planning.validation import PlanValidator

from apps.agent_api.greenbook_agent_api.api.runtime_routes import (
    get_execution_status,
)
from apps.agent_api.greenbook_agent_api.services.execution_authorizer import (
    ExecutionAuthorizer,
)
from tests.plan_factory import GoalPlanFactory


class _TaskProvider:
    def __init__(self, allowed: set[tuple[str, str, str]]) -> None:
        self.allowed = allowed

    async def authorize_task(self, *, task_id: str, user_id: str, tenant_id: str) -> bool:
        return (task_id, user_id, tenant_id) in self.allowed


class _Request:
    def __init__(
        self,
        manager: RuntimeManager,
        authorizer: ExecutionAuthorizer,
        *,
        user_id: str | None,
        tenant_id: str = "tenant-1",
    ) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                execution_runtime_manager=manager,
                execution_authorizer=authorizer,
            )
        )
        self.state = SimpleNamespace(
            auth_context=(
                SimpleNamespace(user_id=user_id, tenant_id=tenant_id)
                if user_id is not None
                else None
            )
        )


@pytest.fixture(autouse=True)
def clear_store() -> None:
    ExecutionRepository.clear()


def _runtime() -> tuple[RuntimeManager, str]:
    registry = CapabilityRegistry()
    plan = GoalPlanFactory(registry).generate_plan(
        task_id="task-owned-by-user-1",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    manager = RuntimeManager(ExecutionStateManager(ExecutionRepository()))
    execution = manager.create_execution(plan, executable)
    return manager, execution.execution_id


@pytest.mark.asyncio
async def test_owner_can_read_execution() -> None:
    manager, execution_id = _runtime()
    authorizer = ExecutionAuthorizer(
        task_provider=_TaskProvider(
            {("task-owned-by-user-1", "user-1", "tenant-1")}
        )
    )

    response = await get_execution_status(
        execution_id,
        _Request(manager, authorizer, user_id="user-1"),
    )

    assert response.execution_id == execution_id


@pytest.mark.asyncio
async def test_other_user_cannot_read_execution() -> None:
    manager, execution_id = _runtime()
    authorizer = ExecutionAuthorizer(
        task_provider=_TaskProvider(
            {("task-owned-by-user-1", "user-1", "tenant-1")}
        )
    )

    with pytest.raises(HTTPException) as raised:
        await get_execution_status(
            execution_id,
            _Request(manager, authorizer, user_id="user-2"),
        )

    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected() -> None:
    manager, execution_id = _runtime()
    authorizer = ExecutionAuthorizer(task_provider=_TaskProvider(set()))

    with pytest.raises(HTTPException) as raised:
        await get_execution_status(
            execution_id,
            _Request(manager, authorizer, user_id=None),
        )

    assert raised.value.status_code == 401
