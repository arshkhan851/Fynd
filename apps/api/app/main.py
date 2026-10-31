import os
from datetime import date, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import check_database_connectivity, get_db
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionOrganizer,
    CompetitionSource,
)

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


class CategoryCount(BaseModel):
    name: str
    slug: str
    competition_count: int


class PublicCategory(BaseModel):
    name: str
    slug: str
    is_primary: bool


class PublicOrganizer(BaseModel):
    name: str
    role: str | None
    website_url: str | None


class PublicPrize(BaseModel):
    award_name: str | None
    place: str | None
    amount: str | None
    currency: str | None
    details: str | None


class PublicSource(BaseModel):
    source_kind: str
    source_url: str
    publisher_name: str | None
    publisher_homepage_url: str | None


class CompetitionDetailResponse(BaseModel):
    id: int
    title: str
    description: str | None
    eligibility_summary: str | None
    format: str | None
    official_url: str | None
    registration_url: str | None
    registration_opens_on: date | None
    registration_opens_at: datetime | None
    registration_deadline_on: date | None
    registration_deadline_at: datetime | None
    event_starts_on: date | None
    event_starts_at: datetime | None
    event_ends_on: date | None
    event_ends_at: datetime | None
    event_timezone: str | None
    categories: list[PublicCategory]
    organizers: list[PublicOrganizer]
    prizes: list[PublicPrize]
    sources: list[PublicSource]


def _safe_public_url(value: str | None) -> str | None:
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    return value


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
    q: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
) -> CompetitionListResponse:
    visible_competitions = Competition.title.is_not(None)
    search_term = q.strip() if q else ""
    filters = [visible_competitions]
    if search_term:
        pattern = f"%{search_term}%"
        filters.append(
            or_(Competition.title.ilike(pattern), Competition.description.ilike(pattern))
        )
    if category:
        filters.append(
            Competition.category_links.any(
                CompetitionCategory.category.has(Category.slug == category)
            )
        )
    try:
        total = session.scalar(
            select(func.count()).select_from(Competition).where(*filters)
        )
        competitions = session.scalars(
            select(Competition)
            .where(*filters)
            .order_by(Competition.discovered_at.desc(), Competition.id.desc())
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
                official_url=_safe_public_url(competition.official_url),
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


@app.get("/categories", response_model=list[CategoryCount])
def list_categories(session: Annotated[Session, Depends(get_db)]) -> list[CategoryCount]:
    eligible_counts = (
        select(
            CompetitionCategory.category_id.label("category_id"),
            func.count(CompetitionCategory.competition_id).label("competition_count"),
        )
        .join(Competition, Competition.id == CompetitionCategory.competition_id)
        .where(Competition.title.is_not(None))
        .group_by(CompetitionCategory.category_id)
        .subquery()
    )
    statement = (
        select(
            Category.name,
            Category.slug,
            func.coalesce(eligible_counts.c.competition_count, 0).label("competition_count"),
        )
        .outerjoin(eligible_counts, eligible_counts.c.category_id == Category.id)
        .order_by(func.lower(Category.name), Category.slug)
    )
    try:
        rows = session.execute(statement).all()
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "unavailable"}) from None
    return [CategoryCount(**row._mapping) for row in rows]


@app.get(
    "/competitions/{competition_id}",
    response_model=CompetitionDetailResponse,
    responses={
        404: {"description": "Competition not found or has no canonical title"},
        503: {"description": "Database unavailable"},
    },
)
def get_competition(
    competition_id: Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)],
    session: Annotated[Session, Depends(get_db)],
) -> CompetitionDetailResponse:
    statement = (
        select(Competition)
        .where(
            Competition.id == competition_id,
            Competition.title.is_not(None),
        )
        .options(
            selectinload(Competition.category_links).selectinload(
                CompetitionCategory.category
            ),
            selectinload(Competition.organizer_links).selectinload(
                CompetitionOrganizer.organizer
            ),
            selectinload(Competition.prizes),
            selectinload(Competition.sources).selectinload(CompetitionSource.publisher),
        )
    )
    try:
        competition = session.scalars(statement).one_or_none()
    except Exception:
        raise HTTPException(
            status_code=503, detail={"status": "unavailable"}
        ) from None

    if competition is None:
        raise HTTPException(status_code=404, detail="Competition not found")

    return CompetitionDetailResponse(
        id=competition.id,
        title=competition.title,
        description=competition.description,
        eligibility_summary=competition.eligibility_summary,
        format=competition.format,
        official_url=_safe_public_url(competition.official_url),
        registration_url=_safe_public_url(competition.registration_url),
        registration_opens_on=competition.registration_opens_on,
        registration_opens_at=competition.registration_opens_at,
        registration_deadline_on=competition.registration_deadline_on,
        registration_deadline_at=competition.registration_deadline_at,
        event_starts_on=competition.event_starts_on,
        event_starts_at=competition.event_starts_at,
        event_ends_on=competition.event_ends_on,
        event_ends_at=competition.event_ends_at,
        event_timezone=competition.event_timezone,
        categories=[
            PublicCategory(
                name=link.category.name,
                slug=link.category.slug,
                is_primary=link.is_primary,
            )
            for link in sorted(
                competition.category_links, key=lambda item: item.category.name.lower()
            )
        ],
        organizers=[
            PublicOrganizer(
                name=link.organizer.name,
                role=link.role,
                website_url=_safe_public_url(link.organizer.website_url),
            )
            for link in sorted(
                competition.organizer_links, key=lambda item: item.organizer.name.lower()
            )
        ],
        prizes=[
            PublicPrize(
                award_name=prize.award_name,
                place=prize.place,
                amount=format(prize.amount, "f") if prize.amount is not None else None,
                currency=prize.currency,
                details=prize.details,
            )
            for prize in sorted(
                competition.prizes,
                key=lambda item: (item.place or "", item.award_name or ""),
            )
        ],
        sources=[
            PublicSource(
                source_kind=source.source_kind,
                source_url=source_url,
                publisher_name=source.publisher.name if source.publisher else None,
                publisher_homepage_url=(
                    _safe_public_url(source.publisher.homepage_url)
                    if source.publisher
                    else None
                ),
            )
            for source in sorted(
                competition.sources,
                key=lambda item: (item.source_kind, item.source_url or ""),
            )
            if (source_url := _safe_public_url(source.source_url)) is not None
        ],
    )
