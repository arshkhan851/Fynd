"""Authenticated saved-opportunity endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.database import get_db
from app.models import Competition, SavedCompetition
from app.saved_opportunities.schemas import (
    SavedOpportunityItem,
    SavedOpportunityListResponse,
    SavedOpportunityMutationResponse,
)

router = APIRouter(prefix="/saved-opportunities", tags=["saved-opportunities"])


def _visible_competition_filters():
    return (Competition.is_published.is_(True), Competition.title.is_not(None))


@router.get("", response_model=SavedOpportunityListResponse)
def list_saved_opportunities(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SavedOpportunityListResponse:
    filters = (
        SavedCompetition.user_id == current_user.id,
        *_visible_competition_filters(),
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(SavedCompetition)
            .join(Competition, Competition.id == SavedCompetition.competition_id)
            .where(*filters)
        )
        or 0
    )
    rows = db.execute(
        select(SavedCompetition, Competition)
        .join(Competition, Competition.id == SavedCompetition.competition_id)
        .where(*filters)
        .order_by(SavedCompetition.created_at.desc(), Competition.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return SavedOpportunityListResponse(
        items=[
            SavedOpportunityItem(
                competition_id=competition.id,
                title=competition.title,
                format=competition.format,
                event_starts_on=competition.event_starts_on,
                event_starts_at=competition.event_starts_at,
                registration_deadline_on=competition.registration_deadline_on,
                registration_deadline_at=competition.registration_deadline_at,
                saved_at=saved.created_at,
            )
            for saved, competition in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.put(
    "/{competition_id}",
    response_model=SavedOpportunityMutationResponse,
    status_code=status.HTTP_200_OK,
)
def save_opportunity(
    competition_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SavedOpportunityMutationResponse:
    competition = db.scalar(
        select(Competition)
        .where(Competition.id == competition_id, *_visible_competition_filters())
        .with_for_update(read=True)
    )
    if competition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Competition not found")

    statement = (
        insert(SavedCompetition)
        .values(user_id=current_user.id, competition_id=competition_id)
        .on_conflict_do_nothing(
            index_elements=[SavedCompetition.user_id, SavedCompetition.competition_id]
        )
    )
    try:
        db.execute(statement)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return SavedOpportunityMutationResponse(competition_id=competition_id, saved=True)


@router.delete(
    "/{competition_id}", response_model=SavedOpportunityMutationResponse
)
def remove_saved_opportunity(
    competition_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SavedOpportunityMutationResponse:
    db.execute(
        SavedCompetition.__table__.delete().where(
            SavedCompetition.user_id == current_user.id,
            SavedCompetition.competition_id == competition_id,
        )
    )
    db.commit()
    return SavedOpportunityMutationResponse(competition_id=competition_id, saved=False)
