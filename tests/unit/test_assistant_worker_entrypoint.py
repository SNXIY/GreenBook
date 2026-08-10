"""Phase 12-A standalone Runtime worker wiring tests."""

from __future__ import annotations

import pytest

from greenbook_assistant_core.execution.persistence_provider import RuntimePersistenceFactory


@pytest.mark.asyncio
async def test_worker_entrypoint_uses_durable_retry_store(monkeypatch) -> None:
    from greenbook_assistant_worker import main as worker_main

    persistence = RuntimePersistenceFactory.from_env(storage="memory")
    captured: dict[str, object] = {}

    class Java:
        def __init__(self, base_url: str) -> None:
            captured["java_base"] = base_url

        async def close(self) -> None:
            captured["java_closed"] = True

    class BackgroundWorker:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> None:
            captured["run"] = True

        async def shutdown(self) -> None:
            captured["shutdown"] = True

    monkeypatch.setattr(worker_main, "JavaClient", Java)
    monkeypatch.setattr(
        worker_main.RuntimePersistenceFactory,
        "from_env",
        classmethod(lambda cls: persistence),
    )
    monkeypatch.setattr(worker_main, "RetryBackgroundWorker", BackgroundWorker)
    monkeypatch.setenv("ASSISTANT_RETRY_WORKER_ID", "worker-12a")

    await worker_main.main()

    scheduler = captured["scheduler"]
    assert scheduler.task_store is persistence.retry_task_store
    assert scheduler.worker_id == "worker-12a"
    assert captured["run"] is True
    assert captured["shutdown"] is True
    assert captured["java_closed"] is True


@pytest.mark.asyncio
async def test_worker_entrypoint_can_consume_execution_queue(monkeypatch) -> None:
    from greenbook_assistant_worker import main as worker_main

    persistence = RuntimePersistenceFactory.from_env(storage="memory")
    captured: dict[str, object] = {}

    class Java:
        def __init__(self, base_url: str) -> None:
            captured["java_base"] = base_url

        async def close(self) -> None:
            captured["java_closed"] = True

    class BackgroundWorker:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self) -> None:
            captured["retry_run"] = True

        async def shutdown(self) -> None:
            captured["retry_shutdown"] = True

    class QueueWorker:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> None:
            captured["execution_run"] = True

        async def shutdown(self) -> None:
            captured["execution_shutdown"] = True

    handler = object()
    monkeypatch.setattr(worker_main, "JavaClient", Java)
    monkeypatch.setattr(
        worker_main.RuntimePersistenceFactory,
        "from_env",
        classmethod(lambda cls: persistence),
    )
    monkeypatch.setattr(worker_main, "RetryBackgroundWorker", BackgroundWorker)
    monkeypatch.setattr(worker_main, "ExecutionQueueWorker", QueueWorker)

    await worker_main.main(execution_handler=handler)

    assert captured["queue"] is persistence.execution_queue
    assert captured["execution_handler"] is handler
    assert captured["retry_run"] is True
    assert captured["execution_run"] is True
    assert captured["execution_shutdown"] is True
    assert captured["java_closed"] is True
