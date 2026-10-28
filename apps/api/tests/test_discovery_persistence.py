from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.discovery import DiscoveryCandidate, persist_candidates, persistence
from app.models import Competition, CompetitionSource, FieldEvidence, SourcePublisher


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
    matching_sources = select(CompetitionSource.id).where(
        CompetitionSource.normalized_url == url
    )
    matching_competitions = select(CompetitionSource.competition_id).where(
        CompetitionSource.normalized_url == url
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(CompetitionSource.normalized_url == url)
        )
        == 1
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Competition)
            .where(Competition.id.in_(matching_competitions))
        )
        == 1
    )
    evidence = db_session.scalar(
        select(FieldEvidence).where(FieldEvidence.source_id.in_(matching_sources))
    )
    assert evidence is not None
    assert evidence.observed_value == {
        "title": "Event",
        "source_external_id": "kaggle-event-1",
    }
    assert evidence.review_state == "candidate"
    source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == url)
    )
    assert source is not None
    assert evidence.competition_id == source.competition_id
    assert evidence.source_id == source.id


def test_lock_key_collision_does_not_deduplicate_different_urls(db_session, monkeypatch):
    monkeypatch.setattr(persistence, "_source_url_lock_key", lambda _url: 42)
    urls = [
        "https://events.example.test/2027/collision-a",
        "https://events.example.test/2027/collision-b",
    ]

    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate("Event A", urls[0], "Collision test publisher"),
            DiscoveryCandidate("Event B", urls[1], "Collision test publisher"),
        ],
        {"example.test"},
    )

    assert summary.discovered == 2
    assert summary.existing == 0
    assert summary.failed == 0
    for url in urls:
        assert (
            db_session.scalar(
                select(func.count())
                .select_from(CompetitionSource)
                .where(CompetitionSource.normalized_url == url)
            )
            == 1
        )


def test_concurrent_ingestion_waits_for_caller_transaction_and_counts_existing(monkeypatch):
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for discovery concurrency tests")
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory locks are required for discovery concurrency tests")

    source_url = f"https://events.example.test/2027/concurrent-{uuid4().hex}"
    publisher_name = f"Concurrent discovery test {uuid4().hex}"
    candidate = DiscoveryCandidate(
        "Concurrent event", source_url, publisher_name
    )
    second_backend_ready = Event()
    second_lock_acquired = Event()
    second_backend_pid = []
    capture_second_backend = Event()
    original_acquire_lock = persistence._acquire_source_url_lock

    def observe_lock_attempt(session, normalized_url):
        is_second_attempt = capture_second_backend.is_set() and normalized_url == source_url
        if is_second_attempt:
            backend_pid = session.connection().exec_driver_sql(
                "SELECT pg_backend_pid()"
            ).scalar_one()
            second_backend_pid.append(backend_pid)
            second_backend_ready.set()
        original_acquire_lock(session, normalized_url)
        if is_second_attempt:
            second_lock_acquired.set()

    monkeypatch.setattr(persistence, "_acquire_source_url_lock", observe_lock_attempt)
    caller_session = SessionLocal()
    caller_transaction = caller_session.begin()
    worker_future = None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        first_summary = persist_candidates(
            caller_session, [candidate], {"example.test"}
        )
        assert first_summary.discovered == 1
        assert first_summary.failed == 0

        def ingest_with_separate_session():
            with SessionLocal() as worker_session:
                # Bound the lock wait in case the test fails before it can
                # observe pg_stat_activity and release the first transaction.
                worker_session.execute(text("SET LOCAL lock_timeout = '10s'"))
                return persist_candidates(
                    worker_session, [candidate], {"example.test"}
                )

        capture_second_backend.set()
        worker_future = executor.submit(ingest_with_separate_session)
        assert second_backend_ready.wait(timeout=10)

        # Observe PostgreSQL itself reporting that this independent backend is
        # waiting on the advisory lock held by caller_session. The worker-side
        # event only tells us its backend PID; it is not used as proof of wait.
        wait_deadline = monotonic() + 10
        observed_advisory_wait = False
        while monotonic() < wait_deadline:
            with engine.connect() as observer_connection:
                wait_state = observer_connection.execute(
                    text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :backend_pid"
                    ),
                    {"backend_pid": second_backend_pid[0]},
                ).one_or_none()
            if wait_state == ("Lock", "advisory"):
                observed_advisory_wait = True
                break
            if second_lock_acquired.is_set() or worker_future.done():
                break
            sleep(0.05)
        assert observed_advisory_wait, (
            "second PostgreSQL backend never reported an advisory-lock wait"
        )

        # The caller owns the first transaction, so its lock remains held after
        # persist_candidates returns and is released only by this commit.
        caller_transaction.commit()
        second_summary = worker_future.result(timeout=12)

        assert second_lock_acquired.is_set()
        assert second_summary.discovered == 0
        assert second_summary.existing == 1
        assert second_summary.failed == 0
        with SessionLocal() as verification_session:
            competition_count = verification_session.scalar(
                select(func.count())
                .select_from(Competition)
                .where(
                    Competition.id.in_(
                        select(CompetitionSource.competition_id).where(
                            CompetitionSource.normalized_url == source_url
                        )
                    )
                )
            )
            source_count = verification_session.scalar(
                select(func.count())
                .select_from(CompetitionSource)
                .where(CompetitionSource.normalized_url == source_url)
            )
        assert competition_count == 1
        assert source_count == 1
    finally:
        if caller_transaction.is_active:
            caller_transaction.rollback()
        if worker_future is not None:
            try:
                worker_future.result(timeout=12)
            except Exception:
                pass
        executor.shutdown(wait=True, cancel_futures=True)
        caller_session.close()

        # The two worker transactions commit independently, so remove only this
        # test's uniquely named records after assertions or failure.
        with SessionLocal.begin() as cleanup_session:
            matching_competitions = select(CompetitionSource.competition_id).where(
                CompetitionSource.normalized_url == source_url
            )
            cleanup_session.execute(
                delete(Competition).where(Competition.id.in_(matching_competitions))
            )
            publisher_has_sources = exists(
                select(CompetitionSource.id).where(
                    CompetitionSource.publisher_id == SourcePublisher.id
                )
            )
            cleanup_session.execute(
                delete(SourcePublisher).where(
                    SourcePublisher.name == publisher_name,
                    ~publisher_has_sources,
                )
            )


