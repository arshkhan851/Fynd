import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from app.auth.models import User, UserSession
from app.auth.service import _hash_token
from app.database import SessionLocal, engine, get_db
from app.main import app
from app.models import Competition, SavedCompetition

pytestmark = pytest.mark.integration


_EXPECTED_DATABASE_TARGET = ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")


def _assert_saved_opportunities_test_database(
    database_url: str | None, engine_url: URL | None = None
) -> None:
    if not database_url:
        raise RuntimeError(
            "Saved Opportunities API tests require DATABASE_URL targeting "
            "127.0.0.1:56000/fynd_test as fynd_test_user"
        )
    try:
        configured_url = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError(
            "Saved Opportunities API tests require a valid DATABASE_URL targeting "
            "127.0.0.1:56000/fynd_test as fynd_test_user"
        ) from None

    configured_target = (
        configured_url.host,
        configured_url.port,
        configured_url.database,
        configured_url.username,
    )
    engine_target = None
    if engine_url is not None:
        engine_target = (
            engine_url.host,
            engine_url.port,
            engine_url.database,
            engine_url.username,
        )
    if configured_target != _EXPECTED_DATABASE_TARGET or (
        engine_target is not None and engine_target != _EXPECTED_DATABASE_TARGET
    ):
        raise RuntimeError(
            "Saved Opportunities API tests refuse database target; expected "
            "host=127.0.0.1 port=56000 database=fynd_test user=fynd_test_user"
        )


_assert_saved_opportunities_test_database(os.environ.get("DATABASE_URL"), engine.url)


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "not-a-database-url",
        "postgresql://fynd_test_user:unused@localhost:56000/fynd_test",
        "postgresql://fynd_test_user:unused@127.0.0.1:5432/fynd_test",
        "postgresql://fynd_test_user:unused@127.0.0.1:56000/fynd",
        "postgresql://fynd:unused@127.0.0.1:56000/fynd_test",
    ],
)
def test_saved_opportunities_database_guard_rejects_unsafe_targets(database_url):
    with pytest.raises(RuntimeError, match="Saved Opportunities API tests"):
        _assert_saved_opportunities_test_database(database_url)


def test_saved_opportunities_database_guard_accepts_isolated_target():
    _assert_saved_opportunities_test_database(
        "postgresql://fynd_test_user:unused@127.0.0.1:56000/fynd_test"
    )


def test_saved_opportunities_database_guard_checks_engine_target():
    configured_target = "postgresql://fynd_test_user:unused@127.0.0.1:56000/fynd_test"
    wrong_engine_target = make_url(
        "postgresql://fynd_test_user:unused@127.0.0.1:5432/fynd_test"
    )
    with pytest.raises(RuntimeError, match="refuse database target"):
        _assert_saved_opportunities_test_database(configured_target, wrong_engine_target)


@pytest.fixture
def saved_opportunities_db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(client: TestClient, db: Session, *, email: str) -> User:
    user = User(id=uuid4(), email=email, is_active=True, is_admin=False)
    token = uuid4().hex + uuid4().hex
    db.add(user)
    db.flush()
    db.add(
        UserSession(
            id=uuid4(),
            user_id=user.id,
            session_token_hash=_hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            is_revoked=False,
        )
    )
    db.commit()
    client.cookies.set("fynd_session", token)
    return user


def _public_competition(db: Session, *, title: str | None = None) -> Competition:
    competition = Competition(
        title=title or f"Saved API test {uuid4().hex}", is_published=True
    )
    db.add(competition)
    db.flush()
    return competition


def _saved_count(db: Session, user_id) -> int:
    return db.scalar(
        select(func.count()).select_from(SavedCompetition).where(
            SavedCompetition.user_id == user_id
        )
    ) or 0


def test_saved_opportunity_endpoints_require_authentication(
    saved_opportunities_db, client
):
    competition = _public_competition(saved_opportunities_db)
    assert client.get("/api/v1/saved-opportunities").status_code == 401
    assert client.put(f"/api/v1/saved-opportunities/{competition.id}").status_code == 401
    assert client.delete(f"/api/v1/saved-opportunities/{competition.id}").status_code == 401


def test_saved_opportunities_are_private_and_delete_is_owner_scoped(
    saved_opportunities_db, client
):
    db = saved_opportunities_db
    owner_client = TestClient(app)
    owner = _login(owner_client, db, email=f"saved-owner-{uuid4().hex}@example.org")
    other = _login(client, db, email=f"saved-other-{uuid4().hex}@example.org")
    competition = _public_competition(db)

    assert owner_client.put(f"/api/v1/saved-opportunities/{competition.id}").status_code == 200
    assert client.get("/api/v1/saved-opportunities").json()["total"] == 0
    assert client.delete(f"/api/v1/saved-opportunities/{competition.id}").json() == {
        "competition_id": competition.id,
        "saved": False,
    }
    assert _saved_count(db, owner.id) == 1
    assert _saved_count(db, other.id) == 0


