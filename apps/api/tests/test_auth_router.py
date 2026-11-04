import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.models import User, UserSession
from app.auth.router import router
from app.database import get_db


class FakeAuthSession:
    def __init__(self, *, raw_token: str, user: User, expires_at=None, revoked=False):
        self.user = user
        self.user_session = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            session_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
            is_revoked=revoked,
        )
        self.commits = 0

    def scalar(self, statement):
        params = statement.compile().params
        if self.user_session.session_token_hash in params.values():
            return self.user_session
        return None

    def get(self, model, identity):
        return self.user if model is User and self.user.id == identity else None

    def execute(self, statement):
        params = statement.compile().params
        if self.user_session.session_token_hash in params.values():
            self.user_session.is_revoked = True

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _client(db):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _user() -> User:
    return User(id=uuid.uuid4(), email="user@example.org", is_admin=False, is_active=True)


def test_me_returns_only_safe_authenticated_user_fields():
    user = _user()
    db = FakeAuthSession(raw_token="valid-cookie", user=user)
    client = _client(db)
    client.cookies.set("fynd_session", "valid-cookie")

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "email": user.email,
        "is_admin": False,
    }
    assert "session_token_hash" not in response.text
    assert "valid-cookie" not in response.text


def test_me_rejects_missing_session():
    client = _client(FakeAuthSession(raw_token="valid-cookie", user=_user()))
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    ("expires_at", "revoked"),
    [
        (datetime.now(UTC) - timedelta(seconds=1), False),
        (datetime.now(UTC) + timedelta(minutes=5), True),
    ],
)
def test_me_rejects_expired_or_revoked_session(expires_at, revoked):
    client = _client(
        FakeAuthSession(
            raw_token="invalid-cookie",
            user=_user(),
            expires_at=expires_at,
            revoked=revoked,
        )
    )
    client.cookies.set("fynd_session", "invalid-cookie")

    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_session_and_clears_cookie_attributes():
    user = _user()
    db = FakeAuthSession(raw_token="logout-cookie", user=user)
    client = _client(db)
    client.cookies.set("fynd_session", "logout-cookie")

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"message": "Logged out"}
    assert db.user_session.is_revoked is True
    assert db.commits == 1
    cookie = response.headers["set-cookie"].lower()
    assert 'fynd_session=""' in cookie
    assert "path=/" in cookie
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    # The response clears the browser cookie, and the server rejects the old token too.
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.set("fynd_session", "logout-cookie")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_requires_authentication():
    client = _client(FakeAuthSession(raw_token="logout-cookie", user=_user()))

    assert client.post("/api/v1/auth/logout").status_code == 401
