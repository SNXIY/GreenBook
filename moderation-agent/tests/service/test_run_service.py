import asyncio

from uvicorn import Config

from run_service import windows_selector_loop_factory


def test_windows_selector_loop_factory_is_psycopg_compatible() -> None:
    loop = windows_selector_loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_uvicorn_can_resolve_windows_selector_loop_factory() -> None:
    config = Config(
        "service:app",
        loop="run_service:windows_selector_loop_factory",
    )

    loop_factory = config.get_loop_factory()

    assert loop_factory is not None
    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
