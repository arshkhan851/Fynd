from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from integration_database_guard import guard_isolated_postgres_integration_tests
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

guard_isolated_postgres_integration_tests()

from app.auth.models import User  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.discovery import DiscoveryCandidate, persistence  # noqa: E402
from app.discovery import persist_candidates as _persist_candidates  # noqa: E402
from app.discovery.candidates import (  # noqa: E402
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.extraction_persistence import (  # noqa: E402
    persist_extraction_candidate,
)
from app.discovery.orchestration import AuthorizedExtractionResult  # noqa: E402
from app.models import (  # noqa: E402
    Competition,
    CompetitionSource,
    FieldEvidence,
    SourcePublisher,
)
from app.review import service as review_service  # noqa: E402
from app.review.service import accept_evidence  # noqa: E402


@pytest.fixture
def db_session():
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for discovery persistence tests")
    transaction = connection.begin()
    session = Session(bind=connection)
    reviewer = User(email=f"discovery-reviewer-{uuid4().hex}@example.test")
    session.add(reviewer)
    session.flush()
    session.info["reviewer"] = reviewer
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def synthetic_source_authorization(monkeypatch):
    def authorize(source_id, operation):
        prefix = "synthetic-test-source:"
        assert source_id.startswith(prefix)
        assert operation in {"store", "internal_review"}
        return SimpleNamespace(
            id=source_id,
            publisher_name=source_id.removeprefix(prefix),
            source_kind="aggregator",
            access={"allowed_hosts": ["example.test"]},
            use={
                "fields": [
                    "title",
                    "description",
                    "official_url",
                    "registration_url",
                    "registration_deadline",
                    "event_starts",
                    "event_ends",
                    "event_timezone",
                    "format",
                    "eligibility_summary",
                ]
            },
        )

    monkeypatch.setattr(persistence, "authorize_source_operation", authorize)
    monkeypatch.setattr(review_service, "authorize_source_operation", authorize)


def persist_candidates(session, candidates, allowed_hosts):
    candidate_list = list(candidates)
    source_id = "synthetic-test-source:"
    if candidate_list:
        source_id += candidate_list[0].publisher_name.strip()
    return _persist_candidates(
        session,
        candidate_list,
        allowed_hosts,
        registry_source_id=source_id,
    )


def _validated_extraction(source_url, *, publisher_name="Extracted Events"):
    return AuthorizedExtractionResult(
        registry_source_id=f"synthetic-test-source:{publisher_name}",
        source_url=source_url,
        publisher_name=publisher_name,
        source_kind="aggregator",
        allowed_hosts=("example.test",),
        observations={
            "title": TextObservation("Extracted Demo", "The Extracted Demo"),
            "description": TextObservation("A demo event", "A two-day demo event"),
        },
    )


def test_extraction_persists_unpublished_candidate_and_all_evidence(db_session):
    source_url = "https://events.example.test/2027/extracted"

    summary = persist_extraction_candidate(
        db_session, _validated_extraction(source_url)
    )

    assert summary.discovered == 1
    assert summary.failed == summary.existing == summary.skipped == 0
    source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == source_url)
    )
    assert source is not None
    assert source.registry_source_id == "synthetic-test-source:Extracted Events"
    competition = db_session.get(Competition, source.competition_id)
    assert competition is not None
    assert competition.is_published is False
    assert competition.title is None
    assert competition.description is None

    evidence = db_session.scalars(
        select(FieldEvidence).where(FieldEvidence.source_id == source.id)
    ).all()
    by_field = {item.field_name: item for item in evidence}
    assert set(by_field) == {"title", "description"}
    assert by_field["title"].observed_value == {"title": "Extracted Demo"}
    assert by_field["title"].evidence_excerpt == "The Extracted Demo"
    assert by_field["description"].observed_value == {"description": "A demo event"}
    assert by_field["description"].evidence_excerpt == "A two-day demo event"
    assert all(item.review_state == "candidate" for item in evidence)


