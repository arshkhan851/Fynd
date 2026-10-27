from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)


def test_db_health_returns_ok_when_database_is_available(monkeypatch):
    monkeypatch.setattr(main, "check_database_connectivity", lambda: None)

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_db_health_hides_database_errors(monkeypatch):
    def fail_with_internal_error():
        raise RuntimeError("password=do-not-return-this")

    monkeypatch.setattr(main, "check_database_connectivity", fail_with_internal_error)

    response = client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {"detail": {"status": "unavailable"}}
    assert "do-not-return-this" not in response.text


def test_basic_health_does_not_depend_on_database(monkeypatch):
    def fail_with_internal_error():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "check_database_connectivity", fail_with_internal_error)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