def test_persistence_requires_postgresql_for_advisory_locking():
    non_postgres_session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            dialect=SimpleNamespace(name="sqlite")
        )
    )

    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        persist_candidates(non_postgres_session, [], {"example.test"})


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
    competition_count_before = db_session.scalar(
        select(func.count()).select_from(Competition)
    )
    source_count_before = db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(
            CompetitionSource.normalized_url
            == "https://events.example.test/2027/existing"
        )
    )

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
    assert (
        db_session.scalar(select(func.count()).select_from(Competition))
        == competition_count_before
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(
                CompetitionSource.normalized_url
                == "https://events.example.test/2027/existing"
            )
        )
        == source_count_before
    )


def test_caller_rollback_removes_persisted_discovery(db_session):
    source_url = "https://events.example.test/2027/rollback"
    caller_transaction = db_session.begin_nested()
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Rollback event",
                source_url,
                "Example Events",
            )
        ],
        {"example.test"},
    )
    assert summary.discovered == 1
    source_competitions = select(CompetitionSource.competition_id).where(
        CompetitionSource.normalized_url == source_url
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Competition)
            .where(Competition.id.in_(source_competitions))
        )
        == 1
    )
    rolled_back_competition_id = db_session.scalar(
        select(CompetitionSource.competition_id).where(
            CompetitionSource.normalized_url == source_url
        )
    )

    caller_transaction.rollback()

    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(CompetitionSource.normalized_url == source_url)
        )
        == 0
    )
    assert db_session.get(Competition, rolled_back_competition_id) is None
    rolled_back_source_ids = select(CompetitionSource.id).where(
        CompetitionSource.normalized_url == source_url
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(FieldEvidence)
            .where(FieldEvidence.source_id.in_(rolled_back_source_ids))
        )
        == 0
    )


def test_failed_candidate_rolls_back_its_rows_and_continues(db_session, monkeypatch, caplog):
    success_url = "https://events.example.test/2027/success"
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
                success_url,
                " Example Events ",
            ),
        ],
        {"example.test"},
    )

    assert summary.failed == 1
    assert "Discovery candidate 1 failed persistence (RuntimeError)" in caplog.text
    assert summary.discovered == 1
    assert summary.existing == 0
    assert db_session.scalar(
        select(Competition.title).where(Competition.title == "Will persist")
    ) == "Will persist"
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(CompetitionSource.normalized_url == success_url)
        )
        == 1
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(FieldEvidence)
            .where(
                FieldEvidence.source_id.in_(
                    select(CompetitionSource.id).where(
                        CompetitionSource.normalized_url == success_url
                    )
                )
            )
        )
        == 1
    )