def test_extraction_persistence_retains_duplicate_url_behavior(db_session):
    source_url = "https://events.example.test/2027/extracted-duplicate"
    result = _validated_extraction(source_url)

    first = persist_extraction_candidate(db_session, result)
    second = persist_extraction_candidate(db_session, result)

    assert first.discovered == 1
    assert second.existing == 1
    assert second.failed == 0
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(CompetitionSource.normalized_url == source_url)
        )
        == 1
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(FieldEvidence)
            .where(FieldEvidence.source_id.in_(
                select(CompetitionSource.id).where(
                    CompetitionSource.normalized_url == source_url
                )
            ))
        )
        == 2
    )


def test_extraction_persistence_rollback_removes_partial_candidate(
    db_session, monkeypatch
):
    source_url = "https://events.example.test/2027/extracted-rollback"
    original_flush = db_session.flush
    partial_competition_ids = []

    def fail_when_evidence_is_pending(objects=None):
        pending_evidence = [
            row for row in db_session.new if isinstance(row, FieldEvidence)
        ]
        if pending_evidence:
            partial_competition_ids.extend(row.competition_id for row in pending_evidence)
            raise RuntimeError("simulated extracted evidence insert failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_when_evidence_is_pending)
    summary = persist_extraction_candidate(
        db_session, _validated_extraction(source_url)
    )

    assert summary.failed == 1
    assert summary.discovered == summary.existing == 0
    assert partial_competition_ids
    assert all(db_session.get(Competition, item) is None for item in partial_competition_ids)
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == source_url)
    ) == 0
    assert db_session.scalar(
        select(func.count())
        .select_from(FieldEvidence)
        .where(FieldEvidence.source_id.in_(
            select(CompetitionSource.id).where(
                CompetitionSource.normalized_url == source_url
            )
        ))
    ) == 0


def test_extraction_persistence_keeps_field_scope_filtering(db_session, monkeypatch):
    def title_only_authorization(source_id, operation):
        assert operation in {"store", "internal_review"}
        return SimpleNamespace(
            id=source_id,
            publisher_name="Extracted Events",
            source_kind="aggregator",
            access={"allowed_hosts": ["example.test"]},
            use={"fields": ["title"]},
        )

    monkeypatch.setattr(persistence, "authorize_source_operation", title_only_authorization)
    source_url = "https://events.example.test/2027/extracted-title-only"
    summary = persist_extraction_candidate(
        db_session, _validated_extraction(source_url)
    )

    assert summary.discovered == 1
    assert summary.skipped == 1
    assert summary.skipped_fields == ("description",)
    source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == source_url)
    )
    assert source is not None
    evidence = db_session.scalars(
        select(FieldEvidence).where(FieldEvidence.source_id == source.id)
    ).all()
    assert [item.field_name for item in evidence] == ["title"]
    assert evidence[0].evidence_excerpt == "The Extracted Demo"


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
    competition_id = db_session.scalar(
        select(CompetitionSource.competition_id).where(
            CompetitionSource.normalized_url == url
        )
    )
    assert db_session.get(Competition, competition_id).title is None
    assert db_session.get(Competition, competition_id).is_published is False
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
    assert source.registry_source_id == "synthetic-test-source:Example Events"
    assert evidence.competition_id == source.competition_id
    assert evidence.source_id == source.id


def test_manual_import_creates_unpublished_competition(db_session):
    source_url = "https://events.example.test/2027/private-import"
    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Imported event", source_url, "Example Events")],
        {"example.test"},
    )

    assert summary.discovered == 1
    competition_id = db_session.scalar(
        select(CompetitionSource.competition_id).where(
            CompetitionSource.normalized_url == source_url
        )
    )
    competition = db_session.get(Competition, competition_id)
    assert competition is not None
    assert competition.title is None
    assert competition.is_published is False


