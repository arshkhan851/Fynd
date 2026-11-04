import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.dependencies import get_current_user, require_admin
from app.auth.models import User, UserSession
from app.database import get_db


def _user(*, is_admin: bool = False, is_active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        email="person@example.org",
        is_admin=is_admin,
        is_active=is_active,
    )


def _session(
    *, user_id: uuid.UUID, expires_at: datetime | None = None, is_revoked: bool = False
) -> UserSession:
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hashlib.sha256(b"session-secret").hexdigest(),
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=10),
        is_revoked=is_revoked,
    )


class FakeSession:
    def __init__(self, user_session: UserSession | None, user: User | None):
        self.user_session = user_session
        self.user = user
        self.statements = []
        self.user_ids = []

    def scalar(self, statement):
        self.statements.append(statement)
        assert isinstance(statement, type(select(UserSession)))
        params = statement.compile().params
        assert self.user_session is None or self.user_session.session_token_hash in params.values()
        return self.user_session

    def get(self, model, identity):
        assert model is User
        self.user_ids.append(identity)
        if self.user is not None and self.user.id == identity:
            return self.user
        return None


def _client(db):
    app = FastAPI()

    @app.get("/protected")
    def protected(user: Annotated[User, Depends(get_current_user)]):
        return {"email": user.email}

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


@pytest.mark.parametrize("cookies", [None, {"fynd_session": ""}])
def test_missing_or_empty_cookie_is_rejected(cookies):
    client = _client(FakeSession(None, None))
    if cookies:
        client.cookies.set("fynd_session", "")
    response = client.get("/protected")
    assert response.status_code == 401


def test_unknown_session_token_is_rejected():
    db = FakeSession(None, None)
    client = _client(db)
    client.cookies.set("fynd_session", "unknown")
    response = client.get("/protected")
    assert response.status_code == 401
    assert "unknown" not in response.text


@pytest.mark.parametrize(
    "user_session",
    [
        lambda user_id: _session(
            user_id=user_id, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        ),
        lambda user_id: _session(user_id=user_id, is_revoked=True),
    ],
)
def test_expired_or_revoked_session_is_rejected(user_session):
    user = _user()
    db = FakeSession(user_session(user.id), user)
    client = _client(db)
    client.cookies.set("fynd_session", "session-secret")
    response = client.get("/protected")
    assert response.status_code == 401
    assert db.user_ids == []


def test_missing_associated_user_is_rejected():
    db = FakeSession(_session(user_id=uuid.uuid4()), None)
    client = _client(db)
    client.cookies.set("fynd_session", "session-secret")
    response = client.get("/protected")
    assert response.status_code == 401


def test_inactive_user_is_rejected():
    user = _user(is_active=False)
    db = FakeSession(_session(user_id=user.id), user)
    client = _client(db)
    client.cookies.set("fynd_session", "session-secret")
    response = client.get("/protected")
    assert response.status_code == 401


def test_valid_cookie_returns_associated_active_user():
    user = _user()
    db = FakeSession(_session(user_id=user.id), user)
    client = _client(db)
    client.cookies.set("fynd_session", "session-secret")
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json() == {"email": user.email}
    assert db.user_ids == [user.id]
    assert hashlib.sha256(b"session-secret").hexdigest() not in response.text


def test_non_admin_is_rejected_by_require_admin():
    with pytest.raises(HTTPException) as error:
        require_admin(_user(is_admin=False))
    assert getattr(error.value, "status_code", None) == 403


def test_admin_is_accepted_by_require_admin():
    user = _user(is_admin=True)
    assert require_admin(user) is user
