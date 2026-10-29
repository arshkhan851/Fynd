import os
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import check_database_connectivity, get_db
from app.models import Competition

app = FastAPI(title=os.getenv("APP_NAME", "api"))

_default_origins = "http://localhost:3000,http://127.0.0.1:3000"
_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET"],
    allow_headers=[],
)


class CompetitionListItem(BaseModel):
    id: int
    title: str
    format: str | None
    official_url: str | None
    event_starts_on: date | None
    event_starts_at: datetime | None
    registration_deadline_on: date | None
    registration_deadline_at: datetime | None


class CompetitionListResponse(BaseModel):
    items: list[CompetitionListItem]
    total: int
    limit: int
    offset: int


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def database_health() -> dict:
    try:
        check_database_connectivity()
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "unavailable"}) from None
    return {"status": "ok"}


@app.get("/competitions", response_model=CompetitionListResponse)
def list_competitions(
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CompetitionListResponse:
    visible_competitions = Competition.title.is_not(None)
    try:
        total = session.scalar(
            select(func.count()).select_from(Competition).where(visible_competitions)
        )
        competitions = session.scalars(
            select(Competition)
            .where(visible_competitions)
            .order_by(Competition.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
    except Exception:
        raise HTTPException(
            status_code=503, detail={"status": "unavailable"}
        ) from None

    return CompetitionListResponse(
        items=[
            CompetitionListItem(
                id=competition.id,
                title=competition.title,
                format=competition.format,
                official_url=competition.official_url,
                event_starts_on=competition.event_starts_on,
                event_starts_at=competition.event_starts_at,
                registration_deadline_on=competition.registration_deadline_on,
                registration_deadline_at=competition.registration_deadline_at,
            )
            for competition in competitions
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
