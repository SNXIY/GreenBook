from fastapi.testclient import TestClient

from service import app


def test_api_root_does_not_expose_a_standalone_console() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 404


def test_openapi_only_exposes_real_moderation_endpoints() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["paths"]
    assert all(path.startswith("/moderation/") for path in schema["paths"])
    assert not any(path.startswith("/community/") for path in schema["paths"])
