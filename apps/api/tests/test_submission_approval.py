from collections.abc import Generator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.auth.models import User, UserSession
from app.auth.service import _hash_token
from app.database import engine, get_db
from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS
from app.main import app
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionOrganizer,
    CompetitionPrize,
    CompetitionSource,
    EvidenceReviewEvent,
    FieldEvidence,
    Organizer,
    OrganizerSubmission,
    OrganizerSubmissionCategory,
    SubmissionConsent,
    SubmissionReviewEvent,
)
from app.review.service import ReviewError
from app.submissions.consent import (
    CURRENT_CONSENT_DECLARATION_TEXT,
    CURRENT_CONSENT_DECLARATION_VERSION,
)
from app.submissions.service import (
    SubmissionValidationError,
    _normalize_provenance_url,
    _validate_existing_competition_sources,
)

pytestmark = pytest.mark.integration


def _assert_approval_test_database(database_name: str | None) -> None:
    if database_name != "fynd_test":
        raise RuntimeError(
            "submission approval integration tests require database name 'fynd_test'"
        )


@pytest.fixture(scope="module", autouse=True)
def _guard_approval_test_database() -> None:
    # engine.url is configuration metadata; inspecting it does not connect.
    _assert_approval_test_database(engine.url.database)


@pytest.mark.parametrize("database_name", [None, "", "fynd", "postgres", "fynd_test "])
def test_approval_database_guard_rejects_unsafe_database_names(database_name):
    with pytest.raises(RuntimeError, match="require database name 'fynd_test'"):
        _assert_approval_test_database(database_name)


def test_approval_database_guard_accepts_only_fynd_test():
    assert _assert_approval_test_database("fynd_test") is None


@pytest.fixture
def approval_db() -> Generator[Session, None, None]:
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


def _login(client: TestClient, db: Session, *, admin: bool, email: str) -> User:
    user = User(id=uuid4(), email=email, is_admin=admin, is_active=True)
    token = uuid4().hex + uuid4().hex
    db.add(user)
    db.flush()
    db.add(
        UserSession(
            id=uuid4(),
            user_id=user.id,
            session_token_hash=_hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            is_revoked=False,
        )
    )
    db.commit()
    client.cookies.set("fynd_session", token)
    return user


def _submission_payload(title: str | None = None, category_ids: list[int] | None = None) -> dict:
    selected_ids = category_ids or []
    return {
        "title": title or f"Approval Test Hackathon {uuid4().hex[:8]}",
        "description": "Create a tested organizer-submitted event.",
        "organizer_name": "Approval Test Organizer",
        "organizer_email": "organizer@example.org",
        "organizer_contact_name": "Event Contact",
        "organizer_contact_email": "contact@example.org",
        "organizer_contact_phone": "+91 9999999999",
        "official_url": "https://events.example.org/hackathon",
        "registration_url": "https://events.example.org/hackathon/register",
        "registration_deadline_on": "2027-06-01",
        "event_starts_on": "2027-06-10",
        "event_ends_on": "2027-06-12",
        "event_timezone": "Asia/Kolkata",
        "format": "online",
        "location_name": "Online",
        "location_country_code": "IN",
        "registration_price_status": "paid",
        "registration_fee_amount": "150.00",
        "registration_fee_currency": "INR",
        "category_ids": selected_ids,
        "primary_category_id": selected_ids[0] if selected_ids else None,
        "consent": {
            "declaration_version": CURRENT_CONSENT_DECLARATION_VERSION,
            "declaration_text": CURRENT_CONSENT_DECLARATION_TEXT,
        },
    }


def _make_under_review(
    client: TestClient,
    db: Session,
    *,
    email: str = "approval-owner@example.org",
    title: str | None = None,
    categories: bool = False,
) -> tuple[int, list[Category]]:
    _login(client, db, admin=False, email=email)
    selected_categories = []
    if categories:
        selected_categories = [
            Category(name=f"Approval AI {uuid4().hex}", slug=f"approval-ai-{uuid4().hex}")
        ]
        db.add_all(selected_categories)
        db.commit()
    created = client.post(
        "/api/v1/submissions",
        json=_submission_payload(
            title=title,
            category_ids=[category.id for category in selected_categories],
        ),
    )
    assert created.status_code == 201, created.text
    submission_id = created.json()["id"]
    db.get(OrganizerSubmission, submission_id).status = "under_review"
    db.commit()
    return submission_id, selected_categories


