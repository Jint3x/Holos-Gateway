from fastapi.testclient import TestClient

from app.main import app


def test_hello_world() -> None:
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"message": "Hello World"}
