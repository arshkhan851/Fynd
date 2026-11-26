from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.database import get_db
from app.discovery.registry import RegistryError
from app.main import app
from app.review import router as review_router
from app.review import service as review_service
from app.review.service import ReviewError


@pytest.fixture
def admin_api_client():
    previous_overrides = app.dependency_overrides.copy()
    admin = User(
        id=uuid4(),
        email=f"review-api-admin-{uuid4().hex}@example.test",
        is_active=True,
        is_admin=True,
    )
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        with TestClient(app) as client:
            yield client, admin
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def test_admin_can_list_pending_evidence_with_existing_filters(monkeypatch, admin_api_client):
    client, _admin = admin_api_client
    observed = {}
    item = {
        "evidence_id": 12,
        "competition_id": 5,
        "canonical_title": "Canonical title",
        "field_name": "category",
        "field_key": "ai",
        "category_name": "AI",
        "category_slug": "ai",
        "observed_value": {"name": "AI"},
        "evidence_excerpt": "AI event",
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_url": "https://events.example.test/ai",
        "publisher": "Example Events",
    }

    def list_evidence(session, *, limit, field_name):
        observed.update(session=session, limit=limit, field_name=field_name)
        return [item]

    monkeypatch.setattr(review_router, "list_pending_evidence", list_evidence)

    response = client.get("/api/v1/admin/evidence?limit=25&field=category")

    assert response.status_code == 200
    assert response.json() == {
        "items": [{**item, "observed_at": "2026-01-01T00:00:00Z"}],
        "limit": 25,
        "field": "category",
    }
    assert observed["limit"] == 25
    assert observed["field_name"] == "category"


def test_admin_can_inspect_evidence_context_and_audit(monkeypatch, admin_api_client):
    client, _admin = admin_api_client
    detail = {
        "evidence": {
            "id": 12,
            "field_name": "title",
            "field_key": "",
            "category_name": None,
            "category_slug": None,
            "observed_value": {"title": "Candidate title"},
            "evidence_excerpt": "Candidate title",
            "confidence": None,
            "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "review_state": "candidate",
            "reviewed_at": None,
        },
        "competition": {"id": 5, "title": "Canonical title", "is_published": False},
        "source": {
            "id": 9,
            "source_url": "https://events.example.test/event",
            "registry_source_id": "trusted-source",
        },
        "review_events": [
            {
                "id": uuid4(),
                "reviewer_user_id": uuid4(),
                "action": "accepted",
                "previous_state": "candidate",
                "new_state": "selected",
                "created_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
        ],
    }
    monkeypatch.setattr(review_router, "get_evidence_detail", lambda _db, _id: detail)

    response = client.get("/api/v1/admin/evidence/12")

    assert response.status_code == 200
    assert response.json()["evidence"]["review_state"] == "candidate"
    assert response.json()["competition"]["is_published"] is False
    assert response.json()["source"]["registry_source_id"] == "trusted-source"
    assert response.json()["review_events"][0]["action"] == "accepted"


def test_evidence_detail_service_combines_context_without_database_access(monkeypatch):
    evidence = SimpleNamespace(id=12, competition_id=5, source_id=9)
    event = SimpleNamespace(
        id=uuid4(),
        reviewer_user_id=uuid4(),
        action="accepted",
        previous_state="candidate",
        new_state="selected",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    class FakeSession:
        def get(self, model, identity):
            assert identity == 12
            return evidence

        def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [event])

    monkeypatch.setattr(
        review_service,
        "get_competition_detail",
        lambda _session, _competition_id: {
            "competition": {"id": 5, "title": "Canonical title"},
            "sources": [
                {
                    "id": 9,
                    "source_url": "https://events.example.test/event",
                    "registry_source_id": "trusted-source",
                    "evidence": [{"id": 12, "review_state": "selected"}],
                }
            ],
        },
    )

    detail = review_service.get_evidence_detail(FakeSession(), 12)

    assert detail["evidence"] == {"id": 12, "review_state": "selected"}
    assert detail["competition"] == {"id": 5, "title": "Canonical title"}
    assert detail["source"] == {
        "id": 9,
        "source_url": "https://events.example.test/event",
        "registry_source_id": "trusted-source",
    }
    assert detail["review_events"][0]["reviewer_user_id"] == event.reviewer_user_id


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/admin/evidence"),
        ("get", "/api/v1/admin/evidence/12"),
        ("post", "/api/v1/admin/evidence/12/accept"),
        ("post", "/api/v1/admin/evidence/12/reject"),
    ],
)
@pytest.mark.parametrize("access,status_code", [("anonymous", 401), ("non_admin", 403)])
def test_evidence_review_endpoints_require_an_active_admin(
    admin_api_client, method, path, access, status_code
):
    client, _admin = admin_api_client
    if access == "anonymous":
        def anonymous_user():
            raise HTTPException(status_code=401, detail="Authentication required")

        app.dependency_overrides[get_current_user] = anonymous_user
    else:
        non_admin = User(
            id=uuid4(),
            email=f"review-api-user-{uuid4().hex}@example.test",
            is_active=True,
            is_admin=False,
        )
        app.dependency_overrides[get_current_user] = lambda: non_admin

    response = getattr(client, method)(path)

    assert response.status_code == status_code