def _admin_login(client: TestClient, db: Session, suffix: str = "admin") -> User:
    client.cookies.clear()
    return _login(client, db, admin=True, email=f"{suffix}-{uuid4().hex}@example.org")


def _counts(db: Session) -> dict[str, int]:
    return {
        "competitions": db.scalar(select(func.count()).select_from(Competition)),
        "submissions": db.scalar(select(func.count()).select_from(OrganizerSubmission)),
        "consents": db.scalar(select(func.count()).select_from(SubmissionConsent)),
        "submission_categories": db.scalar(
            select(func.count()).select_from(OrganizerSubmissionCategory)
        ),
        "categories": db.scalar(select(func.count()).select_from(Category)),
        "sources": db.scalar(select(func.count()).select_from(CompetitionSource)),
        "evidence": db.scalar(select(func.count()).select_from(FieldEvidence)),
        "organizers": db.scalar(select(func.count()).select_from(Organizer)),
        "competition_organizers": db.scalar(select(func.count()).select_from(CompetitionOrganizer)),
        "competition_categories": db.scalar(select(func.count()).select_from(CompetitionCategory)),
        "prizes": db.scalar(select(func.count()).select_from(CompetitionPrize)),
        "review_events": db.scalar(select(func.count()).select_from(SubmissionReviewEvent)),
        "evidence_review_events": db.scalar(
            select(func.count()).select_from(EvidenceReviewEvent)
        ),
    }


def _approval_state_snapshot(db: Session, submission_id: int) -> dict[str, object]:
    def rows(model, order_by, *columns):
        instances = db.scalars(select(model).order_by(*order_by)).all()
        return tuple(
            tuple(deepcopy(getattr(instance, column)) for column in columns)
            for instance in instances
        )

    canonical_fields = (
        "title",
        "description",
        "eligibility_summary",
        "format",
        "official_url",
        "registration_url",
        "registration_opens_at",
        "registration_opens_on",
        "registration_deadline_at",
        "registration_deadline_on",
        "event_starts_at",
        "event_starts_on",
        "event_ends_at",
        "event_ends_on",
        "event_timezone",
        "is_published",
    )
    return {
        "counts": _counts(db),
        "submission": tuple(
            getattr(db.get(OrganizerSubmission, submission_id), field)
            for field in ("status", "approved_competition_id", "updated_at")
        ),
        "competitions": rows(
            Competition,
            (Competition.id,),
            "id",
            *canonical_fields,
        ),
        "sources": rows(
            CompetitionSource,
            (CompetitionSource.id,),
            "id",
            "competition_id",
            "organizer_submission_id",
            "source_kind",
            "source_url",
            "normalized_url",
            "registry_source_id",
        ),
        "evidence": rows(
            FieldEvidence,
            (FieldEvidence.id,),
            "id",
            "competition_id",
            "source_id",
            "field_name",
            "field_key",
            "observed_value",
            "review_state",
            "reviewed_at",
        ),
        "competition_categories": rows(
            CompetitionCategory,
            (CompetitionCategory.competition_id, CompetitionCategory.category_id),
            "competition_id",
            "category_id",
            "is_primary",
        ),
        "competition_organizers": rows(
            CompetitionOrganizer,
            (CompetitionOrganizer.competition_id, CompetitionOrganizer.organizer_id),
            "competition_id",
            "organizer_id",
            "role",
        ),
        "organizers": rows(Organizer, (Organizer.id,), "id", "name"),
        "consents": rows(
            SubmissionConsent,
            (SubmissionConsent.id,),
            "id",
            "submission_id",
            "submitter_user_id",
            "submitter_email_snapshot",
            "declaration_version",
            "declaration_text",
            "declaration_sha256",
            "accepted_at",
        ),
        "evidence_review_events": rows(
            EvidenceReviewEvent,
            (EvidenceReviewEvent.id,),
            "id",
            "evidence_id",
            "reviewer_user_id",
            "action",
            "previous_state",
            "new_state",
            "created_at",
        ),
        "submission_review_events": rows(
            SubmissionReviewEvent,
            (SubmissionReviewEvent.id,),
            "id",
            "submission_id",
            "reviewer_user_id",
            "previous_status",
            "new_status",
            "note",
            "created_at",
        ),
    }


