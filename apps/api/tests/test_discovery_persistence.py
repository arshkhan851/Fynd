import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.discovery import DiscoveryCandidate, persist_candidates
from app.models import Competition, CompetitionSource, FieldEvidence


@pytest.fixture
def db_session():
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for discovery persistence tests")
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


pytestmark = pytest.mark.integration


def test_repeat_source_urls_are_idempotent(db_session):
    url = "https://events.example.test/2027/event"
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Event", f"{url}?utm_source=one", "Example Events", external_id="kaggle-event-1"
            ),
            DiscoveryCandidate("Event duplicate", url, "Example Events"),
        ],
        {"example.test"},
    )

    assert summary.discovered == 1
    assert summary.existing == 1
    assert summary.failed == 0
    assert db_session.scalar(select(func.count()).select_from(Competition)) == 1
    assert db_session.scalar(select(func.count()).select_from(CompetitionSource)) == 1
    evidence = db_session.scalar(select(FieldEvidence))
    assert evidence is not None
    assert evidence.observed_value == {
        "title": "Event",
        "source_external_id": "kaggle-event-1",
    }
    assert evidence.review_state == "candidate"
    source = db_session.scalar(select(CompetitionSource))
    assert source is not None
    assert evidence.competition_id == source.competition_id
    assert evidence.source_id == source.id


def test_existing_source_url_on_another_competition_is_not_duplicated(db_session):
    competition = Competition(title="Preexisting event")
    db_session.add(competition)
    db_session.flush()
    db_session.add(
        CompetitionSource(
            competition_id=competition.id,
            source_kind="aggregator",
            source_url="https://events.example.test/2027/existing",
            normalized_url="https://events.example.test/2027/existing",
        )
    )
    db_session.flush()

    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Different listing title",
                "https://events.example.test/2027/existing?utm_campaign=repeat",
                "Example Events",
            )
        ],
        {"example.test"},
    )

    assert summary.existing == 1
    assert summary.discovered == 0
    assert db_session.scalar(select(func.count()).select_from(Competition)) == 1


def test_caller_rollback_removes_persisted_discovery(db_session):
    caller_transaction = db_session.begin_nested()
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Rollback event",
                "https://events.example.test/2027/rollback",
                "Example Events",
            )
        ],
        {"example.test"},
    )
    assert summary.discovered == 1
    assert db_session.scalar(select(func.count()).select_from(Competition)) == 1

    caller_transaction.rollback()

    assert db_session.scalar(select(func.count()).select_from(Competition)) == 0
    assert db_session.scalar(select(func.count()).select_from(CompetitionSource)) == 0
    assert db_session.scalar(select(func.count()).select_from(FieldEvidence)) == 0


def test_failed_candidate_rolls_back_its_rows_and_continues(db_session, monkeypatch, caplog):
    original_flush = db_session.flush
    flush_count = 0

    def fail_on_first_source_flush(objects=None):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 4:
            raise RuntimeError("simulated source insert failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_on_first_source_flush)
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Will roll back",
                "https://events.example.test/2027/failure",
                "Example Events",
            ),
            DiscoveryCandidate(
                "Will persist",
                "https://events.example.test/2027/success",
                " Example Events ",
            ),
        ],
        {"example.test"},
    )

    assert summary.failed == 1
    assert "Discovery candidate 1 failed persistence (RuntimeError)" in caplog.text
    assert summary.discovered == 1
    assert summary.existing == 0
    assert db_session.scalar(select(func.count()).select_from(Competition)) == 1
    assert db_session.scalar(select(Competition.title)) == "Will persist"
    assert db_session.scalar(select(func.count()).select_from(CompetitionSource)) == 1
    assert db_session.scalar(select(func.count()).select_from(FieldEvidence)) == 1
