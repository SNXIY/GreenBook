import os
import tempfile
from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture(autouse=True)
def _default_sync_moderation(monkeypatch):
    """Keep existing workflow/community tests on the synchronous create_task path."""
    from core import settings

    monkeypatch.setattr(settings, "MODERATION_ASYNC_ENABLED", False)
    monkeypatch.setattr(settings, "MODERATION_EMBEDDED_WORKER_ENABLED", False)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean for each test."""
    # Some SDKs and pathlib still need a resolvable home directory on Windows even
    # when application settings are intentionally isolated from the host environment.
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or tempfile.gettempdir()
    with patch.dict(
        os.environ,
        {"HOME": home, "USERPROFILE": home},
        clear=True,
    ):
        yield
