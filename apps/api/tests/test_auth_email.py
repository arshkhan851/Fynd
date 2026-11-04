import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.email import BaseEmailProvider, get_email_provider
from app.auth.email_address import normalize_email
from app.auth.models import EmailAuthToken, User, UserSession
from app.auth.router import router
from app.database import get_db


class CapturingEmailProvider(BaseEmailProvider):
    def __init__(self):
        self.sent = []

    def send_magic_link(self, email: str, raw_token: str) -> None:
        self.sent.append((email, raw_token))


@pytest.fixture
def auth_client():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    User.__table__.create(engine)
    EmailAuthToken.__table__.create(engine)
    UserSession.__table__.create(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    provider = CapturingEmailProvider()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_email_provider] = lambda: provider
    try:
        yield TestClient(app), factory, provider
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Alice.Example+tag@EXAMPLE.ORG  ", "alice.example+tag@example.org"),
        ("USER@Example.COM", "user@example.com"),
    ],
)
def test_normalize_email_trims_and_casefolds_without_provider_rewrites(value, expected):
    assert normalize_email(value) == expected


@pytest.mark.parametrize(
    "email",
    [
        "",
        "person",
        "person@",
        "@example.org",
        "person..name@example.org",
        "a b@example.org",
        "a@example",
        "a@example..org",
    ],
)
def test_request_link_rejects_malformed_email_with_422(auth_client, email):
    client, _, _ = auth_client
    response = client.post("/api/v1/auth/request-link", json={"email": email})
    assert response.status_code == 422


def test_request_link_normalizes_address_and_keeps_generic_response(auth_client):
    client, factory, provider = auth_client
    response = client.post(
        "/api/v1/auth/request-link", json={"email": "  Alice@EXAMPLE.ORG "}
    )

    assert response.status_code == 202
    assert response.json() == {"message": "If the email is valid, a login link has been sent."}
    assert provider.sent and provider.sent[0][0] == "alice@example.org"
    with factory() as db:
        user = db.scalar(select(User))
        assert user.email == "alice@example.org"


def test_differently_cased_registration_reuses_one_user(auth_client):
    client, factory, _ = auth_client
    for email in ("Alice@Example.org", " alice@example.ORG "):
        response = client.post("/api/v1/auth/request-link", json={"email": email})
        assert response.status_code == 202

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_normalized_request_token_verifies_and_authenticates_normalized_user(auth_client):
    client, factory, provider = auth_client
    requested = client.post(
        "/api/v1/auth/request-link", json={"email": "  USER@Example.ORG "}
    )
    assert requested.status_code == 202

    verified = client.post("/api/v1/auth/verify", json={"token": provider.sent[0][1]})

    assert verified.status_code == 200
    with factory() as db:
        created_session = db.scalar(select(UserSession))
        assert created_session is not None
        assert db.get(User, created_session.user_id).email == "user@example.org"


def test_legacy_mixed_case_user_is_reused_without_modification(auth_client):
    client, factory, _ = auth_client
    with factory() as db:
        legacy_user = User(id=uuid.uuid4(), email="Alice@Example.org")
        db.add(legacy_user)
        db.commit()

    response = client.post(
        "/api/v1/auth/request-link", json={"email": "alice@example.ORG"}
    )

    assert response.status_code == 202
    with factory() as db:
        users = db.scalars(select(User)).all()
        assert len(users) == 1
        assert users[0].email == "Alice@Example.org"