def test_approval_creates_evidence_relations_and_publishes_atomically(approval_db, client):
    db = approval_db
    submission_id, categories = _make_under_review(client, db, categories=True)
    admin = _admin_login(client, db)
    before = _counts(db)

    response = client.post(f"/api/v1/admin/submissions/{submission_id}/approve")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "approved"
    competition_id = result["approved_competition_id"]
    competition = db.get(Competition, competition_id)
    assert competition.is_published is True
    assert competition.title == result["title"]
    assert competition.description == result["description"]
    assert competition.format == "online"
    assert competition.registration_deadline_on.isoformat() == "2027-06-01"
    assert competition.is_published is True
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(
                CompetitionSource.organizer_submission_id == submission_id,
                CompetitionSource.competition_id == competition_id,
            )
        )
        == 1
    )
    source = db.scalar(
        select(CompetitionSource).where(CompetitionSource.organizer_submission_id == submission_id)
    )
    assert source.organizer_submission_id == submission_id
    assert source.registry_source_id is None
    evidence = db.scalars(select(FieldEvidence).where(FieldEvidence.source_id == source.id)).all()
    assert {item.field_name for item in evidence} >= {
        "title",
        "description",
        "format",
        "official_url",
        "registration_url",
        "registration_deadline",
        "event_starts",
        "event_ends",
    }
    assert all(item.field_name in REVIEW_SUPPORTED_EVIDENCE_FIELDS for item in evidence)
    assert all(item.review_state == "selected" for item in evidence)
    evidence_events = db.scalars(
        select(EvidenceReviewEvent).where(
            EvidenceReviewEvent.evidence_id.in_([item.id for item in evidence])
        )
    ).all()
    assert len(evidence_events) == len(evidence)
    assert all(
        (item.action, item.previous_state, item.new_state, item.reviewer_user_id)
        == ("accepted", "candidate", "selected", admin.id)
        for item in evidence_events
    )
    assert all(
        item.field_key == ("" if item.field_name != "category" else item.observed_value["slug"])
        for item in evidence
    )
    category_link = db.get(CompetitionCategory, (competition_id, categories[0].id))
    assert category_link is not None and category_link.is_primary is True
    organizer_link = db.scalar(
        select(CompetitionOrganizer).where(CompetitionOrganizer.competition_id == competition_id)
    )
    assert organizer_link is not None
    assert db.get(Organizer, organizer_link.organizer_id).name == "Approval Test Organizer"
    assert result["consent"]["declaration_text"] == CURRENT_CONSENT_DECLARATION_TEXT
    assert "declaration_sha256" not in response.text
    events = db.scalars(
        select(SubmissionReviewEvent).where(SubmissionReviewEvent.submission_id == submission_id)
    ).all()
    assert len(events) == 1
    assert events[0].reviewer_user_id == admin.id
    assert (events[0].previous_status, events[0].new_status) == (
        "under_review",
        "approved",
    )
    assert client.get(f"/competitions/{competition_id}").status_code == 200
    assert client.get("/competitions", params={"q": result["title"]}).json()["total"] == 1
    assert _counts(db)["competitions"] == before["competitions"] + 1


