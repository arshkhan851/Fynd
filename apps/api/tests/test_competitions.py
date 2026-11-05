from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionOrganizer,
    CompetitionPrize,
    CompetitionSource,
    FieldEvidence,
    Organizer,
    SourcePublisher,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for competition API tests")

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _published_competition(**values) -> Competition:
    return Competition(is_published=True, **values)


def _add_competitions(session: Session) -> tuple[int, int, int]:
    hidden_without_title = Competition(title=None)
    hidden_candidate = Competition(title=None)
    second_visible = _published_competition(title="Visible competition B")
    first_visible = _published_competition(title="Visible competition A")
    session.add_all(
        [hidden_without_title, hidden_candidate, second_visible, first_visible]
    )
    session.flush()

    source = CompetitionSource(
        competition_id=hidden_candidate.id,
        source_kind="aggregator",
        source_url="https://competition.test/candidate-only",
        normalized_url="https://competition.test/candidate-only",
    )
    session.add(source)
    session.flush()
    session.add(
        FieldEvidence(
            competition_id=hidden_candidate.id,
            source_id=source.id,
            field_name="title",
            observed_value={"title": "Candidate only title"},
            review_state="candidate",
        )
    )
    session.flush()
    return hidden_candidate.id, first_visible.id, second_visible.id


def test_competitions_only_returns_canonical_titles_and_is_ordered(db_session, client):
    candidate_id, first_id, second_id = _add_competitions(db_session)

    response = client.get("/competitions", params={"limit": 100})

    assert response.status_code == 200
    payload = response.json()
    ids = [item["id"] for item in payload["items"]]
    expected_ids = db_session.scalars(
        select(Competition.id)
        .where(Competition.is_published.is_(True), Competition.title.is_not(None))
        .order_by(Competition.discovered_at.desc(), Competition.id.desc())
        .limit(100)
    ).all()
    assert candidate_id not in ids
    assert first_id in ids
    assert second_id in ids
    assert all(item["title"] is not None for item in payload["items"])
    assert ids == expected_ids
    assert payload["total"] >= len(ids)
    assert set(payload["items"][ids.index(first_id)]) == {
        "id",
        "title",
        "format",
        "official_url",
        "event_starts_on",
        "event_starts_at",
        "registration_deadline_on",
        "registration_deadline_at",
    }


def test_unpublished_competition_is_excluded_from_list_and_detail(db_session, client):
    unpublished = Competition(title="Reviewed but unpublished")
    db_session.add(unpublished)
    db_session.flush()

    listed = client.get("/competitions", params={"q": "Reviewed but unpublished"})
    detailed = client.get(f"/competitions/{unpublished.id}")

    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert listed.json()["total"] == 0
    assert detailed.status_code == 404


def test_competitions_pagination_defaults_and_limits(db_session, client):
    _add_competitions(db_session)

    default_response = client.get("/competitions")
    first_page = client.get("/competitions", params={"limit": 2, "offset": 0})
    second_page = client.get("/competitions", params={"limit": 2, "offset": 2})
    full_page = client.get("/competitions", params={"limit": 100})

    assert default_response.status_code == 200
    assert default_response.json()["limit"] == 20
    assert default_response.json()["offset"] == 0
    assert first_page.status_code == second_page.status_code == full_page.status_code == 200
    all_ids = [item["id"] for item in full_page.json()["items"]]
    assert [item["id"] for item in first_page.json()["items"]] == all_ids[:2]
    assert [item["id"] for item in second_page.json()["items"]] == all_ids[2:4]
    assert first_page.json()["total"] == full_page.json()["total"]


def test_competitions_search_matches_title_and_description_case_insensitively(db_session, client):
    title_match = _published_competition(title="Quantum Sprint Challenge")
    description_match = _published_competition(
        title="Other challenge", description="Build tools for renewable energy"
    )
    db_session.add_all([title_match, description_match])
    db_session.flush()

    title_response = client.get("/competitions", params={"q": "sPrInT"})
    description_response = client.get("/competitions", params={"q": "RENEWABLE"})

    assert [item["id"] for item in title_response.json()["items"]] == [title_match.id]
    assert [item["id"] for item in description_response.json()["items"]] == [description_match.id]


def test_competitions_search_trims_whitespace_and_empty_query_is_unfiltered(db_session, client):
    match = _published_competition(title="Whitespace Search Match")
    db_session.add(match)
    db_session.flush()

    trimmed = client.get("/competitions", params={"q": "  Search Match  "})
    empty = client.get("/competitions", params={"q": ""})
    whitespace = client.get("/competitions", params={"q": "   "})
    unfiltered = client.get("/competitions")

    assert [item["id"] for item in trimmed.json()["items"]] == [match.id]
    assert empty.json() == unfiltered.json()
    assert whitespace.json() == unfiltered.json()


