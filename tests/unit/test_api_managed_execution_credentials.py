from __future__ import annotations

import base64
import json

import pytest

from greenbook_agent_api.services.execution_credential_broker import (
    ExecutionCredentialBroker,
)
from greenbook_agent_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueStatus,
)
from greenbook_agent_core.execution.execution_queue_worker import (
    ExecutionHandlerDeferredError,
    ExecutionQueueWorker,
)
from greenbook_contracts.identity import AuthContext


def _token(*, exp: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


def _message(queue: ExecutionQueue, *, user_id: str = "user-1"):
    return queue.enqueue(
        "execution-credential",
        payload={
            "user_id": user_id,
            "tenant_id": "tenant-1",
            "auth_context": {
                "user_id": user_id,
                "tenant_id": "tenant-1",
            },
        },
    )


def test_broker_resolves_only_matching_validated_identity() -> None:
    broker = ExecutionCredentialBroker(now_factory=lambda: 100.0)
    broker.register(
        AuthContext(
            user_id="user-1",
            tenant_id="tenant-1",
            raw_access_token=_token(exp=200),
        )
    )
    queue = ExecutionQueue()

    resolved = broker.resolve(_message(queue))

    assert resolved.user_id == "user-1"
    assert resolved.tenant_id == "tenant-1"
    assert resolved.raw_access_token == _token(exp=200)


def test_broker_rejects_missing_or_expired_credentials_without_fallback() -> None:
    broker = ExecutionCredentialBroker(now_factory=lambda: 200.0)
    broker.register(
        AuthContext(
            user_id="user-1",
            tenant_id="tenant-1",
            raw_access_token=_token(exp=100),
        )
    )
    queue = ExecutionQueue()

    with pytest.raises(ExecutionHandlerDeferredError):
        broker.resolve(_message(queue))

    other_queue = ExecutionQueue()
    with pytest.raises(ExecutionHandlerDeferredError):
        broker.resolve(_message(other_queue, user_id="user-2"))


@pytest.mark.asyncio
async def test_missing_process_local_credential_releases_queue_message() -> None:
    broker = ExecutionCredentialBroker(now_factory=lambda: 100.0)
    queue = ExecutionQueue()
    message = _message(queue)
    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=broker.resolve,
        worker_id="api-managed-worker",
    )

    handled = await worker.run_once()

    assert handled == []
    released = queue.get(message.message_id)
    assert released is not None
    assert released.status == ExecutionQueueStatus.READY
    assert released.last_error == ""
    assert "token" not in json.dumps(released.payload).lower()