def test_submission_acceptance_rejects_foreign_source_and_snapshot_evidence(
    approval_db, client
):
    from app.review.service import accept_submission_evidence
    from app.submissions import service

    db = approval_db
    submission_id, _ = _make_under_review(client, db, categories=False)
    other_submission_id, _ = _make_under_review(
        client, db, categories=False, email=f"other-{uuid4().hex}@example.org"
    )
    reviewer = _admin_login(client, db, f"provenance-check-{uuid4().hex}")
    submission = db.get(OrganizerSubmission, submission_id)
    other_submission = db.get(OrganizerSubmission, other_submission_id)
    category_links, categories = service._validate_approval_snapshot(db, submission)
    specs = service._approval_evidence_specs(submission, category_links, categories)
    target_spec = next(spec for spec in specs if spec[0] == "title")
    competition = Competition()
    db.add(competition)
    db.flush()
    target_source = CompetitionSource(
        competition_id=competition.id,
        organizer_submission_id=submission.id,
        source_kind="organizer",
        source_url=submission.official_url,
    )
    foreign_source = CompetitionSource(
        competition_id=competition.id,
        organizer_submission_id=other_submission.id,
        source_kind="organizer",
        source_url=other_submission.official_url,
    )
    db.add_all([target_source, foreign_source])
    db.flush()

    def add_evidence(source, spec):
        field_name, field_key, observed_value, excerpt = spec
        row = FieldEvidence(
            competition_id=competition.id,
            source_id=source.id,
            field_name=field_name,
            field_key=field_key,
            observed_value=observed_value,
            evidence_excerpt=excerpt,
        )
        db.add(row)
        db.flush()
        return row

    foreign_evidence = add_evidence(foreign_source, target_spec)
    target_evidence = add_evidence(target_source, target_spec)

    with pytest.raises(ReviewError, match="not linked to the locked submission"):
        accept_submission_evidence(
            db,
            foreign_evidence.id,
            submission_id=submission.id,
            source_id=foreign_source.id,
            competition_id=competition.id,
            validated_evidence_specs={foreign_evidence.id: target_spec},
            reviewer=reviewer,
        )
    with pytest.raises(ReviewError, match="not part of the validated submission snapshot"):
        accept_submission_evidence(
            db,
            target_evidence.id,
            submission_id=submission.id,
            source_id=target_source.id,
            competition_id=competition.id,
            validated_evidence_specs={},
            reviewer=reviewer,
        )
    with pytest.raises(ReviewError, match="outside the validated submission snapshot"):
        accept_submission_evidence(
            db,
            target_evidence.id,
            submission_id=submission.id,
            source_id=target_source.id,
            competition_id=competition.id,
            validated_evidence_specs={
                target_evidence.id: ("title", "", {"title": "forged"}, "forged")
            },
            reviewer=reviewer,
        )
    db.refresh(target_evidence)
    db.refresh(foreign_evidence)
    assert target_evidence.review_state == "candidate"
    assert foreign_evidence.review_state == "candidate"