def test_users_can_save_same_competition_without_exposing_each_others_data(
    saved_opportunities_db, client
):
    db = saved_opportunities_db
    user_a_client = TestClient(app)
    user_a = _login(user_a_client, db, email=f"saved-cross-a-{uuid4().hex}@example.org")
    user_b = _login(client, db, email=f"saved-cross-b-{uuid4().hex}@example.org")
    competition = _public_competition(db)
    path = f"/api/v1/saved-opportunities/{competition.id}"

    assert user_a_client.put(path).status_code == 200
    assert client.put(path).status_code == 200

    user_b_response = client.get("/api/v1/saved-opportunities").json()
    user_a_response = user_a_client.get("/api/v1/saved-opportunities").json()

    assert user_b_response["total"] == 1
    assert [item["competition_id"] for item in user_b_response["items"]] == [
        competition.id
    ]
    assert user_a_response["total"] == 1
    assert [item["competition_id"] for item in user_a_response["items"]] == [
        competition.id
    ]
    expected_item_fields = {
        "competition_id",
        "title",
        "format",
        "event_starts_on",
        "event_starts_at",
        "registration_deadline_on",
        "registration_deadline_at",
        "saved_at",
    }
    for item in (*user_a_response["items"], *user_b_response["items"]):
        assert set(item) == expected_item_fields
        assert "user_id" not in item
        assert "email" not in item

    assert _saved_count(db, user_a.id) == 1
    assert _saved_count(db, user_b.id) == 1


def test_save_is_idempotent_and_removal_is_idempotent(saved_opportunities_db, client):
    db = saved_opportunities_db
    user = _login(client, db, email=f"saved-idempotent-{uuid4().hex}@example.org")
    competition = _public_competition(db)
    path = f"/api/v1/saved-opportunities/{competition.id}"

    assert client.put(path).json() == {"competition_id": competition.id, "saved": True}
    assert client.put(path).json() == {"competition_id": competition.id, "saved": True}
    assert _saved_count(db, user.id) == 1
    assert client.delete(path).json() == {"competition_id": competition.id, "saved": False}
    assert client.delete(path).json() == {"competition_id": competition.id, "saved": False}
    assert _saved_count(db, user.id) == 0


@pytest.mark.parametrize("published,title", [(True, None), (False, "Hidden event")])
def test_saving_nonexistent_or_ineligible_competitions_returns_404(
    saved_opportunities_db, client, published, title
):
    db = saved_opportunities_db
    user = _login(client, db, email=f"saved-ineligible-{uuid4().hex}@example.org")
    competition = Competition(title=title, is_published=published)
    db.add(competition)
    db.flush()
    missing_id = (db.scalar(select(func.max(Competition.id))) or 0) + 1

    assert client.put(f"/api/v1/saved-opportunities/{missing_id}").status_code == 404
    assert client.put(f"/api/v1/saved-opportunities/{competition.id}").status_code == 404
    assert _saved_count(db, user.id) == 0


def test_unpublishing_hides_saved_competition_without_deleting_bookmark(
    saved_opportunities_db, client
):
    db = saved_opportunities_db
    user = _login(client, db, email=f"saved-hidden-{uuid4().hex}@example.org")
    competition = _public_competition(db)
    assert client.put(f"/api/v1/saved-opportunities/{competition.id}").status_code == 200

    competition.is_published = False
    db.flush()
    result = client.get("/api/v1/saved-opportunities").json()

    assert result["items"] == []
    assert result["total"] == 0
    assert _saved_count(db, user.id) == 1


def test_saved_opportunities_pagination_and_tie_order_are_stable(
    saved_opportunities_db, client
):
    db = saved_opportunities_db
    user = _login(client, db, email=f"saved-page-{uuid4().hex}@example.org")
    competitions = [_public_competition(db) for _ in range(3)]
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    db.add_all(
        [
            SavedCompetition(
                user_id=user.id, competition_id=competition.id, created_at=timestamp
            )
            for competition in competitions
        ]
    )
    db.flush()

    first = client.get("/api/v1/saved-opportunities?limit=2&offset=0").json()
    second = client.get("/api/v1/saved-opportunities?limit=2&offset=2").json()
    expected_ids = sorted((item.id for item in competitions), reverse=True)

    assert first["total"] == 3
    assert first["limit"] == 2
    assert first["offset"] == 0
    assert [item["competition_id"] for item in first["items"]] == expected_ids[:2]
    assert [item["competition_id"] for item in second["items"]] == expected_ids[2:]
    assert client.get("/api/v1/saved-opportunities?limit=51").status_code == 422


def test_viewing_or_searching_competition_does_not_save_it(saved_opportunities_db, client):
    db = saved_opportunities_db
    user = _login(client, db, email=f"saved-passive-{uuid4().hex}@example.org")
    competition = _public_competition(db, title=f"Passive view {uuid4().hex}")

    assert client.get("/competitions", params={"q": "Passive view"}).status_code == 200
    assert client.get(f"/competitions/{competition.id}").status_code == 200
    assert _saved_count(db, user.id) == 0


def test_concurrent_save_requests_create_one_bookmark():
    user_id = uuid4()
    token = uuid4().hex + uuid4().hex
    competition_id = None

    try:
        with SessionLocal.begin() as setup_db:
            setup_db.add(User(id=user_id, email=f"saved-race-{user_id}@example.org"))
            setup_db.add(
                UserSession(
                    id=uuid4(),
                    user_id=user_id,
                    session_token_hash=_hash_token(token),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    is_revoked=False,
                )
            )
            competition = _public_competition(setup_db)
            competition_id = competition.id

        def save_once() -> int:
            with TestClient(app) as request_client:
                request_client.cookies.set("fynd_session", token)
                return request_client.put(
                    f"/api/v1/saved-opportunities/{competition_id}"
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _index: save_once(), range(2)))

        assert statuses == [200, 200]
        with SessionLocal() as verify_db:
            assert _saved_count(verify_db, user_id) == 1
    finally:
        with SessionLocal.begin() as cleanup_db:
            if competition_id is not None:
                cleanup_db.execute(
                    SavedCompetition.__table__.delete().where(
                        SavedCompetition.competition_id == competition_id
                    )
                )
                cleanup_db.execute(
                    Competition.__table__.delete().where(Competition.id == competition_id)
                )
            cleanup_db.execute(User.__table__.delete().where(User.id == user_id))