def test_competitions_search_no_match_and_excludes_candidate_evidence(db_session, client):
    hidden = Competition(title=None)
    db_session.add(hidden)
    db_session.flush()
    source = CompetitionSource(
        competition_id=hidden.id,
        source_kind="aggregator",
        source_url="https://competition.test/evidence-search",
        normalized_url="https://competition.test/evidence-search",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        FieldEvidence(
            competition_id=hidden.id,
            source_id=source.id,
            field_name="title",
            observed_value={"title": "EvidenceOnlyNeedle"},
            review_state="candidate",
        )
    )
    db_session.flush()

    evidence_response = client.get("/competitions", params={"q": "EvidenceOnlyNeedle"})
    missing_response = client.get("/competitions", params={"q": "NoSuchCompetitionNeedle"})

    assert evidence_response.json()["items"] == []
    assert evidence_response.json()["total"] == 0
    assert missing_response.json()["items"] == []
    assert missing_response.json()["total"] == 0


def test_competitions_search_total_is_before_pagination_and_preserves_order(db_session, client):
    first = _published_competition(title="Searchable first")
    second = _published_competition(title="Searchable second")
    third = _published_competition(title="Searchable third")
    unrelated = _published_competition(title="Unrelated")
    db_session.add_all([first, second, third, unrelated])
    db_session.flush()

    all_matches = client.get("/competitions", params={"q": "Searchable", "limit": 100})
    page = client.get("/competitions", params={"q": "Searchable", "limit": 1, "offset": 1})

    assert all_matches.json()["total"] == 3
    assert page.json()["total"] == 3
    assert page.json()["limit"] == 1
    assert page.json()["offset"] == 1
    assert page.json()["items"] == all_matches.json()["items"][1:2]


def test_competitions_filter_by_category_slug_and_unknown_category(db_session, client):
    category = Category(name="AI Challenges", slug=f"ai-{uuid4().hex}")
    match = _published_competition(title="AI competition")
    unrelated = _published_competition(title="Other competition")
    db_session.add_all([category, match, unrelated])
    db_session.flush()
    db_session.add(
        CompetitionCategory(competition_id=match.id, category_id=category.id)
    )
    db_session.flush()

    response = client.get("/competitions", params={"category": category.slug})
    unknown = client.get("/competitions", params={"category": "missing-category"})

    assert [item["id"] for item in response.json()["items"]] == [match.id]
    assert response.json()["total"] == 1
    assert unknown.status_code == 200
    assert unknown.json()["items"] == []
    assert unknown.json()["total"] == 0


def test_competitions_category_combines_with_search_and_pagination(db_session, client):
    category = Category(name="Climate", slug=f"climate-{uuid4().hex}")
    matches = [_published_competition(title=f"Climate Spark {index}") for index in range(3)]
    nonmatch = _published_competition(title="Climate unrelated")
    db_session.add_all([category, *matches, nonmatch])
    db_session.flush()
    db_session.add_all(
        CompetitionCategory(competition_id=item.id, category_id=category.id)
        for item in matches
    )
    db_session.flush()

    complete = client.get(
        "/competitions", params={"category": category.slug, "q": "spark", "limit": 100}
    )
    page = client.get(
        "/competitions",
        params={"category": category.slug, "q": "spark", "limit": 1, "offset": 1},
    )

    assert complete.json()["total"] == 3
    assert page.json()["total"] == 3
    assert page.json()["items"] == complete.json()["items"][1:2]


def test_category_counts_include_zero_and_exclude_untitled_competitions(db_session, client):
    populated = Category(name="Counted category", slug=f"counted-{uuid4().hex}")
    empty = Category(name="Empty category", slug=f"empty-{uuid4().hex}")
    titled = _published_competition(title="Counted competition")
    untitled = Competition(title=None)
    unpublished = Competition(title="Unpublished category item")
    db_session.add_all([populated, empty, titled, untitled, unpublished])
    db_session.flush()
    db_session.add_all(
        [
            CompetitionCategory(competition_id=titled.id, category_id=populated.id),
            CompetitionCategory(competition_id=untitled.id, category_id=populated.id),
            CompetitionCategory(
                competition_id=unpublished.id, category_id=populated.id
            ),
        ]
    )
    db_session.flush()

    counts = client.get("/categories")
    by_slug = {item["slug"]: item for item in counts.json()}
    category_results = client.get("/competitions", params={"category": populated.slug})

    assert by_slug[populated.slug] == {
        "id": populated.id,
        "name": "Counted category",
        "slug": populated.slug,
        "competition_count": 1,
    }
    assert by_slug[empty.slug]["competition_count"] == 0
    assert [item["id"] for item in category_results.json()["items"]] == [titled.id]
    assert category_results.json()["total"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"offset": -1},
        {"limit": "not-a-number"},
    ],
)
def test_competitions_rejects_invalid_pagination_parameters(client, params):
    response = client.get("/competitions", params=params)

    assert response.status_code == 422


