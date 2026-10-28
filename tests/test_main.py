import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.observability import Telemetry, TelemetrySettings


@pytest.fixture
def client():
    with TestClient(
        create_app(telemetry=Telemetry(TelemetrySettings()))
    ) as test_client:
        yield test_client


def test_hello_world(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"message": "Hello World"}


def test_personalized_greeting(client) -> None:
    response = client.get("/", params={"name": " Ada "})

    assert response.status_code == 200
    assert response.json() == {"message": "Hello Ada"}


def test_whitespace_name_returns_documented_error(client) -> None:
    response = client.get("/", params={"name": "   "})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Name must contain a non-whitespace character."
    }


@pytest.mark.parametrize("name", ["", "a" * 81])
def test_invalid_name_returns_validation_error(client, name) -> None:
    response = client.get("/", params={"name": name})

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["query", "name"]
    assert error["type"] == ("string_too_short" if not name else "string_too_long")


def test_maximum_length_name_is_accepted(client) -> None:
    response = client.get("/", params={"name": "a" * 80})

    assert response.status_code == 200
    assert response.json() == {"message": "Hello " + "a" * 80}


def test_health_and_readiness_follow_worker_lifecycle():
    app = create_app(telemetry=Telemetry(TelemetrySettings()))
    assert app.state.ready is False
    with TestClient(app) as client:
        for path, status in [("/healthz", "ok"), ("/readyz", "ready")]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == {"status": status}
        app.state.ready = False
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert client.get("/healthz").status_code == 200
    assert app.state.ready is False


def test_openapi_matches_exported_schema(client) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]["/readyz"]["get"]["responses"]) == {"200", "503"}
    assert set(schema["paths"]["/healthz"]["get"]["responses"]) == {"200"}
    exported = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    assert schema == json.loads(exported.read_text(encoding="utf-8"))
    responses = schema["paths"]["/"]["get"]["responses"]
    assert set(responses) == {"200", "400", "422"}
    for code, model in [
        ("200", "GreetingResponse"),
        ("400", "ErrorResponse"),
        ("422", "HTTPValidationError"),
    ]:
        assert responses[code]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{model}"
        }
