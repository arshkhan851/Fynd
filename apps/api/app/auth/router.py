from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth.service import generate_magic_link_token, verify_magic_token_and_create_session
from app.auth.email import BaseEmailProvider, get_email_provider

router = APIRouter(prefix="/auth", tags=["auth"])

class MagicLinkRequest(BaseModel):
    email: str  # Use EmailStr if pydantic[email] is installed

class VerifyTokenRequest(BaseModel):
    token: str

@router.post("/request-link", status_code=status.HTTP_202_ACCEPTED)
def request_magic_link(
    payload: MagicLinkRequest,
    db: Session = Depends(get_db),
    email_provider: BaseEmailProvider = Depends(get_email_provider)
):
    """Generates a magic link token and dispatches it via the email provider."""
    raw_token = generate_magic_link_token(db, payload.email)
    email_provider.send_magic_link(payload.email, raw_token)
    
    # Always return a generic success message to prevent email enumeration
    return {"message": "If the email is valid, a login link has been sent."}

@router.post("/verify", status_code=status.HTTP_200_OK)
def verify_magic_link(
    payload: VerifyTokenRequest,
    response: Response,
    db: Session = Depends(get_db)
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
        max_age=30 * 24 * 60 * 60,  # 30 days in seconds
    )
    
    return {"message": "Successfully authenticated."}