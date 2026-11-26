from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.auth.models import User
from app.database import get_db
from app.discovery.registry import RegistryError
from app.models import FieldEvidence
from app.review.schemas import (
    EvidenceDetailResponse,
    EvidenceMutationResponse,
    PendingEvidenceResponse,
)
from app.review.service import (
    ReviewError,
    accept_evidence,
    get_evidence_detail,
    list_pending_evidence,
    reject_evidence,
)

router = APIRouter(prefix="/admin/evidence", tags=["admin-evidence-review"])


def _review_error(exc: ReviewError) -> HTTPException:
    message = str(exc)
    if "was not found" in message or "no longer exists" in message:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    if "is already" in message:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
    if isinstance(exc.__cause__, RegistryError):
        if "not registered" in message.lower() or "internal_review refused:" in message.lower():
            return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence review authorization registry is unavailable",
        )
    if any(
        marker in message.lower()
        for marker in (
            "not registered",
            "refused",
            "permission",
            "trusted registry identity",
            "not allowed for internal review",
            "reviewer",
        )
    ):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _mutation_response(evidence: FieldEvidence) -> EvidenceMutationResponse:
    return EvidenceMutationResponse(
        evidence_id=evidence.id,
        competition_id=evidence.competition_id,
        field_name=evidence.field_name,
        field_key=evidence.field_key,
        review_state=evidence.review_state,
        reviewed_at=evidence.reviewed_at,
    )


@router.get("", response_model=PendingEvidenceResponse)
def list_admin_evidence(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    field_name: Annotated[str | None, Query(alias="field")] = None,
) -> PendingEvidenceResponse:
    items = list_pending_evidence(db, limit=limit, field_name=field_name)
    return PendingEvidenceResponse(items=items, limit=limit, field=field_name)


@router.get("/{evidence_id}", response_model=EvidenceDetailResponse)
def get_admin_evidence(
    evidence_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> EvidenceDetailResponse:
    try:
        return EvidenceDetailResponse(**get_evidence_detail(db, evidence_id))
    except ReviewError as exc:
        raise _review_error(exc) from exc


@router.post("/{evidence_id}/accept", response_model=EvidenceMutationResponse)
def accept_admin_evidence(
    evidence_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> EvidenceMutationResponse:
    try:
        evidence = accept_evidence(db, evidence_id, reviewer=admin)
    except ReviewError as exc:
        raise _review_error(exc) from exc
    return _mutation_response(evidence)


@router.post("/{evidence_id}/reject", response_model=EvidenceMutationResponse)
def reject_admin_evidence(
    evidence_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> EvidenceMutationResponse:
    try:
        evidence = reject_evidence(db, evidence_id, reviewer=admin)
    except ReviewError as exc:
        raise _review_error(exc) from exc
    return _mutation_response(evidence)
