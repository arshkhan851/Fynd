from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.models import User, UserSession
from app.auth.service import _hash_token
from app.database import SessionLocal, engine, get_db
from app.main import app
from app.models import (
    Category,
    Competition,
    OrganizerSubmission,
    SubmissionConsent,
    SubmissionReviewEvent,
)
from app.submissions.consent import (
    CURRENT_CONSENT_DECLARATION_TEXT,
    CURRENT_CONSENT_DECLARATION_VERSION,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def submission_api_db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _identity(db: Session, email: str, *, is_admin: bool = False) -> tuple[User, str]:
    user = User(id=uuid4(), email=email, is_active=True, is_admin=is_admin)
    raw_token = uuid4().hex + uuid4().hex
    db.add(user)
    db.flush()
    db.add(
        UserSession(
            id=uuid4(),
            user_id=user.id,
            session_token_hash=_hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            is_revoked=False,
        )
    )
    db.commit()
    return user, raw_token


def _payload(**overrides) -> dict:
    payload = {
        "title": "Organizer API Test Hackathon",
        "description": "A test event submitted by an organizer.",
        "organizer_name": "Test Organizer",
        "organizer_email": "organizer@example.org",
        "organizer_contact_email": "private@example.org",
        "organizer_contact_phone": "+91 1111111111",
        "official_url": "https://events.example.org/hackathon",
        "registration_url": "https://events.example.org/register",
        "registration_deadline_on": "2027-05-01",
        "event_starts_on": "2027-05-10",
        "event_ends_on": "2027-05-12",
        "format": "online",
        "registration_price_status": "free",
        "category_ids": [],
        "primary_category_id": None,
        "consent": {
            "declaration_version": CURRENT_CONSENT_DECLARATION_VERSION,
            "declaration_text": CURRENT_CONSENT_DECLARATION_TEXT,
        },
    }
    payload.update(overrides)
    return payload


def _authenticated(
    client: TestClient,
    db: Session,
    email: str = "owner@example.org",
    *,
    is_admin: bool = False,
) -> User:
    user, token = _identity(db, email, is_admin=is_admin)
    client.cookies.set("fynd_session", token)
    return user


def test_submission_endpoints_require_authentication(submission_api_db, client):
    response = client.post("/api/v1/submissions", json=_payload())
    assert response.status_code == 401
    assert client.get("/api/v1/submissions/me").status_code == 401


def test_submission_json_endpoint_allows_configured_credentialed_cors_preflight(client):
    response = client.options(
        "/api/v1/submissions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_authenticated_submission_is_created_without_leaking_private_fields(
    submission_api_db, client
):
    db = submission_api_db
    user = _authenticated(client, db)
    first_category = Category(name="API AI", slug=f"api-ai-{uuid4().hex}")
    second_category = Category(name="API Web", slug=f"api-web-{uuid4().hex}")
    db.add_all([first_category, second_category])
    db.commit()
    competition_count = db.scalar(select(func.count()).select_from(Competition))

    response = client.post(
        "/api/v1/submissions",
        json=_payload(
            category_ids=[first_category.id, second_category.id],
            primary_category_id=second_category.id,
        ),
    )

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "pending"
    assert result["title"] == "Organizer API Test Hackathon"
    assert {item["id"] for item in result["categories"]} == {
        first_category.id,
        second_category.id,
    }
    assert next(item for item in result["categories"] if item["is_primary"])["id"] == (
        second_category.id
    )
    assert "private@example.org" not in response.text
    assert "declaration_text" not in result
    assert "declaration_sha256" not in result
    assert "reviewer" not in result

    saved = db.get(OrganizerSubmission, result["id"])
    assert saved.submitter_user_id == user.id
    assert db.scalar(select(func.count()).select_from(Competition)) == competition_count
    assert (
        client.get("/competitions", params={"q": "Organizer API Test Hackathon"}).json()["items"]
        == []
    )


def test_modified_or_outdated_consent_is_rejected(submission_api_db, client):
    _authenticated(client, submission_api_db)
    payload = _payload(
        consent={
            "declaration_version": "old-version",
            "declaration_text": CURRENT_CONSENT_DECLARATION_TEXT,
        }
    )
    response = client.post("/api/v1/submissions", json=payload)
    assert response.status_code == 422
    assert submission_api_db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0

    payload["consent"] = {
        "declaration_version": CURRENT_CONSENT_DECLARATION_VERSION,
        "declaration_text": f"{CURRENT_CONSENT_DECLARATION_TEXT} Modified",
    }
    response = client.post("/api/v1/submissions", json=payload)
    assert response.status_code == 422
    assert submission_api_db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0


def test_owner_can_read_submission_but_other_users_receive_not_found(submission_api_db, client):
    db = submission_api_db
    owner = _authenticated(client, db, "owner-read@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    assert client.get(f"/api/v1/submissions/{submission_id}").status_code == 200

    client.cookies.clear()
    _authenticated(client, db, "other-read@example.org")
    other_response = client.get(f"/api/v1/submissions/{submission_id}")
    assert other_response.status_code == 404
    assert other_response.json()["detail"] == "Submission not found"
    assert db.get(OrganizerSubmission, submission_id).submitter_user_id == owner.id


@pytest.mark.parametrize("starting_status", ["pending", "under_review"])
def test_owner_can_withdraw_and_transition_is_not_recorded_as_admin_review(
    submission_api_db, client, starting_status
):
    db = submission_api_db
    _authenticated(client, db, "withdraw-owner@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    db.get(OrganizerSubmission, submission_id).status = starting_status
    db.commit()

    response = client.post(f"/api/v1/submissions/{submission_id}/withdraw")

    assert response.status_code == 200
    assert response.json()["status"] == "withdrawn"
    assert (
        db.scalar(
            select(func.count())
            .select_from(SubmissionReviewEvent)
            .where(SubmissionReviewEvent.submission_id == submission_id)
        )
        == 0
    )
    repeated = client.post(f"/api/v1/submissions/{submission_id}/withdraw")
    assert repeated.status_code == 409


def test_user_cannot_withdraw_another_users_submission(submission_api_db, client):
    db = submission_api_db
    _authenticated(client, db, "withdraw-owner2@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]

    client.cookies.clear()
    _authenticated(client, db, "withdraw-other@example.org")
    response = client.post(f"/api/v1/submissions/{submission_id}/withdraw")
    assert response.status_code == 404
    assert db.get(OrganizerSubmission, submission_id).status == "pending"


@pytest.mark.parametrize("terminal_status", ["approved", "rejected", "withdrawn"])
def test_terminal_submissions_cannot_be_withdrawn(submission_api_db, client, terminal_status):
    db = submission_api_db
    _authenticated(client, db, f"terminal-{terminal_status}@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    submission = db.get(OrganizerSubmission, submission_id)
    submission.status = terminal_status
    db.commit()

    response = client.post(f"/api/v1/submissions/{submission_id}/withdraw")
    assert response.status_code == 409
    assert db.get(OrganizerSubmission, submission_id).status == terminal_status


def test_my_submissions_are_owner_scoped_and_paginated(submission_api_db, client):
    db = submission_api_db
    owner = _authenticated(client, db, "pagination-owner@example.org")
    owner_token = client.cookies.get("fynd_session")
    for index in range(3):
        response = client.post(
            "/api/v1/submissions",
            json=_payload(title=f"Pagination Event {index}"),
        )
        assert response.status_code == 201

    client.cookies.clear()
    other = _authenticated(client, db, "pagination-other@example.org")
    assert other.id != owner.id
    assert (
        client.post("/api/v1/submissions", json=_payload(title="Other user's event")).status_code
        == 201
    )

    client.cookies.clear()
    client.cookies.set("fynd_session", owner_token)
    first_page = client.get("/api/v1/submissions/me", params={"limit": 2, "offset": 0})
    last_page = client.get("/api/v1/submissions/me", params={"limit": 2, "offset": 2})
    assert first_page.status_code == last_page.status_code == 200
    assert first_page.json()["total"] == last_page.json()["total"] == 3
    assert len(first_page.json()["items"]) == 2
    assert len(last_page.json()["items"]) == 1
    assert all("Other user's event" not in str(item) for item in first_page.json()["items"])
    assert all("Other user's event" not in str(item) for item in last_page.json()["items"])
    assert {item["id"] for item in first_page.json()["items"]}.isdisjoint(
        {last_page.json()["items"][0]["id"]}
    )


def test_admin_endpoints_require_admin(submission_api_db, client):
    assert client.get("/api/v1/admin/submissions").status_code == 401
    assert client.get("/api/v1/admin/submissions/99999999").status_code == 401
    assert client.post("/api/v1/admin/submissions/99999999/start-review").status_code == 401
    assert (
        client.post(
            "/api/v1/admin/submissions/99999999/reject", json={"reason": "A clear reason"}
        ).status_code
        == 401
    )

    _authenticated(client, submission_api_db, "not-admin@example.org")
    assert client.get("/api/v1/admin/submissions").status_code == 403
    assert client.get("/api/v1/admin/submissions/99999999").status_code == 403
    assert client.post("/api/v1/admin/submissions/99999999/start-review").status_code == 403
    assert (
        client.post(
            "/api/v1/admin/submissions/99999999/reject", json={"reason": "A clear reason"}
        ).status_code
        == 403
    )


def test_admin_queue_filters_paginates_and_orders_by_oldest(submission_api_db, client):
    db = submission_api_db
    _authenticated(client, db, "queue-owner@example.org")
    submission_ids = []
    for index in range(4):
        response = client.post(
            "/api/v1/submissions", json=_payload(title=f"Admin Queue Event {index}")
        )
        assert response.status_code == 201
        submission_ids.append(response.json()["id"])

    db.get(OrganizerSubmission, submission_ids[2]).status = "under_review"
    db.get(OrganizerSubmission, submission_ids[3]).status = "rejected"
    db.commit()
    client.cookies.clear()
    admin = _authenticated(client, db, "queue-admin@example.org", is_admin=True)

    default_queue = client.get("/api/v1/admin/submissions", params={"limit": 100})
    assert default_queue.status_code == 200
    default_ids = {item["id"] for item in default_queue.json()["items"]}
    assert set(submission_ids[:3]) <= default_ids
    assert submission_ids[3] not in default_ids

    pending = client.get("/api/v1/admin/submissions", params={"status": "pending"})
    assert pending.status_code == 200
    pending_ids = {item["id"] for item in pending.json()["items"]}
    assert submission_ids[0] in pending_ids
    assert submission_ids[2] not in pending_ids

    ordered = client.get("/api/v1/admin/submissions", params={"status": "pending", "limit": 1})
    second_page = client.get(
        "/api/v1/admin/submissions",
        params={"status": "pending", "limit": 1, "offset": 1},
    )
    assert ordered.json()["total"] == pending.json()["total"]
    assert len(ordered.json()["items"]) == len(second_page.json()["items"]) == 1
    first_item = ordered.json()["items"][0]
    next_item = second_page.json()["items"][0]
    assert (first_item["created_at"], first_item["id"]) <= (
        next_item["created_at"],
        next_item["id"],
    )
    assert first_item["submitter_email"] == "queue-owner@example.org"
    assert first_item["review_events"] == []
    assert admin.is_admin

    invalid_filter = client.get("/api/v1/admin/submissions", params={"status": "queued"})
    assert invalid_filter.status_code == 422


def test_admin_detail_exposes_review_fields_but_never_consent_hash(submission_api_db, client):
    db = submission_api_db
    _authenticated(client, db, "detail-owner@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    client.cookies.clear()
    _authenticated(client, db, "detail-admin@example.org", is_admin=True)

    response = client.get(f"/api/v1/admin/submissions/{submission_id}")

    assert response.status_code == 200
    result = response.json()
    assert result["organizer_email"] == "organizer@example.org"
    assert result["organizer_contact_email"] == "private@example.org"
    assert result["consent"]["declaration_version"] == CURRENT_CONSENT_DECLARATION_VERSION
    assert result["consent"]["declaration_text"] == CURRENT_CONSENT_DECLARATION_TEXT
    assert "accepted_at" in result["consent"]
    assert "declaration_sha256" not in response.text
    assert result["review_events"] == []


def test_admin_start_review_records_exact_reviewer_and_rejects_invalid_transition(
    submission_api_db, client
):
    db = submission_api_db
    _authenticated(client, db, "start-owner@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    client.cookies.clear()
    admin = _authenticated(client, db, "start-admin@example.org", is_admin=True)

    response = client.post(f"/api/v1/admin/submissions/{submission_id}/start-review")
    assert response.status_code == 200
    assert response.json()["status"] == "under_review"
    events = db.scalars(
        select(SubmissionReviewEvent).where(SubmissionReviewEvent.submission_id == submission_id)
    ).all()
    assert len(events) == 1
    assert events[0].reviewer_user_id == admin.id
    assert events[0].previous_status == "pending"
    assert events[0].new_status == "under_review"

    repeated = client.post(f"/api/v1/admin/submissions/{submission_id}/start-review")
    assert repeated.status_code == 409
    assert (
        db.scalar(
            select(func.count())
            .select_from(SubmissionReviewEvent)
            .where(SubmissionReviewEvent.submission_id == submission_id)
        )
        == 1
    )


def test_admin_reject_requires_reason_and_records_reviewer_and_transition(
    submission_api_db, client
):
    db = submission_api_db
    _authenticated(client, db, "reject-owner@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    db.get(OrganizerSubmission, submission_id).status = "under_review"
    db.commit()
    client.cookies.clear()
    admin = _authenticated(client, db, "reject-admin@example.org", is_admin=True)

    assert (
        client.post(
            f"/api/v1/admin/submissions/{submission_id}/reject", json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/admin/submissions/{submission_id}/reject", json={"reason": "          "}
        ).status_code
        == 422
    )
    assert db.get(OrganizerSubmission, submission_id).status == "under_review"

    response = client.post(
        f"/api/v1/admin/submissions/{submission_id}/reject",
        json={"reason": "The submitted event dates are inconsistent."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    events = db.scalars(
        select(SubmissionReviewEvent)
        .where(SubmissionReviewEvent.submission_id == submission_id)
        .order_by(SubmissionReviewEvent.id)
    ).all()
    assert len(events) == 1
    assert events[0].reviewer_user_id == admin.id
    assert events[0].previous_status == "under_review"
    assert events[0].new_status == "rejected"
    assert events[0].note == "The submitted event dates are inconsistent."

    repeated = client.post(
        f"/api/v1/admin/submissions/{submission_id}/reject",
        json={"reason": "Another valid reason."},
    )
    assert repeated.status_code == 409
    assert len(events) == 1


def test_rejecting_pending_submission_is_invalid_transition(submission_api_db, client):
    db = submission_api_db
    _authenticated(client, db, "pending-reject-owner@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    client.cookies.clear()
    _authenticated(client, db, "pending-reject-admin@example.org", is_admin=True)

    response = client.post(
        f"/api/v1/admin/submissions/{submission_id}/reject",
        json={"reason": "A meaningful reason."},
    )

    assert response.status_code == 409
    assert db.get(OrganizerSubmission, submission_id).status == "pending"
    assert (
        db.scalar(
            select(func.count())
            .select_from(SubmissionReviewEvent)
            .where(SubmissionReviewEvent.submission_id == submission_id)
        )
        == 0
    )


@pytest.mark.parametrize("action", ["start-review", "reject"])
def test_admin_transition_rolls_back_status_and_audit_event_on_flush_failure(
    submission_api_db, client, monkeypatch, action
):
    db = submission_api_db
    _authenticated(client, db, f"rollback-owner-{action}@example.org")
    created = client.post("/api/v1/submissions", json=_payload())
    submission_id = created.json()["id"]
    submission = db.get(OrganizerSubmission, submission_id)
    if action == "reject":
        submission.status = "under_review"
        db.commit()
    db.expire(submission)
    client.cookies.clear()
    _authenticated(client, db, f"rollback-admin-{action}@example.org", is_admin=True)
    admin_token = client.cookies.get("fynd_session")
    original_flush = db.flush

    def fail_flush(*args, **kwargs):
        raise RuntimeError("simulated audit insert failure")

    monkeypatch.setattr(db, "flush", fail_flush)
    rollback_client = TestClient(app, raise_server_exceptions=False)
    rollback_client.cookies.set("fynd_session", admin_token)
    try:
        response = rollback_client.post(
            f"/api/v1/admin/submissions/{submission_id}/{action}",
            json={"reason": "A meaningful rejection reason."} if action == "reject" else None,
        )
    finally:
        monkeypatch.setattr(db, "flush", original_flush)

    assert response.status_code == 500
    db.expire_all()
    assert db.get(OrganizerSubmission, submission_id).status == (
        "under_review" if action == "reject" else "pending"
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(SubmissionReviewEvent)
            .where(SubmissionReviewEvent.submission_id == submission_id)
        )
        == 0
    )


def test_concurrent_start_review_attempts_create_one_event(monkeypatch):
    from app.database import get_db

    email_suffix = uuid4().hex
    factory = SessionLocal
    admin_id = uuid4()
    submitter_id = uuid4()
    admin_token = uuid4().hex + uuid4().hex
    submitter_token = uuid4().hex + uuid4().hex
    submission_id = None
    with factory() as db:
        admin = User(
            id=admin_id, email=f"concurrent-admin-{email_suffix}@example.org", is_admin=True
        )
        submitter = User(id=submitter_id, email=f"concurrent-owner-{email_suffix}@example.org")
        db.add_all([admin, submitter])
        db.flush()
        db.add_all(
            [
                UserSession(
                    id=uuid4(),
                    user_id=admin_id,
                    session_token_hash=_hash_token(admin_token),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    is_revoked=False,
                ),
                UserSession(
                    id=uuid4(),
                    user_id=submitter_id,
                    session_token_hash=_hash_token(submitter_token),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    is_revoked=False,
                ),
            ]
        )
        db.commit()
        from app.submissions.service import SubmittedEvent, create_submission

        submission = create_submission(
            db,
            user=submitter,
            event=SubmittedEvent(
                title=f"Concurrent review {email_suffix}",
                description="Concurrency test submission.",
                organizer_name="Concurrency Test",
                organizer_email="concurrency@example.org",
                official_url="https://events.example.org/concurrent",
                registration_url="https://events.example.org/concurrent/register",
                registration_deadline_on=datetime(2027, 5, 1).date(),
                event_starts_on=datetime(2027, 5, 10).date(),
                event_ends_on=datetime(2027, 5, 12).date(),
                format="online",
                registration_price_status="free",
            ),
            organizer_profile_id=None,
            category_ids=[],
            primary_category_id=None,
            declaration_version=CURRENT_CONSENT_DECLARATION_VERSION,
            declaration_text=CURRENT_CONSENT_DECLARATION_TEXT,
        )
        submission_id = submission.id

    original_override = app.dependency_overrides.get(get_db)

    def session_dependency():
        with factory() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, session_dependency)
    barrier = Barrier(3)

    def attempt():
        client = TestClient(app)
        client.cookies.set("fynd_session", admin_token)
        barrier.wait()
        return client.post(f"/api/v1/admin/submissions/{submission_id}/start-review")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt) for _ in range(2)]
            barrier.wait()
            responses = [future.result(timeout=20) for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        with factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(SubmissionReviewEvent)
                    .where(SubmissionReviewEvent.submission_id == submission_id)
                )
                == 1
            )
            assert db.get(OrganizerSubmission, submission_id).status == "under_review"
    finally:
        if original_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = original_override
        with factory() as db:
            db.execute(
                delete(SubmissionReviewEvent).where(
                    SubmissionReviewEvent.submission_id == submission_id
                )
            )
            db.execute(
                delete(SubmissionConsent).where(SubmissionConsent.submission_id == submission_id)
            )
            db.execute(delete(OrganizerSubmission).where(OrganizerSubmission.id == submission_id))
            db.execute(delete(UserSession).where(UserSession.user_id.in_([admin_id, submitter_id])))
            db.execute(delete(User).where(User.id.in_([admin_id, submitter_id])))
            db.commit()