def test_competitions_hides_database_errors(client):
    class BrokenSession:
        def scalar(self, statement):
            raise RuntimeError("database password must not leak")

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = client.get("/competitions")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json() == {"detail": {"status": "unavailable"}}
    assert "password" not in response.text


def test_competition_detail_returns_canonical_fields_and_public_relationships(
    db_session, client
):
    slug = f"test-category-{uuid4().hex}"
    competition = _published_competition(
        title="Reviewed competition",
        description="Canonical overview.",
        eligibility_summary="Open to students.",
        format="online",
        official_url="https://official.example.test/event",
        registration_url="https://register.example.test/event",
    )
    category = Category(name="Artificial Intelligence", slug=slug)
    organizer = Organizer(
        name="Test Organizer", website_url="https://organizer.example.test"
    )
    publisher = SourcePublisher(
        name="Test Publisher", homepage_url="https://publisher.example.test"
    )
    db_session.add_all([competition, category, organizer, publisher])
    db_session.flush()
    db_session.add_all(
        [
            CompetitionCategory(
                competition_id=competition.id,
                category_id=category.id,
                is_primary=True,
            ),
            CompetitionOrganizer(
                competition_id=competition.id,
                organizer_id=organizer.id,
                role="Host",
            ),
            CompetitionPrize(
                competition_id=competition.id,
                award_name="Grand prize",
                place="1st",
                amount=Decimal("250.00"),
                currency="USD",
            ),
        ]
    )
    source = CompetitionSource(
        competition_id=competition.id,
        publisher_id=publisher.id,
        source_kind="official",
        source_url="https://source.example.test/event",
        normalized_url="https://source.example.test/event",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        FieldEvidence(
            competition_id=competition.id,
            source_id=source.id,
            field_name="title",
            observed_value={"title": "Unreviewed candidate title"},
            review_state="candidate",
        )
    )
    db_session.flush()

    response = client.get(f"/competitions/{competition.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Reviewed competition"
    assert payload["description"] == "Canonical overview."
    assert payload["categories"] == [
        {"name": "Artificial Intelligence", "slug": slug, "is_primary": True}
    ]
    assert payload["organizers"] == [
        {
            "name": "Test Organizer",
            "role": "Host",
            "website_url": "https://organizer.example.test",
        }
    ]
    assert payload["prizes"] == [
        {
            "award_name": "Grand prize",
            "place": "1st",
            "amount": "250.00",
            "currency": "USD",
            "details": None,
        }
    ]
    assert payload["sources"] == [
        {
            "source_kind": "official",
            "source_url": "https://source.example.test/event",
            "publisher_name": "Test Publisher",
            "publisher_homepage_url": "https://publisher.example.test",
        }
    ]
    assert "evidence" not in payload
    assert "Unreviewed candidate title" not in response.text
    assert "check_status" not in payload["sources"][0]
    assert "last_verified_at" not in payload["sources"][0]


def test_competition_detail_returns_not_found_for_missing_or_untitled_rows(
    db_session, client
):
    untitled = Competition(title=None)
    db_session.add(untitled)
    db_session.flush()

    missing_response = client.get("/competitions/999999999")
    untitled_response = client.get(f"/competitions/{untitled.id}")

    assert missing_response.status_code == 404
    assert untitled_response.status_code == 404


def test_competition_detail_omits_invalid_external_urls(db_session, client):
    competition = _published_competition(
        title="Canonical title",
        official_url="javascript:alert(1)",
    )
    publisher = SourcePublisher(
        name="Unsafe publisher", homepage_url="file:///etc/passwd"
    )
    db_session.add_all([competition, publisher])
    db_session.flush()
    source = CompetitionSource(
        competition_id=competition.id,
        publisher_id=publisher.id,
        source_kind="aggregator",
        source_url="javascript:alert(2)",
    )
    db_session.add(source)
    db_session.flush()

    response = client.get(f"/competitions/{competition.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["official_url"] is None
    assert payload["sources"] == []


def test_competition_detail_hides_database_errors(client):
    class BrokenSession:
        def scalars(self, statement):
            raise RuntimeError("database password must not leak")

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = client.get("/competitions/1")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json() == {"detail": {"status": "unavailable"}}
    assert "password" not in response.text