def test_structured_observations_persist_as_candidate_evidence_only(db_session):
    source_url = "https://events.example.test/2027/structured-observations"
    candidate = DiscoveryCandidate(
        "Structured event",
        source_url,
        "Structured Events",
        description=TextObservation("Event details", "Description excerpt"),
        official_url=UrlObservation("https://official.example.test/event", "Official excerpt"),
        registration_url=UrlObservation(
            "https://official.example.test/register", "Registration excerpt"
        ),
        registration_deadline=DateObservationField(
            DateOnlyObservation(date(2027, 6, 10)), "Deadline excerpt"
        ),
        event_starts=DateObservationField(
            DateTimeObservation(datetime(2027, 6, 11, 9, 30, tzinfo=UTC)), "Start excerpt"
        ),
        event_ends=DateObservationField(DateOnlyObservation(date(2027, 6, 13)), "End excerpt"),
        event_timezone=TextObservation("Asia/Kolkata", "Timezone excerpt"),
        format=FormatObservation("hybrid", "Format excerpt"),
        eligibility_summary=TextObservation("Open to students", "Eligibility excerpt"),
    )

    summary = persist_candidates(db_session, [candidate], {"example.test"})

    assert summary.discovered == 1
    assert summary.skipped == 0
    assert summary.failed == 0
    source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == source_url)
    )
    assert source is not None
    competition = db_session.get(Competition, source.competition_id)
    assert competition is not None
    assert competition.is_published is False
    assert competition.title is None
    assert competition.description is None
    assert competition.official_url is None
    assert competition.registration_url is None
    assert competition.registration_deadline_on is None
    assert competition.registration_deadline_at is None
    assert competition.event_starts_on is None
    assert competition.event_starts_at is None
    assert competition.event_ends_on is None
    assert competition.event_ends_at is None
    assert competition.event_timezone is None
    assert competition.format is None
    assert competition.eligibility_summary is None

    evidence_rows = db_session.scalars(
        select(FieldEvidence)
        .where(FieldEvidence.source_id == source.id)
        .order_by(FieldEvidence.field_name)
    ).all()
    by_field = {row.field_name: row for row in evidence_rows}
    assert set(by_field) == {
        "title",
        "description",
        "official_url",
        "registration_url",
        "registration_deadline",
        "event_starts",
        "event_ends",
        "event_timezone",
        "format",
        "eligibility_summary",
    }
    expected_values = {
        "title": {"title": "Structured event"},
        "description": {"description": "Event details"},
        "official_url": {"official_url": "https://official.example.test/event"},
        "registration_url": {"registration_url": "https://official.example.test/register"},
        "registration_deadline": {"registration_deadline": {"on": "2027-06-10"}},
        "event_starts": {"event_starts": {"at": "2027-06-11T09:30:00+00:00"}},
        "event_ends": {"event_ends": {"on": "2027-06-13"}},
        "event_timezone": {"event_timezone": "Asia/Kolkata"},
        "format": {"format": "hybrid"},
        "eligibility_summary": {"eligibility_summary": "Open to students"},
    }
    expected_excerpts = {
        "title": "Structured event",
        "description": "Description excerpt",
        "official_url": "Official excerpt",
        "registration_url": "Registration excerpt",
        "registration_deadline": "Deadline excerpt",
        "event_starts": "Start excerpt",
        "event_ends": "End excerpt",
        "event_timezone": "Timezone excerpt",
        "format": "Format excerpt",
        "eligibility_summary": "Eligibility excerpt",
    }
    for field_name, row in by_field.items():
        assert row.competition_id == competition.id
        assert row.source_id == source.id
        assert row.field_key == ""
        assert row.observed_value == expected_values[field_name]
        assert row.evidence_excerpt == expected_excerpts[field_name]
        assert row.review_state == "candidate"


def test_observations_outside_registry_field_scope_are_reported_and_not_persisted(
    db_session, monkeypatch
):
    source_url = "https://events.example.test/2027/scoped-observations"

    def authorize(source_id, operation):
        assert source_id == "synthetic-test-source:Scoped Events"
        assert operation in {"store", "internal_review"}
        return SimpleNamespace(
            id=source_id,
            publisher_name="Scoped Events",
            source_kind="aggregator",
            access={"allowed_hosts": ["example.test"]},
            use={"fields": ["title", "description"]},
        )

    monkeypatch.setattr(persistence, "authorize_source_operation", authorize)
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Scoped event",
                source_url,
                "Scoped Events",
                description=TextObservation("Allowed description"),
                format=FormatObservation("online"),
            )
        ],
        {"example.test"},
    )

    assert summary.discovered == 1
    assert summary.skipped == 1
    assert summary.skipped_fields == ("format",)
    source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == source_url)
    )
    assert source is not None
    evidence = db_session.scalars(
        select(FieldEvidence).where(FieldEvidence.source_id == source.id)
    ).all()
    assert {row.field_name for row in evidence} == {"title", "description"}


