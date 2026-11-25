from types import SimpleNamespace
from uuid import uuid4

import pytest
from integration_database_guard import guard_isolated_postgres_integration_tests
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

guard_isolated_postgres_integration_tests()

from app.auth.models import User  # noqa: E402
from app.database import engine  # noqa: E402
from app.discovery.registry import RegistryError  # noqa: E402
from app.models import (  # noqa: E402
    Competition,
    CompetitionSource,
    EvidenceReviewEvent,
    FieldEvidence,
)
from app.review import ReviewError, accept_evidence, reject_evidence  # noqa: E402
from app.review import service as review_service  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def review_database_available():
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for evidence review tests")


@pytest.fixture
def db_session(review_database_available):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def reviewer(db_session):
    user = User(id=uuid4(), email=f"reviewer-{uuid4().hex}@example.test")
    db_session.add(user)
    db_session.flush()
    return user


def _candidate(session, *, registry_source_id="trusted-source", source_url=None):
    competition = Competition(title="Canonical title")
    session.add(competition)
    session.flush()
    source = CompetitionSource(
        competition_id=competition.id,
        source_kind="official",
        source_url=source_url or f"https://allowed.example.test/{uuid4().hex}",
        normalized_url=f"https://allowed.example.test/{uuid4().hex}",
        registry_source_id=registry_source_id,
    )
    session.add(source)
    session.flush()
    evidence = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name="title",
        field_key="",
        observed_value={"title": "Reviewed title"},
        evidence_excerpt="Reviewed title",
        review_state="candidate",
    )
    session.add(evidence)
    session.flush()
    return competition, source, evidence


def _allow_source(monkeypatch, host="allowed.example.test"):
    monkeypatch.setattr(
        review_service,
        "authorize_source_operation",
        lambda source_id, operation: SimpleNamespace(
            id=source_id,
            access={"allowed_hosts": [host]},
        ),
    )


def _event_count(session, evidence_id):
    return session.scalar(
        select(func.count())
        .select_from(EvidenceReviewEvent)
        .where(EvidenceReviewEvent.evidence_id == evidence_id)
    )


def test_accept_records_trusted_authorized_review_and_reviewer(
    db_session, reviewer, monkeypatch
):
    _allow_source(monkeypatch)
    competition, _, evidence = _candidate(db_session)

    accepted = accept_evidence(db_session, evidence.id, reviewer=reviewer)
    event = db_session.scalar(
        select(EvidenceReviewEvent).where(EvidenceReviewEvent.evidence_id == evidence.id)
    )

    db_session.refresh(competition)
    assert accepted.review_state == "selected"
    assert competition.title == "Reviewed title"
    assert (event.action, event.previous_state, event.new_state) == (
        "accepted",
        "candidate",
        "selected",
    )
    assert event.reviewer_user_id == reviewer.id


@pytest.mark.parametrize("reviewer_state", ["inactive", "missing"])
def test_accept_rejects_inactive_or_missing_reviewer_without_mutation(
    db_session, reviewer, monkeypatch, reviewer_state
):
    _allow_source(monkeypatch)
    competition, _, evidence = _candidate(db_session)
    if reviewer_state == "inactive":
        reviewer.is_active = False
        db_session.flush()
        supplied_reviewer = reviewer
    else:
        supplied_reviewer = User(id=uuid4(), email="absent-reviewer@example.test")

    with pytest.raises(ReviewError, match="does not exist or is inactive"):
        accept_evidence(db_session, evidence.id, reviewer=supplied_reviewer)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == "Canonical title"
    assert evidence.review_state == "candidate"
    assert _event_count(db_session, evidence.id) == 0


@pytest.mark.parametrize(
    "registry_source_id,authorization_error,source_url,expected_error",
    [
        (None, None, None, "legacy evidence"),
        ("missing-source", "unknown", None, "not registered"),
        ("revoked-source", "revoked", None, "refused"),
        (
            "trusted-source",
            None,
            "https://blocked.example.test/event",
            "persisted source URL is not allowed for internal review",
        ),
    ],
)
def test_acceptance_authorization_failures_leave_review_state_and_audit_unchanged(
    db_session,
    reviewer,
    monkeypatch,
    registry_source_id,
    authorization_error,
    source_url,
    expected_error,
):
    if authorization_error:
        def deny(_source_id, _operation):
            if authorization_error == "unknown":
                raise RegistryError("source is not registered")
            raise RegistryError("source internal_review refused: permission revoked")

        monkeypatch.setattr(review_service, "authorize_source_operation", deny)
    else:
        _allow_source(monkeypatch)
    competition, _, evidence = _candidate(
        db_session,
        registry_source_id=registry_source_id,
        source_url=source_url,
    )
    before_title = competition.title

    with pytest.raises(ReviewError, match=expected_error):
        accept_evidence(db_session, evidence.id, reviewer=reviewer)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == before_title
    assert evidence.review_state == "candidate"
    assert _event_count(db_session, evidence.id) == 0


