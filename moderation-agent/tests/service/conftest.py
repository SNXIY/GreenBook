from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from service import app


@pytest.fixture
def test_client():
    """Create a client without starting external infrastructure."""
    return TestClient(app)


@pytest.fixture
def mock_settings(mock_env):
    """Patch service settings for authentication tests."""
    with patch("service.service.settings") as patched_settings:
        yield patched_settings