def test_accepting_imported_title_evidence_does_not_publish_competition(db_session):
    source_url = "https://events.example.test/2027/imported-title-review"
    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Reviewed imported event", source_url, "Example Events")],
        {"example.test"},
    )
    assert summary.discovered == 1
    evidence = db_session.scalar(
        select(FieldEvidence).join(CompetitionSource).where(
            CompetitionSource.normalized_url == source_url,
            FieldEvidence.field_name == "title",
        )
    )
    competition = db_session.scalar(
        select(Competition).join(CompetitionSource).where(
            CompetitionSource.normalized_url == source_url
        )
    )
    assert evidence is not None
    assert competition is not None
    assert competition.is_published is False

    accept_evidence(
        db_session,
        evidence.id,
        reviewer=db_session.info["reviewer"],
        commit=False,
    )

    db_session.refresh(competition)
    assert competition.title == "Reviewed imported event"
    assert competition.is_published is False


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
        persist_candidates(
            non_postgres_session,
            [DiscoveryCandidate("Event", "https://events.example.test/event", "Publisher")],
            {"example.test"},
        )


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
    existing_source = db_session.scalar(
        select(CompetitionSource).where(
            CompetitionSource.normalized_url == "https://events.example.test/2027/existing"
        )
    )
    assert existing_source is not None
    assert existing_source.registry_source_id is None


def test_existing_source_with_conflicting_registry_identity_is_not_reused(db_session):
    competition = Competition(title="Existing trusted event")
    db_session.add(competition)
    db_session.flush()
    source_url = "https://events.example.test/2027/conflicting-source"
    existing_source = CompetitionSource(
        competition_id=competition.id,
        source_kind="aggregator",
        source_url=source_url,
        normalized_url=source_url,
        registry_source_id="different-trusted-source",
    )
    db_session.add(existing_source)
    db_session.flush()

    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Repeated event", source_url, "Example Events")],
        {"example.test"},
    )

    assert summary.failed == 1
    assert summary.existing == 0
    assert summary.discovered == 0
    db_session.refresh(existing_source)
    assert existing_source.registry_source_id == "different-trusted-source"
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == source_url)
    ) == 1


def _add_existing_source(db_session, source_url, registry_source_id):
    competition = Competition(title="Existing source record")
    db_session.add(competition)
    db_session.flush()
    source = CompetitionSource(
        competition_id=competition.id,
        source_kind="aggregator",
        source_url=source_url,
        normalized_url=source_url,
        registry_source_id=registry_source_id,
    )
    db_session.add(source)
    db_session.flush()
    return competition, source


def test_all_matching_source_rows_are_deduplicated(db_session):
    source_url = f"https://events.example.test/2027/matching-rows-{uuid4().hex}"
    trusted_id = "synthetic-test-source:Example Events"
    _, first = _add_existing_source(db_session, source_url, trusted_id)
    _, second = _add_existing_source(db_session, source_url, trusted_id)
    counts_before = (
        db_session.scalar(select(func.count()).select_from(Competition)),
        db_session.scalar(select(func.count()).select_from(CompetitionSource)),
        db_session.scalar(select(func.count()).select_from(FieldEvidence)),
    )

    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Repeated event", source_url, "Example Events")],
        {"example.test"},
    )

    assert (summary.existing, summary.failed, summary.discovered) == (1, 0, 0)
    assert first.registry_source_id == second.registry_source_id == trusted_id
    assert (
        db_session.scalar(select(func.count()).select_from(Competition)),
        db_session.scalar(select(func.count()).select_from(CompetitionSource)),
        db_session.scalar(select(func.count()).select_from(FieldEvidence)),
    ) == counts_before


