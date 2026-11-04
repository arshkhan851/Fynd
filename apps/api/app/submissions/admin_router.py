"""Administrator queue and human review actions for organizer submissions."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import require_admin
from app.auth.models import User
from app.database import get_db
from app.models import (
    OrganizerSubmission,
    OrganizerSubmissionCategory,
    SubmissionReviewEvent,
)
from app.submissions.router import _response
from app.submissions.schemas import (
    AdminConsentResponse,
    AdminReviewEventResponse,
    AdminSubmissionDetailResponse,
    AdminSubmissionQueueItem,
    AdminSubmissionQueueResponse,
    ApproveSubmissionRequest,
    RejectSubmissionRequest,
    SubmissionStatus,
)
from app.submissions.service import (
    SubmissionAuthorizationError,
    SubmissionNotFoundError,
    SubmissionStateConflictError,
    SubmissionValidationError,
    approve_submission,
    reject_submission,
    start_submission_review,
)

router = APIRouter(prefix="/admin/submissions", tags=["admin-submissions"])


def _review_events(submission: OrganizerSubmission) -> list[AdminReviewEventResponse]:
    return [
        AdminReviewEventResponse(
            id=event.id,
            reviewer_user_id=event.reviewer_user_id,
            reviewer_email=event.reviewer.email,
            previous_status=event.previous_status,
            new_status=event.new_status,
            note=event.note,
            created_at=event.created_at,
        )
        for event in submission.review_events
    ]


def _load_options():
    return (
        selectinload(OrganizerSubmission.submitter),
        selectinload(OrganizerSubmission.consent),
        selectinload(OrganizerSubmission.category_links).selectinload(
            OrganizerSubmissionCategory.category
        ),
        selectinload(OrganizerSubmission.review_events).selectinload(
            SubmissionReviewEvent.reviewer
        ),
    )


def _admin_query():
    return select(OrganizerSubmission).options(*_load_options())


def _detail(submission: OrganizerSubmission) -> AdminSubmissionDetailResponse:
    return AdminSubmissionDetailResponse(
        **_response(submission).model_dump(),
        submitter_email=submission.submitter.email,
        organizer_email=submission.organizer_email,
        organizer_contact_name=submission.organizer_contact_name,
        organizer_contact_email=submission.organizer_contact_email,
        organizer_contact_phone=submission.organizer_contact_phone,
        organizer_profile_id=submission.organizer_profile_id,
        approved_competition_id=submission.approved_competition_id,
        consent=AdminConsentResponse(
            declaration_version=submission.consent.declaration_version,
            accepted_at=submission.consent.accepted_at,
            declaration_text=submission.consent.declaration_text,
        ),
        review_events=_review_events(submission),
    )


def _review_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SubmissionNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    if isinstance(exc, SubmissionStateConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, SubmissionAuthorizationError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, SubmissionValidationError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    raise exc


@router.get("", response_model=AdminSubmissionQueueResponse)
def list_admin_submissions(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[list[SubmissionStatus] | None, Query(alias="status")] = None,
) -> AdminSubmissionQueueResponse:
    selected_statuses = status_filter or [SubmissionStatus.pending, SubmissionStatus.under_review]
    filters = [OrganizerSubmission.status.in_([value.value for value in selected_statuses])]
    total = db.scalar(select(func.count()).select_from(OrganizerSubmission).where(*filters)) or 0
    submissions = db.scalars(
        _admin_query()
        .where(*filters)
        .order_by(OrganizerSubmission.created_at.asc(), OrganizerSubmission.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        AdminSubmissionQueueItem(
            id=submission.id,
            status=submission.status,
            title=submission.title,
            organizer_name=submission.organizer_name,
            submitter_email=submission.submitter.email,
            organizer_profile_id=submission.organizer_profile_id,
            created_at=submission.created_at,
            updated_at=submission.updated_at,
            review_events=_review_events(submission),
        )
        for submission in submissions
    ]
    return AdminSubmissionQueueResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{submission_id}", response_model=AdminSubmissionDetailResponse)
def get_admin_submission(
    submission_id: int,
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> AdminSubmissionDetailResponse:
    submission = db.scalar(_admin_query().where(OrganizerSubmission.id == submission_id))
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return _detail(submission)


@router.post("/{submission_id}/start-review", response_model=AdminSubmissionDetailResponse)
def start_review(
    submission_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> AdminSubmissionDetailResponse:
    try:
        start_submission_review(db, submission_id=submission_id, reviewer=admin)
        submission = db.scalar(_admin_query().where(OrganizerSubmission.id == submission_id))
        assert submission is not None
        return _detail(submission)
    except (
        SubmissionAuthorizationError,
        SubmissionNotFoundError,
        SubmissionStateConflictError,
        SubmissionValidationError,
    ) as exc:
        raise _review_error(exc) from exc


@router.post("/{submission_id}/reject", response_model=AdminSubmissionDetailResponse)
def reject(
    submission_id: int,
    payload: RejectSubmissionRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> AdminSubmissionDetailResponse:
    try:
        reject_submission(
            db,
            submission_id=submission_id,
            reviewer=admin,
            reason=payload.reason,
        )
        submission = db.scalar(_admin_query().where(OrganizerSubmission.id == submission_id))
        assert submission is not None
        return _detail(submission)
    except (
        SubmissionAuthorizationError,
        SubmissionNotFoundError,
        SubmissionStateConflictError,
        SubmissionValidationError,
    ) as exc:
        raise _review_error(exc) from exc


@router.post("/{submission_id}/approve", response_model=AdminSubmissionDetailResponse)
def approve(
    submission_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    payload: ApproveSubmissionRequest | None = None,
) -> AdminSubmissionDetailResponse:
    try:
        approve_submission(
            db,
            submission_id=submission_id,
            reviewer=admin,
            existing_competition_id=(
                payload.existing_competition_id if payload is not None else None
            ),
            existing_organizer_id=(payload.existing_organizer_id if payload is not None else None),
        )
        submission = db.scalar(_admin_query().where(OrganizerSubmission.id == submission_id))
        assert submission is not None
        return _detail(submission)
    except (
        SubmissionAuthorizationError,
        SubmissionNotFoundError,
        SubmissionStateConflictError,
        SubmissionValidationError,
    ) as exc:
        raise _review_error(exc) from exc
