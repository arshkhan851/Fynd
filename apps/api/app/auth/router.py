import ipaddress
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.email import (
    BaseEmailProvider,
    EmailDeliveryError,
    EmailProviderConfigurationError,
    get_email_provider,
)
from app.auth.email_address import normalize_email
from app.auth.models import User
from app.auth.rate_limit import (
    RateLimitConfigurationError,
    RateLimitDecision,
    RateLimitIdentifierError,
    RateLimitStorageError,
    increment_request_counters,
)
from app.auth.service import (
    ActiveMagicLinkLimitExceeded,
    generate_magic_link_token,
    revoke_session,
    verify_magic_token_and_create_session,
)
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
_GENERIC_REQUEST_LINK_RESPONSE = {
    "message": "If the email is valid, a login link has been sent."
}
_RATE_LIMIT_UNAVAILABLE_DETAIL = "Authentication service is temporarily unavailable."


def get_email_provider_factory() -> Callable[[], BaseEmailProvider]:
    """Return a lazy provider factory so denied requests do not resolve a provider."""
    return get_email_provider


def _client_ip_from_request(request: Request) -> str:
    client = request.client
    if client is None or not client.host:
        raise RateLimitIdentifierError("Client IP is unavailable")
    try:
        ipaddress.ip_address(client.host)
    except (TypeError, ValueError):
        raise RateLimitIdentifierError("Client IP is invalid") from None
    return client.host


class AuthenticatedUserResponse(BaseModel):
    id: UUID
    email: str
    is_admin: bool


class MagicLinkRequest(BaseModel):
    email: str

    @field_validator("email", mode="before")
    @classmethod
    def normalize_and_validate_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Invalid email address")
        return normalize_email(value)

class VerifyTokenRequest(BaseModel):
    token: str


@router.get("/me", response_model=AuthenticatedUserResponse)
def current_user_info(
    current_user: Annotated[User, Depends(get_current_user)],
) -> AuthenticatedUserResponse:
    return AuthenticatedUserResponse(
        id=current_user.id,
        email=current_user.email,
        is_admin=current_user.is_admin,
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    session_token: Annotated[str | None, Cookie(alias="fynd_session")] = None,
) -> dict[str, str]:
    # get_current_user has already validated this exact cookie and its associated user.
    revoke_session(db, session_token or "")
    response.delete_cookie(
        key="fynd_session",
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return {"message": "Logged out"}

@router.post("/request-link", status_code=status.HTTP_202_ACCEPTED)
def request_magic_link(
    payload: MagicLinkRequest,
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008 - existing FastAPI dependency convention
    email_provider_factory: Callable[[], BaseEmailProvider] = Depends(  # noqa: B008
        get_email_provider_factory
    ),
):
    """Apply persistent abuse limits before generating or emailing a login link."""
    try:
        decision: RateLimitDecision = increment_request_counters(
            db,
            email=payload.email,
            client_ip=_client_ip_from_request(request),
        )
    except (
        RateLimitConfigurationError,
        RateLimitIdentifierError,
        RateLimitStorageError,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_RATE_LIMIT_UNAVAILABLE_DETAIL,
        ) from None

    if not decision.allowed:
        return _GENERIC_REQUEST_LINK_RESPONSE

    try:
        email_provider = email_provider_factory()
    except EmailProviderConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication email service is temporarily unavailable.",
        ) from None

    try:
        raw_token = generate_magic_link_token(db, payload.email)
    except ActiveMagicLinkLimitExceeded:
        # Keep the response indistinguishable from successful issuance so callers
        # cannot learn whether this address has reached its active-token cap.
        return _GENERIC_REQUEST_LINK_RESPONSE
    try:
        email_provider.send_magic_link(payload.email, raw_token)
    except EmailDeliveryError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication email service is temporarily unavailable.",
        ) from None
    
    # Always return a generic success message to prevent email enumeration
    return _GENERIC_REQUEST_LINK_RESPONSE

@router.post("/verify", status_code=status.HTTP_200_OK)
def verify_magic_link(
    payload: VerifyTokenRequest,
    response: Response,
    db: Session = Depends(get_db),  # noqa: B008 - existing FastAPI dependency convention
):
    """Exchanges a valid magic link token for a secure session cookie."""
    session_token = verify_magic_token_and_create_session(db, payload.token)
    
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token."
        )

    # Set the secure HttpOnly cookie
    response.set_cookie(
        key="fynd_session",
        value=session_token,
        httponly=True,
        secure=True,  # Set to False only if testing locally without HTTPS
        samesite="lax",
        path="/",
        max_age=30 * 24 * 60 * 60,  # 30 days in seconds
    )
    
    return {"message": "Successfully authenticated."}