def test_matching_and_null_source_rows_are_deduplicated_without_backfill(db_session):
    source_url = f"https://events.example.test/2027/mixed-null-rows-{uuid4().hex}"
    trusted_id = "synthetic-test-source:Example Events"
    _, matching = _add_existing_source(db_session, source_url, trusted_id)
    _, legacy_null = _add_existing_source(db_session, source_url, None)

    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Repeated event", source_url, "Example Events")],
        {"example.test"},
    )

    assert (summary.existing, summary.failed, summary.discovered) == (1, 0, 0)
    db_session.refresh(matching)
    db_session.refresh(legacy_null)
    assert matching.registry_source_id == trusted_id
    assert legacy_null.registry_source_id is None
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == source_url)
    ) == 2


def test_conflict_in_later_matching_url_row_fails_candidate_without_writes(db_session):
    source_url = f"https://events.example.test/2027/later-conflict-{uuid4().hex}"
    trusted_id = "synthetic-test-source:Example Events"
    _, matching_first = _add_existing_source(db_session, source_url, trusted_id)
    _, conflicting_second = _add_existing_source(
        db_session, source_url, "different-trusted-source"
    )
    competition_count_before = db_session.scalar(
        select(func.count()).select_from(Competition)
    )
    source_count_before = db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == source_url)
    )

    summary = persist_candidates(
        db_session,
        [DiscoveryCandidate("Repeated event", source_url, "Example Events")],
        {"example.test"},
    )

    assert (summary.existing, summary.failed, summary.discovered) == (0, 1, 0)
    db_session.refresh(matching_first)
    db_session.refresh(conflicting_second)
    assert matching_first.registry_source_id == trusted_id
    assert conflicting_second.registry_source_id == "different-trusted-source"
    assert db_session.scalar(select(func.count()).select_from(Competition)) == (
        competition_count_before
    )
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == source_url)
    ) == source_count_before
    assert db_session.scalar(
        select(func.count())
        .select_from(FieldEvidence)
        .where(
            FieldEvidence.source_id.in_(
                select(CompetitionSource.id).where(
                    CompetitionSource.normalized_url == source_url
                )
            )
        )
    ) == 0


def test_conflicting_candidate_does_not_prevent_later_batch_candidate(db_session):
    conflict_url = f"https://events.example.test/2027/batch-conflict-{uuid4().hex}"
    success_url = f"https://events.example.test/2027/batch-success-{uuid4().hex}"
    _add_existing_source(db_session, conflict_url, "different-trusted-source")

    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate("Conflicting event", conflict_url, "Example Events"),
            DiscoveryCandidate("Independent event", success_url, "Example Events"),
        ],
        {"example.test"},
    )

    assert (summary.failed, summary.discovered, summary.existing) == (1, 1, 0)
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == conflict_url)
    ) == 1
    successful_source = db_session.scalar(
        select(CompetitionSource).where(CompetitionSource.normalized_url == success_url)
    )
    assert successful_source is not None
    assert successful_source.registry_source_id == "synthetic-test-source:Example Events"
    assert db_session.scalar(
        select(func.count())
        .select_from(FieldEvidence)
        .where(FieldEvidence.source_id == successful_source.id)
    ) == 1


