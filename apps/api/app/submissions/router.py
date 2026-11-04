"""Authenticated organizer submission endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.database import get_db
from app.models import OrganizerSubmission, OrganizerSubmissionCategory
from app.submissions.schemas import (
    SubmissionCreateRequest,
    SubmissionListResponse,
    SubmissionResponse,
)
from app.submissions.service import (
    SubmissionAuthorizationError,
    SubmissionValidationError,
    SubmittedEvent,
    create_submission,
)

router = APIRouter(prefix="/submissions", tags=["submissions"])


def _response(submission: OrganizerSubmission) -> SubmissionResponse:
    return SubmissionResponse(
        id=submission.id,
        status=submission.status,
        title=submission.title,
        description=submission.description,
        organizer_name=submission.organizer_name,
        official_url=submission.official_url,
        registration_url=submission.registration_url,
        registration_deadline_at=submission.registration_deadline_at,
        registration_deadline_on=submission.registration_deadline_on,
        event_starts_at=submission.event_starts_at,
        event_starts_on=submission.event_starts_on,
        event_ends_at=submission.event_ends_at,
        event_ends_on=submission.event_ends_on,
        event_timezone=submission.event_timezone,
        format=submission.format,
        location_name=submission.location_name,
        location_city=submission.location_city,
        location_region=submission.location_region,
        location_country_code=submission.location_country_code,
        registration_price_status=submission.registration_price_status,
        registration_fee_amount=submission.registration_fee_amount,
        registration_fee_currency=submission.registration_fee_currency,
        categories=[
            {
                "id": link.category.id,
                "name": link.category.name,
                "slug": link.category.slug,
                "is_primary": link.is_primary,
            }
            for link in submission.category_links
        ],
        created_at=submission.created_at,
    )


def _owned_submission_query(user: User):
    return (
        select(OrganizerSubmission)
        .where(OrganizerSubmission.submitter_user_id == user.id)
        .options(
            selectinload(OrganizerSubmission.category_links).selectinload(
                OrganizerSubmissionCategory.category
            )
        )
    )


@router.post("", response_model=SubmissionResponse, status_code=status.HTTP_201_CREATED)
def submit_event(
    payload: SubmissionCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SubmissionResponse:
    event_fields = payload.model_dump(
        exclude={
            "organizer_profile_id",
            "category_ids",
            "primary_category_id",
            "consent",
        }
    )
    event = SubmittedEvent(**event_fields)
    try:
        submission = create_submission(
            db,
            user=current_user,
            event=event,
            organizer_profile_id=payload.organizer_profile_id,
            category_ids=payload.category_ids,
            primary_category_id=payload.primary_category_id,
            declaration_version=payload.consent.declaration_version,
            declaration_text=payload.consent.declaration_text,
        )
    except SubmissionAuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except SubmissionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return _response(submission)


@router.get("/me", response_model=SubmissionListResponse)
def list_my_submissions(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SubmissionListResponse:
    filters = (OrganizerSubmission.submitter_user_id == current_user.id,)
    total = db.scalar(select(func.count()).select_from(OrganizerSubmission).where(*filters)) or 0
    submissions = db.scalars(
        select(OrganizerSubmission)
        .where(*filters)
        .options(
            selectinload(OrganizerSubmission.category_links).selectinload(
                OrganizerSubmissionCategory.category
            )
        )
        .order_by(OrganizerSubmission.created_at.desc(), OrganizerSubmission.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return SubmissionListResponse(
        items=[_response(submission) for submission in submissions],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{submission_id}", response_model=SubmissionResponse)
def get_my_submission(
    submission_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SubmissionResponse:
    submission = db.scalar(
        _owned_submission_query(current_user).where(OrganizerSubmission.id == submission_id)
    )
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return _response(submission)


@router.post("/{submission_id}/withdraw", response_model=SubmissionResponse)
def withdraw_submission(
    submission_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SubmissionResponse:
    try:
        submission = db.scalar(
            _owned_submission_query(current_user)
            .where(OrganizerSubmission.id == submission_id)
            .with_for_update()
        )
        if submission is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found"
            )
        if submission.status not in {"pending", "under_review"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Submission cannot be withdrawn in its current status",
            )

        # SubmissionReviewEvent represents reviewer actions and requires a reviewer_user_id.
        # A submitter withdrawal is not an admin review; avoid fabricating a reviewer.
        submission.status = "withdrawn"
        db.flush()
        db.commit()
        return _response(submission)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
