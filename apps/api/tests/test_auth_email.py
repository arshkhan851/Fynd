import hashlib
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register SavedCompetition relationship mapping
from app.auth.email import (
    BaseEmailProvider,
    ConsoleEmailProvider,
    EmailDeliveryError,
    EmailProviderConfigurationError,
    ResendEmailProvider,
    get_email_provider,
)
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


def test_resend_provider_posts_text_email_without_logging_token(monkeypatch, caplog, capsys):
    raw_token = "sensitive-magic-token"
    request = {}

    def fake_post(url, **kwargs):
        request.update(url=url, **kwargs)
        return httpx.Response(200, json={"id": "email-id"})

    monkeypatch.setattr("app.auth.email.httpx.post", fake_post)
    provider = ResendEmailProvider(
        api_key="secret-api-key",
        sender="Fynd <login@example.org>",
        frontend_url="https://fynd.example.org",
    )

    provider.send_magic_link("student@example.org", raw_token)

    assert request["url"] == "https://api.resend.com/emails"
    assert request["headers"]["Authorization"] == "Bearer secret-api-key"
    assert request["timeout"] == 10
    body = request["json"]
    assert body["from"] == "Fynd <login@example.org>"
    assert body["to"] == ["student@example.org"]
    assert "https://fynd.example.org/auth/verify?token=sensitive-magic-token" in body["text"]
    assert "15 minutes" in body["text"]
    assert "you can ignore it" in body["text"]
    assert raw_token not in caplog.text
    assert raw_token not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("api_key", "sender", "missing"),
    [
        ("", "Fynd <login@example.org>", "RESEND_API_KEY"),
        ("resend-key", "", "EMAIL_FROM"),
    ],
)
def test_resend_provider_rejects_missing_configuration(api_key, sender, missing):
    with pytest.raises(EmailProviderConfigurationError, match=missing):
        ResendEmailProvider(
            api_key=api_key,
            sender=sender,
            frontend_url="https://fynd.example.org",
        )


def test_resend_provider_rejects_unsuccessful_response(monkeypatch):
    monkeypatch.setattr(
        "app.auth.email.httpx.post",
        lambda *args, **kwargs: httpx.Response(422, text="provider details include secrets"),
    )
    provider = ResendEmailProvider(
        api_key="secret-api-key",
        sender="Fynd <login@example.org>",
        frontend_url="https://fynd.example.org",
    )

    with pytest.raises(EmailDeliveryError, match="HTTP 422") as exc_info:
        provider.send_magic_link("student@example.org", "sensitive-token")
    assert "provider details" not in str(exc_info.value)
    assert "secret-api-key" not in str(exc_info.value)
    assert "sensitive-token" not in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("private connection detail"), httpx.TimeoutException("timeout")],
)
def test_resend_provider_sanitizes_network_failures(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("app.auth.email.httpx.post", fail)
    provider = ResendEmailProvider(
        api_key="secret-api-key",
        sender="Fynd <login@example.org>",
        frontend_url="https://fynd.example.org",
    )

    with pytest.raises(EmailDeliveryError, match="temporarily unavailable") as exc_info:
        provider.send_magic_link("student@example.org", "sensitive-token")
    assert "private connection detail" not in str(exc_info.value)
    assert "secret-api-key" not in str(exc_info.value)
    assert "sensitive-token" not in str(exc_info.value)


def test_provider_selection_defaults_to_console_only_in_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")

    assert isinstance(get_email_provider(), ConsoleEmailProvider)


def test_provider_selection_requires_explicit_app_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)

    with pytest.raises(EmailProviderConfigurationError, match="APP_ENV must be explicitly set"):
        get_email_provider()


def test_provider_selection_rejects_unsupported_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(EmailProviderConfigurationError, match="APP_ENV must be development"):
        get_email_provider()


def test_production_selects_resend_explicitly(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "secret-api-key")
    monkeypatch.setenv("EMAIL_FROM", "Fynd <login@example.org>")
    monkeypatch.setenv("FRONTEND_URL", "https://fynd.example.org")

    assert isinstance(get_email_provider(), ResendEmailProvider)


@pytest.mark.parametrize(
    "provider_name",
    ["", "console"],
)
def test_production_never_falls_back_to_console(monkeypatch, provider_name):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EMAIL_PROVIDER", provider_name)

    with pytest.raises(EmailProviderConfigurationError, match="EMAIL_PROVIDER=resend"):
        get_email_provider()


