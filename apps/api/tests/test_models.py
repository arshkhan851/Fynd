from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionOrganizer,
    CompetitionPrize,
    CompetitionSource,
    CompetitionTopic,
    DuplicateCandidate,
    FieldEvidence,
    Organizer,
    SourcePublisher,
    Topic,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db_session():
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for model integration tests")

    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_models_create_relationships_and_preserve_unknown_values(db_session):
    competition = Competition(
        title=None,
        description=None,
        eligibility_summary=None,
        format="online",
        event_starts_on=date(2027, 5, 2),
        event_timezone="America/New_York",
    )
    organizer = Organizer(name="Fynd Community")
    category = Category(name="Hackathon", slug="hackathon")
    topic = Topic(name="Climate Tech", slug="climate-tech")
    publisher = SourcePublisher(name="Official organizer")
    db_session.add_all([competition, organizer, category, topic, publisher])
    db_session.flush()

    competition.organizer_links.append(
        CompetitionOrganizer(organizer=organizer, role="host")
    )
    competition.category_links.append(
        CompetitionCategory(category=category, is_primary=True)
    )
    competition.topic_links.append(CompetitionTopic(topic=topic))
    competition.prizes.append(
        CompetitionPrize(award_name="Grand prize", details="Amount not published")
    )
    source = CompetitionSource(
        competition_id=competition.id,
        publisher_id=publisher.id,
        source_kind="official",
        source_url="https://example.test/competition",
        normalized_url="https://example.test/competition",
        check_status="reachable",
    )
    db_session.add(source)
    db_session.flush()

    evidence = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name="title",
        observed_value={"title": "not stated"},
        review_state="candidate",
    )
    source.evidence.append(evidence)
    db_session.flush()

    loaded = db_session.scalar(select(Competition).where(Competition.id == competition.id))
    assert loaded is not None
    assert loaded.title is None
    assert loaded.registration_deadline_at is None
    assert loaded.registration_deadline_on is None
    assert loaded.organizer_links[0].organizer.name == "Fynd Community"
    assert loaded.category_links[0].category.slug == "hackathon"
    assert loaded.topic_links[0].topic.slug == "climate-tech"
    assert loaded.prizes[0].amount is None
    assert loaded.sources[0].evidence[0].observed_value == {"title": "not stated"}


def test_conflicting_source_values_are_retained(db_session):
    competition = Competition(title="Official title")
    db_session.add(competition)
    db_session.flush()
    sources = [
        CompetitionSource(
            competition_id=competition.id,
            source_kind="official",
            source_url=f"https://example.test/source-{index}",
            normalized_url=f"https://example.test/source-{index}",
        )
        for index in (1, 2)
    ]
    db_session.add_all(sources)
    db_session.flush()
    db_session.add_all(
        [
            FieldEvidence(
                competition_id=competition.id,
                source_id=source.id,
                field_name="title",
                observed_value={"title": title},
                review_state=state,
            )
            for source, title, state in zip(
                sources,
                ("Official title", "Conflicting title"),
                ("selected", "candidate"),
                strict=True,
            )
        ]
    )
    db_session.flush()

    evidence = db_session.scalars(
        select(FieldEvidence).where(FieldEvidence.competition_id == competition.id)
    ).all()
    assert len(evidence) == 2
    assert {row.observed_value["title"] for row in evidence} == {
        "Official title",
        "Conflicting title",
    }
    assert len({row.source_id for row in evidence}) == 2


def test_selected_evidence_is_unique_per_competition_field(db_session):
    competition = Competition(title="Current title")
    db_session.add(competition)
    db_session.flush()
    sources = [
        CompetitionSource(
            competition_id=competition.id,
            source_kind="official",
            source_url=f"https://example.test/selected-{index}",
            normalized_url=f"https://example.test/selected-{index}",
        )
        for index in (1, 2)
    ]
    db_session.add_all(sources)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add_all(
                [
                    FieldEvidence(
                        competition_id=competition.id,
                        source_id=source.id,
                        field_name="title",
                        observed_value={"title": f"Title {index}"},
                        review_state="selected",
                    )
                    for index, source in enumerate(sources)
                ]
            )
            db_session.flush()


def test_competition_rejects_inconsistent_date_precision(db_session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                Competition(
                    registration_deadline_at=datetime.fromisoformat(
                        "2027-04-01T17:00:00+00:00"
                    ),
                    registration_deadline_on=date(2027, 4, 1),
                )
            )
            db_session.flush()


def test_duplicate_candidate_requires_review_and_does_not_merge(db_session):
    first = Competition(title="Competition A")
    second = Competition(title="Competition B")
    db_session.add_all([first, second])
    db_session.flush()
    lower_id, higher_id = sorted((first.id, second.id))
    candidate = DuplicateCandidate(
        competition_a_id=lower_id,
        competition_b_id=higher_id,
        score=0.95,
        matching_signals={"title_similarity": 0.95},
        algorithm_version="test-v1",
        review_state="confirmed_same",
    )
    db_session.add(candidate)
    db_session.flush()

    assert db_session.get(Competition, first.id) is not None
    assert db_session.get(Competition, second.id) is not None
    assert db_session.get(DuplicateCandidate, (lower_id, higher_id)).review_state == (
        "confirmed_same"
    )
