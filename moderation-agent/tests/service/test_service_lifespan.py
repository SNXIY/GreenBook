from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_lifespan_initializes_moderation_runtime(monkeypatch) -> None:
    from service import service

    class Component:
        def __init__(self) -> None:
            self.setup_called = False

        async def setup(self) -> None:
            self.setup_called = True

    saver = Component()
    store = Component()
    graph = type("Graph", (), {"checkpointer": None, "store": None})()
    services = object()

    @asynccontextmanager
    async def fake_initialize_database():
        yield saver

    @asynccontextmanager
    async def fake_initialize_store():
        yield store

    @asynccontextmanager
    async def fake_initialize_moderation_services(received_graph):
        assert received_graph is graph
        yield services

    monkeypatch.setattr(service, "initialize_database", fake_initialize_database)
    monkeypatch.setattr(service, "initialize_store", fake_initialize_store)
    monkeypatch.setattr(
        service,
        "initialize_moderation_services",
        fake_initialize_moderation_services,
    )
    monkeypatch.setattr(service, "moderation_agent", graph)

    application = FastAPI()
    async with service.lifespan(application):
        assert application.state.moderation_services is services

    assert saver.setup_called
    assert store.setup_called
    assert graph.checkpointer is saver
    assert graph.store is store
    assert not hasattr(application.state, "moderation_services")
