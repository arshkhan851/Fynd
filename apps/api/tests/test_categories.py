from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app


def test_categories_response_includes_canonical_numeric_id(monkeypatch):
    class FakeResult:
        def all(self):
            return [
                SimpleNamespace(
                    _mapping={
                        "id": 41,
                        "name": "AI & Machine Learning",
                        "slug": "ai-machine-learning",
                        "competition_count": 3,
                    }
                )
            ]

    class FakeSession:
        def execute(self, statement):
            return FakeResult()

    monkeypatch.setitem(app.dependency_overrides, get_db, FakeSession)
    try:
        response = TestClient(app).get("/categories")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 41,
            "name": "AI & Machine Learning",
            "slug": "ai-machine-learning",
            "competition_count": 3,
        }
    ]