@pytest.mark.parametrize("action,state", [("accept", "selected"), ("reject", "rejected")])
def test_review_mutations_use_authenticated_reviewer_not_request_data(
    monkeypatch, admin_api_client, action, state
):
    client, admin = admin_api_client
    received = {}
    evidence = SimpleNamespace(
        id=12,
        competition_id=5,
        field_name="title",
        field_key="",
        review_state=state,
        reviewed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    def mutate(session, evidence_id, *, reviewer):
        received.update(session=session, evidence_id=evidence_id, reviewer=reviewer)
        return evidence

    monkeypatch.setattr(review_router, f"{action}_evidence", mutate)

    response = client.post(
        f"/api/v1/admin/evidence/12/{action}",
        json={"reviewer_id": str(uuid4())},
    )

    assert response.status_code == 200
    assert response.json()["review_state"] == state
    assert received["evidence_id"] == 12
    assert received["reviewer"] is admin


@pytest.mark.parametrize(
    "action,error,status_code",
    [
        ("accept", "evidence 12 was not found", 404),
        ("reject", "evidence 12 is already selected", 409),
        ("accept", "source source-1 is not registered", 403),
        ("accept", "evidence has an invalid canonical value", 422),
    ],
)
def test_review_mutations_map_service_errors(
    monkeypatch, admin_api_client, action, error, status_code
):
    client, _admin = admin_api_client

    def fail(*_args, **_kwargs):
        raise ReviewError(error)

    monkeypatch.setattr(review_router, f"{action}_evidence", fail)

    response = client.post(f"/api/v1/admin/evidence/12/{action}")

    assert response.status_code == status_code
    assert response.json()["detail"] == error


@pytest.mark.parametrize(
    "registry_error,status_code",
    [
        ("cannot load registry file", 503),
        ("registry validation failed closed due to malformed data", 503),
        ("source source-1 permission must be a table", 503),
        ("source source-1 is not registered", 403),
        ("source source-1 internal_review refused: permission denied", 403),
    ],
)
def test_registry_errors_map_to_server_or_authorization_status(
    monkeypatch, admin_api_client, registry_error, status_code
):
    client, _admin = admin_api_client

    def fail(*_args, **_kwargs):
        cause = RegistryError(registry_error)
        raise ReviewError(str(cause)) from cause

    monkeypatch.setattr(review_router, "accept_evidence", fail)

    response = client.post("/api/v1/admin/evidence/12/accept")

    assert response.status_code == status_code
    if status_code == 503:
        assert response.json()["detail"] == "Evidence review authorization registry is unavailable"
    else:
        assert response.json()["detail"] == registry_error
