from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.database import Base
from app.database import engine as postgres_engine
from app.models import (
    Category,
    Competition,
    CompetitionSource,
    Organizer,
    OrganizerProfile,
    OrganizerProfileMember,
    OrganizerSubmission,
    OrganizerSubmissionCategory,
    SourcePublisher,
    SubmissionConsent,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "d7e82b64a109_add_organizer_submission_models.py"
)
_MIGRATION_SPEC = spec_from_file_location("organizer_submission_migration", _MIGRATION_PATH)
assert _MIGRATION_SPEC is not None and _MIGRATION_SPEC.loader is not None
_MIGRATION = module_from_spec(_MIGRATION_SPEC)
_MIGRATION_SPEC.loader.exec_module(_MIGRATION)


@pytest.fixture
def submission_db():
    engine = create_engine("sqlite+pysqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        User.__table__,
        Competition.__table__,
        Organizer.__table__,
        Category.__table__,
        SourcePublisher.__table__,
        OrganizerProfile.__table__,
        OrganizerProfileMember.__table__,
        OrganizerSubmission.__table__,
        SubmissionConsent.__table__,
        OrganizerSubmissionCategory.__table__,
        CompetitionSource.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _user(email: str) -> User:
    return User(id=uuid4(), email=email)


def _submission(submission_id: int, user_id, **overrides) -> OrganizerSubmission:
    values = {
        "id": submission_id,
        "submitter_user_id": user_id,
        "title": "Student AI Hackathon",
        "description": "Build a useful AI application.",
        "organizer_name": "Fynd Community",
        "organizer_email": "events@example.org",
        "official_url": "https://events.example.org/hackathon",
        "registration_url": "https://events.example.org/register",
        "registration_deadline_on": date(2027, 4, 1),
        "event_starts_on": date(2027, 4, 10),
        "event_ends_on": date(2027, 4, 12),
        "format": "online",
        "registration_price_status": "unknown",
    }
    values.update(overrides)
    return OrganizerSubmission(**values)


def test_profiles_and_memberships_support_many_users_and_organizations(submission_db):
    first_user = _user("first@example.org")
    second_user = _user("second@example.org")
    first_profile = OrganizerProfile(
        id=uuid4(), claimed_name="Org One", contact_email="one@example.org"
    )
    second_profile = OrganizerProfile(
        id=uuid4(), claimed_name="Org Two", contact_email="two@example.org"
    )
    submission_db.add_all([first_user, second_user, first_profile, second_profile])
    submission_db.flush()
    submission_db.add_all(
        [
            OrganizerProfileMember(
                organizer_profile_id=first_profile.id,
                user_id=first_user.id,
                role="owner",
            ),
            OrganizerProfileMember(
                organizer_profile_id=first_profile.id,
                user_id=second_user.id,
                role="manager",
            ),
            OrganizerProfileMember(
                organizer_profile_id=second_profile.id,
                user_id=first_user.id,
                role="owner",
            ),
        ]
    )
    submission_db.flush()

    assert len(first_profile.members) == 2
    assert len(second_profile.members) == 1
    assert {member.user_id for member in first_profile.members} == {
        first_user.id,
        second_user.id,
    }


def test_profile_canonical_organizer_link_is_unique(submission_db):
    organizer = Organizer(id=1, name="Verified Organizer")
    profiles = [
        OrganizerProfile(
            id=uuid4(),
            claimed_name=f"Claim {index}",
            contact_email=f"claim{index}@example.org",
            verification_status="verified",
            canonical_organizer=organizer,
            verified_at=datetime.now(UTC),
        )
        for index in (1, 2)
    ]
    submission_db.add_all([organizer, *profiles])

    with pytest.raises(IntegrityError):
        submission_db.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"verification_status": "approved"},
        {"verification_status": "verified"},
    ],
)
def test_profile_rejects_unknown_status_or_verified_without_canonical_link(
    submission_db, values
):
    submission_db.add(
        OrganizerProfile(
            id=uuid4(),
            claimed_name="Invalid profile",
            contact_email="invalid@example.org",
            **values,
        )
    )

    with pytest.raises(IntegrityError):
        submission_db.flush()