def test_approval_can_use_explicit_existing_competition_and_organizer(approval_db, client):
    db = approval_db
    existing_competition = Competition(title="Existing canonical title", is_published=False)
    existing_organizer = Organizer(name="Explicitly selected organization")
    db.add_all([existing_competition, existing_organizer])
    db.commit()
    submission_id, _ = _make_under_review(client, db, categories=False)
    admin = _admin_login(client, db, "existing")

    response = client.post(
        f"/api/v1/admin/submissions/{submission_id}/approve",
        json={
            "existing_competition_id": existing_competition.id,
            "existing_organizer_id": existing_organizer.id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["approved_competition_id"] == existing_competition.id
    db.refresh(existing_competition)
    assert existing_competition.is_published is True
    assert existing_competition.title == response.json()["title"]
    link = db.get(CompetitionOrganizer, (existing_competition.id, existing_organizer.id))
    assert link is not None
    assert (
        db.scalar(
            select(func.count())
            .select_from(SubmissionReviewEvent)
            .where(
                SubmissionReviewEvent.submission_id == submission_id,
                SubmissionReviewEvent.reviewer_user_id == admin.id,
            )
        )
        == 1
    )


def test_multiple_submissions_can_preserve_provenance_to_same_competition(approval_db, client):
    db = approval_db
    competition = Competition(is_published=False)
    organizer = Organizer(name="Shared organizer")
    db.add_all([competition, organizer])
    db.commit()
    submission_ids = []
    for index in range(2):
        if index == 1:
            first_source = db.scalar(
                select(CompetitionSource).where(
                    CompetitionSource.competition_id == competition.id
                )
            )
            first_source.source_url = (
                "https://events.example.org/hackathon?utm_source=old"
            )
            db.commit()
        submission_id, _ = _make_under_review(
            client, db, email=f"shared-{index}-{uuid4().hex}@example.org"
        )
        admin = _admin_login(client, db, f"shared-admin-{index}")
        if index == 0:
            first_reviewer_id = admin.id
        else:
            second_reviewer_id = admin.id
        response = client.post(
            f"/api/v1/admin/submissions/{submission_id}/approve",
            json={
                "existing_competition_id": competition.id,
                "existing_organizer_id": organizer.id,
            },
        )
        assert response.status_code == 200, response.text
        submission_ids.append(submission_id)

    sources = db.scalars(
        select(CompetitionSource).where(
            CompetitionSource.competition_id == competition.id,
            CompetitionSource.organizer_submission_id.in_(submission_ids),
        )
    ).all()
    assert {source.organizer_submission_id for source in sources} == set(submission_ids)
    assert len(sources) == 2
    source_ids = {source.organizer_submission_id: source.id for source in sources}
    first_title = db.scalar(
        select(FieldEvidence).where(
            FieldEvidence.source_id == source_ids[submission_ids[0]],
            FieldEvidence.field_name == "title",
        )
    )
    second_title = db.scalar(
        select(FieldEvidence).where(
            FieldEvidence.source_id == source_ids[submission_ids[1]],
            FieldEvidence.field_name == "title",
        )
    )
    assert first_title.review_state == "superseded"
    assert second_title.review_state == "selected"
    first_events = db.scalars(
        select(EvidenceReviewEvent).where(EvidenceReviewEvent.evidence_id == first_title.id)
    ).all()
    second_events = db.scalars(
        select(EvidenceReviewEvent).where(EvidenceReviewEvent.evidence_id == second_title.id)
    ).all()
    assert {(item.action, item.reviewer_user_id) for item in first_events} == {
        ("accepted", first_reviewer_id),
        ("superseded", second_reviewer_id),
    }
    assert [(item.action, item.reviewer_user_id) for item in second_events] == [
        ("accepted", second_reviewer_id)
    ]
    assert (
        db.scalar(
            select(func.count())
            .select_from(CompetitionOrganizer)
            .where(
                CompetitionOrganizer.competition_id == competition.id,
                CompetitionOrganizer.organizer_id == organizer.id,
            )
        )
        == 1
    )


def test_provenance_url_uses_existing_normalization_rules():
    assert _normalize_provenance_url(
        "https://EVENTS.example.org/hackathon?utm_source=old&campaign=shared"
    ) == _normalize_provenance_url(
        "https://events.example.org/hackathon?campaign=shared"
    )
    with pytest.raises(SubmissionValidationError):
        _normalize_provenance_url("http://events.example.org/hackathon")


def test_non_organizer_provenance_guard_is_exercised_without_database():
    class Result:
        def all(self):
            return [
                type(
                    "Source",
                    (),
                    {
                        "source_kind": "aggregator",
                        "organizer_submission_id": None,
                    },
                )()
            ]

    class SessionStub:
        def scalars(self, _statement):
            return Result()

        def scalar(self, _statement):
            raise AssertionError("non-organizer sources must be rejected before lookup")

    with pytest.raises(SubmissionValidationError, match="non-organizer"):
        _validate_existing_competition_sources(
            SessionStub(), type("Competition", (), {"id": 101})()
        )


def _create_approved_organizer_source(db, client):
    competition = Competition(is_published=False)
    organizer = Organizer(name="Previously approved organizer")
    db.add_all([competition, organizer])
    db.commit()
    submission_id, _ = _make_under_review(client, db)
    _admin_login(client, db, "provenance-first-admin")
    response = client.post(
        f"/api/v1/admin/submissions/{submission_id}/approve",
        json={
            "existing_competition_id": competition.id,
            "existing_organizer_id": organizer.id,
        },
    )
    assert response.status_code == 200, response.text
    source = db.scalar(
        select(CompetitionSource).where(
            CompetitionSource.competition_id == competition.id
        )
    )
    return competition, source


@pytest.mark.parametrize(
    "invalid_provenance",
    [
        "non_organizer_kind",
        "missing_submission_link",
        "mismatched_submission_link",
        "missing_consent",
        "invalid_consent_hash",
        "mismatched_consent_submitter",
        "missing_approval_event",
        "source_url_mismatch",
    ],
)
def test_existing_competition_reuse_rejects_invalid_provenance_atomically(
    approval_db, client, invalid_provenance
):
    db = approval_db
    competition, source = _create_approved_organizer_source(db, client)
    target_submission_id, _ = _make_under_review(
        client, db, email=f"provenance-target-{uuid4().hex}@example.org"
    )
    linked_submission = db.get(OrganizerSubmission, source.organizer_submission_id)

    if invalid_provenance == "non_organizer_kind":
        source.source_kind = "aggregator"
    elif invalid_provenance == "missing_submission_link":
        source.organizer_submission_id = None
    elif invalid_provenance == "mismatched_submission_link":
        source.organizer_submission_id = target_submission_id
    elif invalid_provenance == "missing_consent":
        consent = db.scalar(
            select(SubmissionConsent).where(
                SubmissionConsent.submission_id == linked_submission.id
            )
        )
        db.delete(consent)
    elif invalid_provenance == "invalid_consent_hash":
        consent = db.scalar(
            select(SubmissionConsent).where(
                SubmissionConsent.submission_id == linked_submission.id
            )
        )
        consent.declaration_sha256 = "0" * 64
    elif invalid_provenance == "mismatched_consent_submitter":
        consent = db.scalar(
            select(SubmissionConsent).where(
                SubmissionConsent.submission_id == linked_submission.id
            )
        )
        consent.submitter_user_id = db.get(
            OrganizerSubmission, target_submission_id
        ).submitter_user_id
    elif invalid_provenance == "missing_approval_event":
        approval_event = db.scalar(
            select(SubmissionReviewEvent).where(
                SubmissionReviewEvent.submission_id == linked_submission.id,
                SubmissionReviewEvent.new_status == "approved",
            )
        )
        db.delete(approval_event)
    elif invalid_provenance == "source_url_mismatch":
        source.source_url = "https://events.example.org/a-different-event"
    db.commit()

    before_counts = _counts(db)
    before_publication = competition.is_published
    _admin_login(client, db, f"provenance-reject-{invalid_provenance}")
    response = client.post(
        f"/api/v1/admin/submissions/{target_submission_id}/approve",
        json={"existing_competition_id": competition.id},
    )

    assert response.status_code == 422, response.text
    assert db.get(OrganizerSubmission, target_submission_id).status == "under_review"
    db.refresh(competition)
    assert competition.is_published is before_publication
    assert _counts(db) == before_counts


def test_kaggle_style_source_cannot_be_republished_by_submission_approval(
    approval_db, client
):
    db = approval_db
    competition = Competition(is_published=False)
    db.add(competition)
    db.flush()
    kaggle_source = CompetitionSource(
        competition_id=competition.id,
        source_kind="aggregator",
        source_url="https://www.kaggle.com/competitions/example",
    )
    db.add(kaggle_source)
    db.commit()
    submission_id, _ = _make_under_review(client, db)
    before_counts = _counts(db)

    _admin_login(client, db, "kaggle-provenance-admin")
    response = client.post(
        f"/api/v1/admin/submissions/{submission_id}/approve",
        json={"existing_competition_id": competition.id},
    )

    assert response.status_code == 422, response.text
    assert db.get(OrganizerSubmission, submission_id).status == "under_review"
    db.refresh(competition)
    assert competition.is_published is False
    assert _counts(db) == before_counts


def test_approval_rejects_invalid_status_competition_and_organizer(approval_db, client):
    db = approval_db
    _login(client, db, admin=True, email=f"invalid-state-admin-{uuid4().hex}@example.org")
    created = client.post("/api/v1/submissions", json=_submission_payload())
    pending_id = created.json()["id"]
    before = _counts(db)
    assert client.post(f"/api/v1/admin/submissions/{pending_id}/approve").status_code == 409
    assert _counts(db) == before

    submission_id, _ = _make_under_review(
        client, db, email=f"invalid-ref-owner-{uuid4().hex}@example.org"
    )
    _admin_login(client, db, "invalid-ref-admin")
    for selection in (
        {"existing_competition_id": 999999999},
        {"existing_organizer_id": 999999999},
    ):
        before = _counts(db)
        response = client.post(f"/api/v1/admin/submissions/{submission_id}/approve", json=selection)
        assert response.status_code == 422
        assert _counts(db) == before
        assert db.get(OrganizerSubmission, submission_id).status == "under_review"


def test_approval_endpoint_requires_admin(approval_db, client):
    assert client.post("/api/v1/admin/submissions/123/approve").status_code == 401
    _login(client, approval_db, admin=False, email=f"approval-nonadmin-{uuid4().hex}@example.org")
    assert client.post("/api/v1/admin/submissions/123/approve").status_code == 403


def _assert_approval_rolled_back(
    db: Session, submission_id: int, before: dict[str, object]
):
    db.expire_all()
    submission = db.get(OrganizerSubmission, submission_id)
    assert submission.status == "under_review"
    assert submission.approved_competition_id is None
    assert _approval_state_snapshot(db, submission_id) == before


@pytest.mark.parametrize(
    "failure_point", ["missing_evidence", "invalid_category", "submission_acceptance"]
)
def test_missing_or_unacceptable_evidence_rolls_back_all_changes(
    approval_db, client, monkeypatch, failure_point
):
    from app.submissions import service

    db = approval_db
    submission_id, _ = _make_under_review(client, db, categories=True)
    _admin_login(client, db, f"evidence-failure-{failure_point}")
    admin_token = client.cookies.get("fynd_session")
    before = _approval_state_snapshot(db, submission_id)
    if failure_point == "missing_evidence":
        monkeypatch.setattr(service, "_approval_evidence_specs", lambda *_args: [])
    elif failure_point == "invalid_category":
        monkeypatch.setattr(
            service,
            "_validate_approval_snapshot",
            lambda *_args: (_ for _ in ()).throw(
                service.SubmissionValidationError(
                    "submission contains a missing canonical category"
                )
            ),
        )
    else:
        original_accept = service.accept_submission_evidence
        first_accept = True

        def accept_then_fail(*args, **kwargs):
            nonlocal first_accept
            accepted = original_accept(*args, **kwargs)
            if first_accept:
                first_accept = False
                raise ReviewError("simulated failure after evidence acceptance")
            return accepted

        monkeypatch.setattr(
            service,
            "accept_submission_evidence",
            accept_then_fail,
        )
    failure_client = TestClient(app, raise_server_exceptions=False)
    failure_client.cookies.set("fynd_session", admin_token)
    response = failure_client.post(f"/api/v1/admin/submissions/{submission_id}/approve")
    assert response.status_code == 422
    _assert_approval_rolled_back(db, submission_id, before)


@pytest.mark.parametrize(
    "failure_point",
    [
        "relationship",
        "canonical_application",
        "evidence_review_event",
        "submission_review_event",
        "publication",
    ],
)
def test_approval_failures_roll_back_competition_source_and_evidence(
    approval_db, client, monkeypatch, failure_point
):
    from app.submissions import service

    db = approval_db
    submission_id, _ = _make_under_review(client, db)
    _admin_login(client, db, f"transaction-failure-{failure_point}")
    admin_token = client.cookies.get("fynd_session")
    before = _approval_state_snapshot(db, submission_id)
    if failure_point == "relationship":

        def fail_relationship_insert(session, _flush_context, _instances):
            if any(isinstance(row, CompetitionOrganizer) for row in session.new):
                raise RuntimeError("simulated relationship failure")

        event.listen(db, "before_flush", fail_relationship_insert)
    elif failure_point == "evidence_review_event":

        def fail_evidence_review_event_insert(session, _flush_context):
            if any(isinstance(row, EvidenceReviewEvent) for row in session.new):
                raise RuntimeError("simulated evidence review-event insertion failure")

        event.listen(db, "after_flush", fail_evidence_review_event_insert)
    elif failure_point == "submission_review_event":

        def fail_review_event_insert(session, _flush_context):
            if any(isinstance(row, SubmissionReviewEvent) for row in session.new):
                raise RuntimeError("simulated review-event insertion failure")

        event.listen(db, "after_flush", fail_review_event_insert)
    elif failure_point == "canonical_application":
        from app.review import service as review_service

        original_apply = review_service._apply_canonical_value

        def apply_then_fail(competition, evidence):
            original_apply(competition, evidence)
            db.flush()
            raise RuntimeError("simulated canonical application failure")

        monkeypatch.setattr(review_service, "_apply_canonical_value", apply_then_fail)
    elif failure_point == "publication":
        original_publish = service._publish_approved_competition

        def publish_then_fail(session, competition):
            original_publish(session, competition)
            session.flush()
            raise RuntimeError("simulated post-publication failure")

        monkeypatch.setattr(service, "_publish_approved_competition", publish_then_fail)
    try:
        failure_client = TestClient(app, raise_server_exceptions=False)
        failure_client.cookies.set("fynd_session", admin_token)
        response = failure_client.post(f"/api/v1/admin/submissions/{submission_id}/approve")
    finally:
        if failure_point == "relationship":
            event.remove(db, "before_flush", fail_relationship_insert)
        if failure_point == "evidence_review_event":
            event.remove(db, "after_flush", fail_evidence_review_event_insert)
        if failure_point == "submission_review_event":
            event.remove(db, "after_flush", fail_review_event_insert)

    assert response.status_code == 500
    _assert_approval_rolled_back(db, submission_id, before)


def test_concurrent_approval_attempts_publish_once_and_record_one_event(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.database import SessionLocal, get_db
    from app.submissions.service import SubmittedEvent, create_submission

    suffix = uuid4().hex
    admin_id = uuid4()
    submitter_id = uuid4()
    admin_token = uuid4().hex + uuid4().hex
    submission_id = None
    with SessionLocal() as db:
        admin = User(id=admin_id, email=f"approval-race-admin-{suffix}@example.org", is_admin=True)
        submitter = User(
            id=submitter_id,
            email=f"approval-race-owner-{suffix}@example.org",
        )
        db.add_all([admin, submitter])
        db.flush()
        db.add(
            UserSession(
                id=uuid4(),
                user_id=admin_id,
                session_token_hash=_hash_token(admin_token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                is_revoked=False,
            )
        )
        db.commit()
        submission = create_submission(
            db,
            user=submitter,
            event=SubmittedEvent(
                title=f"Approval race {suffix}",
                description="Concurrent approval test event.",
                organizer_name=f"Race Test Organizer {suffix}",
                organizer_email="race@example.org",
                official_url="https://events.example.org/race",
                registration_url="https://events.example.org/race/register",
                registration_deadline_on=datetime(2027, 7, 1).date(),
                event_starts_on=datetime(2027, 7, 10).date(),
                event_ends_on=datetime(2027, 7, 12).date(),
                format="online",
                registration_price_status="free",
            ),
            organizer_profile_id=None,
            category_ids=[],
            primary_category_id=None,
            declaration_version=CURRENT_CONSENT_DECLARATION_VERSION,
            declaration_text=CURRENT_CONSENT_DECLARATION_TEXT,
        )
        submission.status = "under_review"
        db.commit()
        submission_id = submission.id

    original_override = app.dependency_overrides.get(get_db)

    def session_dependency():
        with SessionLocal() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, session_dependency)
    barrier = Barrier(3)

    def attempt():
        test_client = TestClient(app)
        test_client.cookies.set("fynd_session", admin_token)
        barrier.wait()
        return test_client.post(f"/api/v1/admin/submissions/{submission_id}/approve")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt) for _ in range(2)]
            barrier.wait()
            responses = [future.result(timeout=30) for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        with SessionLocal() as db:
            submission = db.get(OrganizerSubmission, submission_id)
            assert submission.status == "approved"
            assert submission.approved_competition_id is not None
            assert db.scalar(
                select(func.count()).select_from(CompetitionSource).where(
                    CompetitionSource.organizer_submission_id == submission_id
                )
            ) == 1
            assert db.scalar(
                select(func.count()).select_from(SubmissionReviewEvent).where(
                    SubmissionReviewEvent.submission_id == submission_id
                )
            ) == 1
    finally:
        if original_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = original_override
        with SessionLocal() as db:
            competition_id = db.scalar(
                select(OrganizerSubmission.approved_competition_id).where(
                    OrganizerSubmission.id == submission_id
                )
            )
            source_ids = select(CompetitionSource.id).where(
                CompetitionSource.organizer_submission_id == submission_id
            )
            evidence_ids = select(FieldEvidence.id).where(
                FieldEvidence.source_id.in_(source_ids)
            )
            db.execute(
                delete(EvidenceReviewEvent).where(
                    EvidenceReviewEvent.evidence_id.in_(evidence_ids)
                )
            )
            db.execute(delete(FieldEvidence).where(FieldEvidence.source_id.in_(source_ids)))
            db.execute(
                delete(CompetitionSource).where(
                    CompetitionSource.organizer_submission_id == submission_id
                )
            )
            db.execute(
                delete(CompetitionOrganizer).where(
                    CompetitionOrganizer.competition_id == competition_id
                )
            )
            db.execute(
                delete(SubmissionReviewEvent).where(
                    SubmissionReviewEvent.submission_id == submission_id
                )
            )
            db.execute(
                delete(SubmissionConsent).where(SubmissionConsent.submission_id == submission_id)
            )
            db.execute(
                delete(OrganizerSubmission).where(OrganizerSubmission.id == submission_id)
            )
            if competition_id is not None:
                db.execute(delete(Competition).where(Competition.id == competition_id))
            db.execute(delete(Organizer).where(Organizer.name == f"Race Test Organizer {suffix}"))
            db.execute(delete(UserSession).where(UserSession.user_id.in_([admin_id, submitter_id])))
            db.execute(delete(User).where(User.id.in_([admin_id, submitter_id])))
            db.commit()
