from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.email_address import normalize_email
from app.auth.models import User
from app.database import engine as postgres_engine
from app.models import (
    Category,
    Competition,
    CompetitionSource,
    OrganizerProfile,
    OrganizerProfileMember,
    OrganizerSubmission,
    OrganizerSubmissionCategory,
    SubmissionConsent,
)
from app.submissions.consent import (
    CURRENT_CONSENT_DECLARATION_TEXT,
    CURRENT_CONSENT_DECLARATION_VERSION,
)
from app.submissions.service import (
    SubmissionValidationError,
    SubmittedEvent,
    create_submission,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def submission_service_db():
    connection = postgres_engine.connect()
    transaction = connection.begin()
    try:
        with Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as db:
            yield db
    finally:
        transaction.rollback()
        connection.close()


def _event(**overrides) -> SubmittedEvent:
    values = {
        "title": "Student AI Hackathon",
        "description": "Build an AI application.",
        "organizer_name": "Fynd Community",
        "organizer_email": "Events@Example.org",
        "official_url": "https://events.example.org/hackathon",
        "registration_url": "https://events.example.org/register",
        "registration_deadline_on": date(2027, 4, 1),
        "event_starts_on": date(2027, 4, 10),
        "event_ends_on": date(2027, 4, 12),
        "format": "online",
        "registration_price_status": "free",
    }
    values.update(overrides)
    return SubmittedEvent(**values)


def _create_user(db: Session, *, email: str = "Submitter@Example.org", **values) -> User:
    user = User(id=uuid4(), email=email, **values)
    db.add(user)
    db.commit()
    return user


def _create(db: Session, user: User, event_info: SubmittedEvent | None = None, **kwargs):
    return create_submission(
        db,
        user=user,
        event=event_info or _event(),
        organizer_profile_id=kwargs.pop("organizer_profile_id", None),
        category_ids=kwargs.pop("category_ids", []),
        primary_category_id=kwargs.pop("primary_category_id", None),
        declaration_version=kwargs.pop("declaration_version", CURRENT_CONSENT_DECLARATION_VERSION),
        declaration_text=kwargs.pop("declaration_text", CURRENT_CONSENT_DECLARATION_TEXT),
        **kwargs,
    )


def test_successful_submission_owns_user_records_consent_categories_and_stays_unpublished(
    submission_service_db,
):
    db = submission_service_db
    competition_count_before = db.scalar(select(func.count()).select_from(Competition))
    source_count_before = db.scalar(select(func.count()).select_from(CompetitionSource))
    user = _create_user(db)
    db.add_all([Category(id=101, name="AI", slug="ai"), Category(id=102, name="Web", slug="web")])
    db.commit()

    submission = _create(db, user, category_ids=[101, 102], primary_category_id=102)

    assert submission.submitter_user_id == user.id
    assert submission.status == "pending"
    assert submission.organizer_email == "events@example.org"
    assert [(link.category_id, link.is_primary) for link in submission.category_links] == [
        (101, False),
        (102, True),
    ]
    consent = db.scalar(
        select(SubmissionConsent).where(SubmissionConsent.submission_id == submission.id)
    )
    assert consent.submitter_user_id == user.id
    assert consent.submitter_email_snapshot == normalize_email(user.email)
    assert consent.declaration_text == CURRENT_CONSENT_DECLARATION_TEXT
    assert consent.declaration_version == CURRENT_CONSENT_DECLARATION_VERSION
    assert (
        consent.declaration_sha256 == sha256(CURRENT_CONSENT_DECLARATION_TEXT.encode()).hexdigest()
    )
    assert consent.accepted_at.tzinfo is not None
    assert db.scalar(select(func.count()).select_from(Competition)) == competition_count_before
    assert db.scalar(select(func.count()).select_from(CompetitionSource)) == source_count_before


def test_optional_profile_requires_membership_and_accepts_authorized_member(submission_service_db):
    db = submission_service_db
    owner = _create_user(db, email="owner@example.org")
    other = _create_user(db, email="other@example.org")
    profile = OrganizerProfile(
        id=uuid4(), claimed_name="Organizer", contact_email="org@example.org"
    )
    db.add(profile)
    db.flush()
    db.add(OrganizerProfileMember(organizer_profile_id=profile.id, user_id=owner.id, role="owner"))
    db.commit()

    with pytest.raises(SubmissionValidationError, match="authorized member"):
        _create(db, other, organizer_profile_id=profile.id)
    submission = _create(db, owner, organizer_profile_id=profile.id)
    assert submission.organizer_profile_id == profile.id


@pytest.mark.parametrize(
    ("category_ids", "primary_category_id", "expected"),
    [
        ([101], 102, "primary category"),
        ([101, 999], 101, "do not exist"),
        ([101], None, "primary category"),
    ],
)
def test_category_selection_validation(
    submission_service_db, category_ids, primary_category_id, expected
):
    db = submission_service_db
    user = _create_user(db)
    db.add(Category(id=101, name="AI", slug="ai"))
    db.commit()
    with pytest.raises(SubmissionValidationError, match=expected):
        _create(db, user, category_ids=category_ids, primary_category_id=primary_category_id)
    assert db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0


@pytest.mark.parametrize(
    "event_info",
    [
        _event(format="remote"),
        _event(event_starts_on=date(2027, 4, 13)),
        _event(registration_deadline_on=None, registration_deadline_at=None),
        _event(
            event_starts_at=datetime(2027, 4, 10, 9),
            event_starts_on=None,
        ),
        _event(
            registration_price_status="paid",
            registration_fee_amount=None,
            registration_fee_currency="INR",
        ),
        _event(
            registration_price_status="paid",
            registration_fee_amount=Decimal("0"),
            registration_fee_currency="INR",
        ),
        _event(
            registration_price_status="free",
            registration_fee_amount=Decimal("5"),
            registration_fee_currency="INR",
        ),
        _event(registration_price_status="unknown", registration_fee_currency="INR"),
    ],
)
def test_invalid_event_values_are_rejected(submission_service_db, event_info):
    db = submission_service_db
    user = _create_user(db)
    with pytest.raises(SubmissionValidationError):
        _create(db, user, event_info)
    assert db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0


@pytest.mark.parametrize(
    "declaration",
    [
        {"declaration_version": "old", "declaration_text": CURRENT_CONSENT_DECLARATION_TEXT},
        {
            "declaration_version": CURRENT_CONSENT_DECLARATION_VERSION,
            "declaration_text": "client edited",
        },
    ],
)
def test_invalid_or_outdated_consent_is_rejected(submission_service_db, declaration):
    db = submission_service_db
    user = _create_user(db)
    with pytest.raises(SubmissionValidationError, match="consent"):
        _create(db, user, **declaration)
    assert db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0


def test_missing_inactive_or_unpersisted_user_is_rejected(submission_service_db):
    db = submission_service_db
    inactive = _create_user(db, email="inactive@example.org", is_active=False)
    with pytest.raises(SubmissionValidationError, match="active"):
        _create(db, inactive)
    with pytest.raises(SubmissionValidationError, match="active"):
        _create(db, User(id=uuid4(), email="detached@example.org"))


@pytest.mark.parametrize("failing_model", [SubmissionConsent, OrganizerSubmissionCategory])
def test_insert_failure_rolls_back_entire_submission(submission_service_db, failing_model):
    db = submission_service_db
    user = _create_user(db)
    db.add(Category(id=101, name="AI", slug="ai"))
    db.commit()

    def fail_insert(session, _flush_context, _instances):
        if any(isinstance(instance, failing_model) for instance in session.new):
            raise RuntimeError("simulated dependent record failure")

    sqlalchemy_event.listen(db, "before_flush", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            _create(db, user, category_ids=[101], primary_category_id=101)
    finally:
        sqlalchemy_event.remove(db, "before_flush", fail_insert)

    assert db.scalar(select(func.count()).select_from(OrganizerSubmission)) == 0
    assert db.scalar(select(func.count()).select_from(SubmissionConsent)) == 0
    assert db.scalar(select(func.count()).select_from(OrganizerSubmissionCategory)) == 0