def test_production_requires_all_resend_configuration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    for name in ("RESEND_API_KEY", "EMAIL_FROM", "FRONTEND_URL"):
        monkeypatch.delenv(name, raising=False)
        with pytest.raises(EmailProviderConfigurationError, match=name):
            get_email_provider()
        monkeypatch.setenv(name, "configured")


def test_automated_tests_must_inject_fake_provider(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(EmailProviderConfigurationError, match="inject a fake"):
        get_email_provider()


def test_provider_rejection_returns_safe_unavailable_response(auth_client):
    from app.auth.router import get_email_provider as dependency

    client, _, _ = auth_client

    class RejectingProvider(BaseEmailProvider):
        def send_magic_link(self, email: str, raw_token: str) -> None:
            raise EmailDeliveryError("sanitized provider failure")

    client.app.dependency_overrides[dependency] = RejectingProvider
    response = client.post("/api/v1/auth/request-link", json={"email": "user@example.org"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Authentication email service is temporarily unavailable."
    }
    assert "sanitized provider failure" not in response.text


def test_failed_email_delivery_does_not_invalidate_earlier_link(auth_client):
    from app.auth.router import get_email_provider as dependency

    client, factory, provider = auth_client
    first = client.post(
        "/api/v1/auth/request-link", json={"email": "user@example.org"}
    )
    assert first.status_code == 202
    previously_delivered_token = provider.sent[0][1]
    previous_hash = hashlib.sha256(previously_delivered_token.encode()).hexdigest()

    class RejectingProvider(BaseEmailProvider):
        def __init__(self):
            self.raw_token = None

        def send_magic_link(self, email: str, raw_token: str) -> None:
            self.raw_token = raw_token
            raise EmailDeliveryError("sanitized provider failure")

    rejecting_provider = RejectingProvider()
    client.app.dependency_overrides[dependency] = lambda: rejecting_provider
    failed = client.post(
        "/api/v1/auth/request-link", json={"email": "user@example.org"}
    )

    assert failed.status_code == 503
    failed_hash = hashlib.sha256(rejecting_provider.raw_token.encode()).hexdigest()
    with factory() as db:
        active_rows = db.scalars(
            select(EmailAuthToken).where(
                EmailAuthToken.used_at.is_(None),
                EmailAuthToken.expires_at > func.now(),
            )
        ).all()
        assert len(active_rows) == 2
        assert {row.token_hash for row in active_rows} == {previous_hash, failed_hash}

    # The failed delivery's token may remain active, but earlier delivered links
    # remain untouched and usable until their normal expiry or consumption.
    client.app.dependency_overrides[dependency] = lambda: provider
    for _ in range(3):
        successful = client.post(
            "/api/v1/auth/request-link", json={"email": "user@example.org"}
        )
        assert successful.status_code == 202

    provider_send_count = len(provider.sent)
    cap_response = client.post(
        "/api/v1/auth/request-link", json={"email": "user@example.org"}
    )
    assert cap_response.status_code == first.status_code
    assert cap_response.json() == first.json()
    assert len(provider.sent) == provider_send_count
    with factory() as db:
        assert db.scalar(
            select(func.count())
            .select_from(EmailAuthToken)
            .where(
                EmailAuthToken.used_at.is_(None),
                EmailAuthToken.expires_at > func.now(),
            )
        ) == 5

    verified = client.post(
        "/api/v1/auth/verify", json={"token": previously_delivered_token}
    )
    assert verified.status_code == 200
    replay = client.post(
        "/api/v1/auth/verify", json={"token": previously_delivered_token}
    )
    assert replay.status_code == 401


def test_active_token_cap_response_matches_success_without_sending_email(auth_client):
    client, _, provider = auth_client
    successful_responses = [
        client.post(
            "/api/v1/auth/request-link", json={"email": "user@example.org"}
        )
        for _ in range(5)
    ]
    assert all(response.status_code == 202 for response in successful_responses)
    success = successful_responses[0]
    sent_count = len(provider.sent)

    limited = client.post(
        "/api/v1/auth/request-link", json={"email": "user@example.org"}
    )

    assert limited.status_code == success.status_code == 202
    assert limited.json() == success.json()
    assert len(provider.sent) == sent_count == 5
