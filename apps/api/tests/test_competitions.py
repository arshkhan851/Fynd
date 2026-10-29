from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Competition, CompetitionSource, FieldEvidence

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


def _add_competitions(session: Session) -> tuple[int, int, int]:
    hidden_without_title = Competition(title=None)
    hidden_candidate = Competition(title=None)
    second_visible = Competition(title="Visible competition B")
    first_visible = Competition(title="Visible competition A")
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
    assert candidate_id not in ids
    assert first_id in ids
    assert second_id in ids
    assert all(item["title"] is not None for item in payload["items"])
    assert ids == sorted(ids)
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