@pytest.mark.parametrize("registry_source_id", [None, "revoked-source"])
def test_rejection_does_not_require_registry_identity_or_permission(
    db_session, reviewer, monkeypatch, registry_source_id
):
    def deny(*_args):
        raise RegistryError("source internal_review refused: permission revoked")

    monkeypatch.setattr(review_service, "authorize_source_operation", deny)
    _, _, evidence = _candidate(db_session, registry_source_id=registry_source_id)

    rejected = reject_evidence(db_session, evidence.id, reviewer=reviewer)
    audit = db_session.scalar(
        select(EvidenceReviewEvent).where(EvidenceReviewEvent.evidence_id == evidence.id)
    )

    assert rejected.review_state == "rejected"
    assert (audit.action, audit.previous_state, audit.new_state) == (
        "rejected",
        "candidate",
        "rejected",
    )
    assert audit.reviewer_user_id == reviewer.id


@pytest.mark.parametrize("reviewer_state", ["inactive", "missing"])
def test_reject_rejects_inactive_or_missing_reviewer_without_mutation(
    db_session, reviewer, monkeypatch, reviewer_state
):
    _, _, evidence = _candidate(db_session, registry_source_id=None)
    if reviewer_state == "inactive":
        reviewer.is_active = False
        db_session.flush()
        supplied_reviewer = reviewer
    else:
        supplied_reviewer = User(id=uuid4(), email="absent-reviewer@example.test")

    with pytest.raises(ReviewError, match="does not exist or is inactive"):
        reject_evidence(db_session, evidence.id, reviewer=supplied_reviewer)

    db_session.refresh(evidence)
    assert evidence.review_state == "candidate"
    assert _event_count(db_session, evidence.id) == 0


def test_invalid_repeated_transitions_do_not_add_audit_events(
    db_session, reviewer, monkeypatch
):
    _allow_source(monkeypatch)
    _, _, evidence = _candidate(db_session)
    reject_evidence(db_session, evidence.id, reviewer=reviewer)

    with pytest.raises(ReviewError, match="already rejected"):
        reject_evidence(db_session, evidence.id, reviewer=reviewer)
    with pytest.raises(ReviewError, match="already rejected"):
        accept_evidence(db_session, evidence.id, reviewer=reviewer)

    assert _event_count(db_session, evidence.id) == 1


def test_supersession_records_both_transitions_atomically(db_session, reviewer, monkeypatch):
    _allow_source(monkeypatch)
    competition, source, first = _candidate(db_session)
    accept_evidence(db_session, first.id, reviewer=reviewer)
    replacement = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name="title",
        field_key="",
        observed_value={"title": "Replacement title"},
        evidence_excerpt="Replacement title",
        review_state="candidate",
    )
    db_session.add(replacement)
    db_session.flush()

    accept_evidence(db_session, replacement.id, reviewer=reviewer)

    db_session.refresh(competition)
    db_session.refresh(first)
    events = db_session.scalars(
        select(EvidenceReviewEvent)
        .where(EvidenceReviewEvent.evidence_id.in_([first.id, replacement.id]))
        .order_by(EvidenceReviewEvent.evidence_id, EvidenceReviewEvent.action)
    ).all()
    assert competition.title == "Replacement title"
    assert first.review_state == "superseded"
    assert replacement.review_state == "selected"
    transitions = {
        (event.evidence_id, event.action, event.previous_state, event.new_state)
        for event in events
    }
    assert transitions == {
        (first.id, "accepted", "candidate", "selected"),
        (first.id, "superseded", "selected", "superseded"),
        (replacement.id, "accepted", "candidate", "selected"),
    }
    assert all(event.reviewer_user_id == reviewer.id for event in events)


def test_failed_supersession_rolls_back_canonical_states_and_new_audit_events(
    db_session, reviewer, monkeypatch
):
    _allow_source(monkeypatch)
    competition, source, first = _candidate(db_session)
    accept_evidence(db_session, first.id, reviewer=reviewer)
    replacement = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name="title",
        field_key="",
        observed_value={"title": "Must roll back"},
        evidence_excerpt="Must roll back",
        review_state="candidate",
    )
    db_session.add(replacement)
    db_session.flush()

    def fail_audit_flush(session, _flush_context, _instances):
        if any(isinstance(item, EvidenceReviewEvent) for item in session.new):
            raise RuntimeError("simulated audit insert failure")

    sqlalchemy_event.listen(db_session, "before_flush", fail_audit_flush)
    try:
        with pytest.raises(RuntimeError, match="simulated audit insert"):
            accept_evidence(db_session, replacement.id, reviewer=reviewer)
    finally:
        sqlalchemy_event.remove(db_session, "before_flush", fail_audit_flush)
    db_session.expire_all()

    assert db_session.get(Competition, competition.id).title == "Reviewed title"
    assert db_session.get(FieldEvidence, first.id).review_state == "selected"
    assert db_session.get(FieldEvidence, replacement.id).review_state == "candidate"
    assert _event_count(db_session, first.id) == 1
    assert _event_count(db_session, replacement.id) == 0
