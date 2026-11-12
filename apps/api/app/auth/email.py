"""Magic-link email providers and environment-based provider selection."""

import abc
import os
from urllib.parse import urlsplit

import httpx

MAGIC_LINK_LIFETIME_MINUTES = 15
RESEND_EMAILS_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0


class EmailProviderConfigurationError(RuntimeError):
    """Raised when the selected provider cannot be configured safely."""


class EmailDeliveryError(RuntimeError):
    """A sanitized provider failure safe to propagate through the API."""


class BaseEmailProvider(abc.ABC):
    """Abstract interface for all email dispatchers."""

    @abc.abstractmethod
    def send_magic_link(self, email: str, raw_token: str) -> None:
        """Send a passwordless login link without exposing it in logs."""


class ConsoleEmailProvider(BaseEmailProvider):
    """Local-development email provider; never select this in production."""

    def __init__(self, frontend_url: str = "http://localhost:3000"):
        self.frontend_url = frontend_url.rstrip("/")

    def send_magic_link(self, email: str, raw_token: str) -> None:
        magic_link = f"{self.frontend_url}/auth/verify?token={raw_token}"
        print("\n" + "=" * 60)
        print("EMAIL INTERCEPTED (LOCAL DEVELOPMENT)")
        print(f"To: {email}")
        print("Subject: Log in to Fynd")
        print("Body: Click the link below to securely log in.")
        print(f"Link: {magic_link}")
        print("=" * 60 + "\n")


class ResendEmailProvider(BaseEmailProvider):
    """Synchronous provider using Resend's official email API."""

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        frontend_url: str,
        timeout: float = RESEND_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise EmailProviderConfigurationError("RESEND_API_KEY is required")
        if not sender.strip():
            raise EmailProviderConfigurationError("EMAIL_FROM is required")
        if not _valid_frontend_url(frontend_url):
            raise EmailProviderConfigurationError("FRONTEND_URL must be an absolute HTTP(S) URL")
        self._api_key = api_key
        self._sender = sender
        self._frontend_url = frontend_url.rstrip("/")
        self._timeout = timeout

    def send_magic_link(self, email: str, raw_token: str) -> None:
        magic_link = f"{self._frontend_url}/auth/verify?token={raw_token}"
        text = (
            "You requested a sign-in link for Fynd.\n\n"
            f"Sign in: {magic_link}\n\n"
            f"This link expires in {MAGIC_LINK_LIFETIME_MINUTES} minutes. "
            "If you did not request this email, you can ignore it."
        )
        try:
            response = httpx.post(
                RESEND_EMAILS_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": self._sender,
                    "to": [email],
                    "subject": "Your Fynd sign-in link",
                    "text": text,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            raise EmailDeliveryError("Email provider is temporarily unavailable") from None

        if not response.is_success:
            raise EmailDeliveryError(
                f"Email provider rejected the message (HTTP {response.status_code})"
            )


def _valid_frontend_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not any(char.isspace() for char in value)
    )


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EmailProviderConfigurationError(f"{name} is required for Resend")
    return value


def get_email_provider() -> BaseEmailProvider:
    """Select a provider explicitly; production cannot fall back to console."""
    configured_app_env = os.getenv("APP_ENV", "").strip().lower()
    if not configured_app_env:
        raise EmailProviderConfigurationError(
            "APP_ENV must be explicitly set to development, test, or production"
        )
    app_env = configured_app_env
    selected = os.getenv("EMAIL_PROVIDER", "").strip().lower()

    if app_env not in {"development", "test", "production"}:
        raise EmailProviderConfigurationError(
            "APP_ENV must be development, test, or production"
        )

    if app_env == "production":
        if selected != "resend":
            raise EmailProviderConfigurationError(
                "EMAIL_PROVIDER=resend is required when APP_ENV=production"
            )
        api_key = _required_environment("RESEND_API_KEY")
        sender = _required_environment("EMAIL_FROM")
        frontend_url = _required_environment("FRONTEND_URL")
        if not _valid_frontend_url(frontend_url) or urlsplit(frontend_url).scheme != "https":
            raise EmailProviderConfigurationError(
                "FRONTEND_URL must use HTTPS in production"
            )
        return ResendEmailProvider(
            api_key=api_key,
            sender=sender,
            frontend_url=frontend_url,
        )

    if app_env == "test":
        raise EmailProviderConfigurationError(
            "Tests must inject a fake email provider"
        )
    if selected in {"", "console"}:
        return ConsoleEmailProvider(os.getenv("FRONTEND_URL", "http://localhost:3000"))
    if selected == "resend":
        return ResendEmailProvider(
            api_key=_required_environment("RESEND_API_KEY"),
            sender=_required_environment("EMAIL_FROM"),
            frontend_url=os.getenv("FRONTEND_URL", "http://localhost:3000"),
        )
    raise EmailProviderConfigurationError("EMAIL_PROVIDER must be console or resend")