def test_concurrent_same_url_different_registry_id_fails_second_candidate(
    monkeypatch,
):
    source_url = f"https://events.example.test/2027/identity-race-{uuid4().hex}"
    first_publisher = f"Concurrent source A {uuid4().hex}"
    second_publisher = f"Concurrent source B {uuid4().hex}"
    first_registry_id = f"synthetic-test-source:{first_publisher}"
    second_registry_id = f"synthetic-test-source:{second_publisher}"
    first_candidate = DiscoveryCandidate("First event", source_url, first_publisher)
    second_candidate = DiscoveryCandidate("Second event", source_url, second_publisher)
    second_backend_ready = Event()
    second_lock_acquired = Event()
    capture_second_backend = Event()
    second_backend_pid = []
    original_acquire_lock = persistence._acquire_source_url_lock

    def observe_lock_attempt(session, normalized_url):
        is_second_attempt = capture_second_backend.is_set() and normalized_url == source_url
        if is_second_attempt:
            second_backend_pid.append(
                session.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            )
            second_backend_ready.set()
        original_acquire_lock(session, normalized_url)
        if is_second_attempt:
            second_lock_acquired.set()

    monkeypatch.setattr(persistence, "_acquire_source_url_lock", observe_lock_attempt)
    caller_session = SessionLocal()
    caller_transaction = caller_session.begin()
    executor = ThreadPoolExecutor(max_workers=1)
    worker_future = None
    try:
        first_summary = _persist_candidates(
            caller_session,
            [first_candidate],
            {"example.test"},
            registry_source_id=first_registry_id,
        )
        assert (first_summary.discovered, first_summary.failed) == (1, 0)

        def persist_with_second_identity():
            with SessionLocal() as worker_session:
                worker_session.execute(text("SET LOCAL lock_timeout = '10s'"))
                return _persist_candidates(
                    worker_session,
                    [second_candidate],
                    {"example.test"},
                    registry_source_id=second_registry_id,
                )

        capture_second_backend.set()
        worker_future = executor.submit(persist_with_second_identity)
        assert second_backend_ready.wait(timeout=10)

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
        assert observed_advisory_wait, "second backend did not wait on the URL advisory lock"

        caller_transaction.commit()
        second_summary = worker_future.result(timeout=12)

        assert second_lock_acquired.is_set()
        assert (second_summary.discovered, second_summary.existing, second_summary.failed) == (
            0,
            0,
            1,
        )
        with SessionLocal() as verification_session:
            sources = verification_session.scalars(
                select(CompetitionSource).where(
                    CompetitionSource.normalized_url == source_url
                )
            ).all()
            evidence_count = verification_session.scalar(
                select(func.count())
                .select_from(FieldEvidence)
                .where(FieldEvidence.source_id.in_(select(CompetitionSource.id).where(
                    CompetitionSource.normalized_url == source_url
                )))
            )
        assert len(sources) == 1
        assert sources[0].registry_source_id == first_registry_id
        assert evidence_count == 1
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
                    SourcePublisher.name.in_([first_publisher, second_publisher]),
                    ~publisher_has_sources,
                )
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
    failure_url = "https://events.example.test/2027/failure"
    success_url = "https://events.example.test/2027/success"
    original_flush = db_session.flush
    failed_competition_ids = []

    def fail_on_first_source_flush(objects=None):
        pending_sources = [
            row
            for row in db_session.new
            if isinstance(row, CompetitionSource) and row.normalized_url == failure_url
        ]
        if pending_sources:
            failed_competition_ids.extend(row.competition_id for row in pending_sources)
            raise RuntimeError("simulated source insert failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_on_first_source_flush)
    summary = persist_candidates(
        db_session,
        [
            DiscoveryCandidate(
                "Will roll back",
                failure_url,
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
    assert len(failed_competition_ids) == 1
    assert db_session.get(Competition, failed_competition_ids[0]) is None
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionSource)
        .where(CompetitionSource.normalized_url == failure_url)
    ) == 0
    assert db_session.scalar(
        select(func.count())
        .select_from(FieldEvidence)
        .where(
            FieldEvidence.source_id.in_(
                select(CompetitionSource.id).where(
                    CompetitionSource.normalized_url == failure_url
                )
            )
        )
    ) == 0
    success_competition_id = db_session.scalar(
        select(CompetitionSource.competition_id).where(
            CompetitionSource.normalized_url == success_url
        )
    )
    assert db_session.get(Competition, success_competition_id).title is None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompetitionSource)
            .where(CompetitionSource.normalized_url == success_url)
        )
        == 1
    )
    success_evidence = db_session.scalar(
        select(FieldEvidence).where(
            FieldEvidence.source_id.in_(
                select(CompetitionSource.id).where(
                    CompetitionSource.normalized_url == success_url
                )
            )
        )
    )
    assert success_evidence is not None
    assert success_evidence.field_name == "title"
    assert success_evidence.observed_value["title"] == "Will persist"
    assert success_evidence.review_state == "candidate"
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