@pytest.mark.parametrize("status", ["pending", "under_review", "approved", "rejected", "withdrawn"])
def test_submission_accepts_each_status(submission_db, status):
    user = _user(f"{status}@example.org")
    submission_db.add(user)
    submission_db.flush()
    submission_db.add(_submission(10, user.id, status=status))
    submission_db.flush()


@pytest.mark.parametrize("overrides", [{"status": "queued"}, {"format": "remote"}])
def test_submission_rejects_invalid_status_and_format(submission_db, overrides):
    user = _user("invalid@example.org")
    submission_db.add(user)
    submission_db.flush()
    submission_db.add(_submission(20, user.id, **overrides))

    with pytest.raises(IntegrityError):
        submission_db.flush()


def test_submission_accepts_date_and_timestamp_precision(submission_db):
    user = _user("dates@example.org")
    submission_db.add(user)
    submission_db.flush()
    submission_db.add(
        _submission(
            30,
            user.id,
            registration_deadline_at=datetime(2027, 4, 1, 17, tzinfo=UTC),
            registration_deadline_on=None,
            event_starts_at=datetime(2027, 4, 10, 9, tzinfo=UTC),
            event_starts_on=None,
            event_ends_at=datetime(2027, 4, 12, 17, tzinfo=UTC),
            event_ends_on=None,
        )
    )
    submission_db.flush()


@pytest.mark.parametrize(
    "overrides",
    [
        {"registration_deadline_at": datetime(2027, 4, 1, tzinfo=UTC)},
        {"event_ends_on": date(2027, 4, 9)},
        {
            "event_starts_at": datetime(2027, 4, 10, tzinfo=UTC),
            "event_ends_at": datetime(2027, 4, 9, tzinfo=UTC),
            "event_starts_on": None,
            "event_ends_on": None,
        },
        {"event_starts_on": None},
    ],
)
def test_submission_rejects_invalid_date_combinations(submission_db, overrides):
    user = _user("bad-date@example.org")
    submission_db.add(user)
    submission_db.flush()
    submission_db.add(_submission(40, user.id, **overrides))

    with pytest.raises(IntegrityError):
        submission_db.flush()


@pytest.mark.parametrize(
    ("status", "amount", "currency", "valid"),
    [
        ("free", None, None, True),
        ("paid", Decimal("25.00"), "INR", True),
        ("unknown", None, None, True),
        ("paid", None, "INR", False),
        ("paid", Decimal("25.00"), None, False),
        ("paid", Decimal("0.00"), "INR", False),
        ("free", Decimal("25.00"), "INR", False),
        ("free", None, "INR", False),
        ("unknown", None, "INR", False),
        ("unknown", Decimal("25.00"), None, False),
        ("invalid", None, None, False),
    ],
)
def test_submission_pricing_constraints(submission_db, status, amount, currency, valid):
    user = _user(f"price-{status}-{amount}@example.org")
    submission_db.add(user)
    submission_db.flush()
    submission_db.add(
        _submission(
            50,
            user.id,
            registration_price_status=status,
            registration_fee_amount=amount,
            registration_fee_currency=currency,
        )
    )
    if valid:
        submission_db.flush()
    else:
        with pytest.raises(IntegrityError):
            submission_db.flush()


def test_pricing_constraint_matches_migration_expression():
    model_check = next(
        check
        for check in OrganizerSubmission.__table__.constraints
        if check.name == "ck_organizer_submissions_fee_matches_status"
    )

    assert " ".join(str(model_check.sqltext).split()) == " ".join(
        _MIGRATION.FEE_STATUS_CHECK_EXPRESSION.split()
    )


