from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import Competition, CompetitionSource, FieldEvidence, SourcePublisher
from app.review import (
    ReviewError,
    UnsupportedEvidenceField,
    accept_evidence,
    get_competition_detail,
    list_pending_evidence,
    reject_evidence,
)

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


def _add_source_and_evidence(
    session,
    competition,
    *,
    value,
    field_name="title",
    review_state="candidate",
    suffix="source",
):
    publisher = SourcePublisher(name=f"Review test publisher {suffix}")
    session.add(publisher)
    session.flush()
    source = CompetitionSource(
        competition_id=competition.id,
        publisher_id=publisher.id,
        source_kind="official",
        source_url=f"https://review.example.test/{suffix}",
        normalized_url=f"https://review.example.test/{suffix}",
    )
    session.add(source)
    session.flush()
    evidence = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name=field_name,
        observed_value=value,
        evidence_excerpt=str(value),
        review_state=review_state,
    )
    session.add(evidence)
    session.flush()
    return source, evidence


def test_list_and_detail_include_source_provenance_and_evidence(db_session):
    competition = Competition()
    db_session.add(competition)
    db_session.flush()
    source, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Reviewable event"},
        suffix="list-detail",
    )

    pending = list_pending_evidence(db_session)
    listed = next(row for row in pending if row["evidence_id"] == evidence.id)
    detail = get_competition_detail(db_session, competition.id)

    assert listed["source_url"] == source.source_url
    assert listed["publisher"] == source.publisher.name
    assert listed["observed_value"] == {"title": "Reviewable event"}
    assert detail["competition"]["title"] is None
    assert detail["sources"][0]["source_url"] == source.source_url
    assert detail["sources"][0]["evidence"][0]["review_state"] == "candidate"


def test_rejecting_evidence_preserves_canonical_values_and_provenance(db_session):
    competition = Competition(title="Existing title")
    db_session.add(competition)
    db_session.flush()
    source, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Rejected title"},
        suffix="reject",
    )

    rejected = reject_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(source)
    assert rejected.review_state == "rejected"
    assert rejected.reviewed_at is not None
    assert competition.title == "Existing title"
    assert source.source_url == "https://review.example.test/reject"
    assert rejected.source_id == source.id


def test_accepting_evidence_updates_only_its_mapped_canonical_field(db_session):
    competition = Competition(title=None, description=None)
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"description": "A concise event description."},
        field_name="description",
        suffix="description",
    )

    accepted = accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    assert accepted.review_state == "selected"
    assert accepted.reviewed_at is not None
    assert competition.description == "A concise event description."
    assert competition.title is None


def test_accepting_replacement_supersedes_prior_selection_and_keeps_both(db_session):
    competition = Competition(title="Previous title")
    db_session.add(competition)
    db_session.flush()
    _, old = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Previous title"},
        review_state="selected",
        suffix="selected-old",
    )
    _, replacement = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Updated title"},
        suffix="selected-new",
    )

    accept_evidence(db_session, replacement.id)

    db_session.refresh(competition)
    db_session.refresh(old)
    db_session.refresh(replacement)
    assert competition.title == "Updated title"
    assert old.review_state == "superseded"
    assert replacement.review_state == "selected"
    assert db_session.scalar(
        select(func.count()).select_from(FieldEvidence).where(
            FieldEvidence.competition_id == competition.id,
            FieldEvidence.field_name == "title",
        )
    ) == 2


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("title", {"title": "  "}),
        ("format", {"format": "in-person"}),
        ("official_url", {"official_url": "javascript:alert(1)"}),
        ("event_starts", {"event_starts": {"at": "2027-01-01T12:00:00"}}),
    ],
)
def test_invalid_evidence_is_not_selected_or_applied(db_session, field_name, value):
    competition = Competition(title="Stable title")
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value=value,
        field_name=field_name,
        suffix=f"invalid-{field_name}",
    )

    with pytest.raises(ReviewError):
        accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == "Stable title"
    assert evidence.review_state == "candidate"
    assert evidence.reviewed_at is None


def test_unsupported_relationship_evidence_cannot_update_canonical_data(db_session):
    competition = Competition(title="Stable title")
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"name": "Organizer A"},
        field_name="organizer",
        suffix="unsupported-organizer",
    )

    with pytest.raises(UnsupportedEvidenceField, match="not supported"):
        accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == "Stable title"
    assert evidence.review_state == "candidate"


def test_flush_failure_rolls_back_review_and_canonical_update(db_session, monkeypatch):
    competition = Competition()
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Must not partially apply"},
        suffix="flush-failure",
    )
    original_flush = db_session.flush
    flush_count = 0

    def fail_after_changes(objects=None):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            raise RuntimeError("simulated review flush failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_after_changes)
    with pytest.raises(RuntimeError, match="simulated review"):
        accept_evidence(db_session, evidence.id)
    monkeypatch.setattr(db_session, "flush", original_flush)
    db_session.expire_all()

    assert db_session.get(Competition, competition.id).title is None
    assert db_session.get(FieldEvidence, evidence.id).review_state == "candidate"


def test_migration_clears_only_matching_unselected_candidate_titles(db_session, monkeypatch):
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "6a1c4f2b9d30_clear_unreviewed_candidate_titles.py"
    )
    spec = spec_from_file_location("candidate_title_cleanup", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(execute=lambda statement: db_session.execute(text(statement))),
    )

    matching = Competition(title="Matching candidate")
    mismatch = Competition(title="Manually changed title")
    selected = Competition(title="Accepted title")
    db_session.add_all([matching, mismatch, selected])
    db_session.flush()
    _, matching_evidence = _add_source_and_evidence(
        db_session,
        matching,
        value={"title": "Matching candidate"},
        suffix="migration-match",
    )
    _, mismatch_evidence = _add_source_and_evidence(
        db_session,
        mismatch,
        value={"title": "Original candidate"},
        suffix="migration-mismatch",
    )
    _, selected_evidence = _add_source_and_evidence(
        db_session,
        selected,
        value={"title": "Accepted title"},
        suffix="migration-selected-candidate",
    )
    _add_source_and_evidence(
        db_session,
        selected,
        value={"title": "Accepted title"},
        review_state="selected",
        suffix="migration-selected",
    )

    migration.upgrade()
    db_session.expire_all()

    assert db_session.get(Competition, matching.id).title is None
    assert db_session.get(Competition, mismatch.id).title == "Manually changed title"
    assert db_session.get(Competition, selected.id).title == "Accepted title"
    assert db_session.get(FieldEvidence, matching_evidence.id) is not None
    assert db_session.get(FieldEvidence, mismatch_evidence.id) is not None
    assert db_session.get(FieldEvidence, selected_evidence.id) is not None


def test_candidate_title_cleanup_migration_downgrade_fails_explicitly():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "6a1c4f2b9d30_clear_unreviewed_candidate_titles.py"
    )
    spec = spec_from_file_location("candidate_title_cleanup_downgrade", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    with pytest.raises(RuntimeError, match="irreversible.*cannot be restored"):
        migration.downgrade()