@pytest.mark.integration
def test_postgresql_pricing_constraint_rejects_null_unknown_semantics():
    table_name = f"submission_pricing_check_{uuid4().hex}"
    expression = _MIGRATION.FEE_STATUS_CHECK_EXPRESSION
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    f"CREATE TEMP TABLE {table_name} ("
                    "registration_price_status varchar(8) NOT NULL, "
                    "registration_fee_amount numeric(14, 2), "
                    "registration_fee_currency varchar(3), "
                    f"CONSTRAINT fee_status_check CHECK ({expression})"
                    ") ON COMMIT DROP"
                )
            )
            insert = text(
                f"INSERT INTO {table_name} "
                "(registration_price_status, registration_fee_amount, "
                "registration_fee_currency) VALUES (:status, :amount, :currency)"
            )
            for status, amount, currency in (
                ("paid", 25, "INR"),
                ("free", None, None),
                ("unknown", None, None),
            ):
                connection.execute(
                    insert,
                    {"status": status, "amount": amount, "currency": currency},
                )

            for status, amount, currency in (
                ("paid", None, "INR"),
                ("paid", 25, None),
                ("free", 25, "INR"),
                ("free", None, "INR"),
                ("unknown", 25, None),
                ("unknown", None, "INR"),
            ):
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(
                            insert,
                            {
                                "status": status,
                                "amount": amount,
                                "currency": currency,
                            },
                        )
        finally:
            transaction.rollback()


def test_consent_is_unique_per_submission_and_retains_exact_declaration(submission_db):
    user = _user("consent@example.org")
    submission = _submission(60, user.id)
    submission_db.add_all([user, submission])
    submission_db.flush()
    consent = SubmissionConsent(
        id=1,
        submission_id=submission.id,
        submitter_user_id=user.id,
        submitter_email_snapshot=user.email,
        declaration_version="2026-11-v1",
        declaration_text="I authorize Fynd to display this event information.",
        declaration_sha256=sha256(
            b"I authorize Fynd to display this event information."
        ).hexdigest(),
        accepted_at=datetime.now(UTC),
    )
    submission_db.add(consent)
    submission_db.flush()
    assert submission.consent.declaration_text == consent.declaration_text

    with pytest.raises(IntegrityError):
        with submission_db.begin_nested():
            submission_db.add(
                SubmissionConsent(
                    id=2,
                    submission_id=submission.id,
                    submitter_user_id=user.id,
                    submitter_email_snapshot=user.email,
                    declaration_version="2026-11-v1",
                    declaration_text=consent.declaration_text,
                    declaration_sha256=consent.declaration_sha256,
                    accepted_at=datetime.now(UTC),
                )
            )
            submission_db.flush()


def test_submission_categories_prevent_duplicates_and_multiple_primary_values(submission_db):
    user = _user("categories@example.org")
    submission = _submission(70, user.id)
    categories = [
        Category(id=1, name="AI", slug="ai"),
        Category(id=2, name="Web", slug="web"),
    ]
    submission_db.add_all([user, submission, *categories])
    submission_db.flush()
    submission_db.add_all(
        [
            OrganizerSubmissionCategory(
                submission_id=submission.id, category_id=1, is_primary=True
            ),
            OrganizerSubmissionCategory(
                submission_id=submission.id, category_id=2, is_primary=False
            ),
        ]
    )
    submission_db.flush()

    with pytest.raises(IntegrityError):
        with submission_db.begin_nested():
            submission_db.add(
                OrganizerSubmissionCategory(
                    submission_id=submission.id, category_id=1, is_primary=False
                )
            )
            submission_db.flush()

    with pytest.raises(IntegrityError):
        with submission_db.begin_nested():
            submission_db.add(
                OrganizerSubmissionCategory(
                    submission_id=submission.id, category_id=2, is_primary=True
                )
            )
            submission_db.flush()


def test_multiple_submissions_can_reference_one_competition(submission_db):
    user = _user("shared-event@example.org")
    competition = Competition(id=80, title="Canonical shared event")
    first = _submission(81, user.id, approved_competition=competition, status="approved")
    second = _submission(82, user.id, approved_competition=competition, status="approved")
    submission_db.add_all([user, first, second])
    submission_db.flush()

    assert [row.id for row in competition.organizer_submissions] == [81, 82]


def test_competition_source_submission_provenance_is_nullable(submission_db):
    competition = Competition(id=90)
    publisher = SourcePublisher(id=91, name="Existing discovery publisher")
    source = CompetitionSource(
        id=92,
        competition=competition,
        publisher=publisher,
        source_kind="aggregator",
        source_url="https://source.example.org/event",
    )
    submission_db.add(source)
    submission_db.flush()

    assert source.organizer_submission_id is None
