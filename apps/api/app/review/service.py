from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionSource,
    FieldEvidence,
    SourcePublisher,
)


class ReviewError(ValueError):
    """A requested evidence review cannot be applied."""


class UnsupportedEvidenceField(ReviewError):
    """The evidence field has no explicit canonical mapping yet."""


def _validate_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{field_name} evidence must contain a non-empty string")
    return value.strip()


def _validate_format(value: Any) -> str:
    result = _validate_text(value, "format")
    if result not in {"online", "offline", "hybrid"}:
        raise ReviewError("format evidence must be online, offline, or hybrid")
    return result


def _validate_url(value: Any, field_name: str) -> str:
    result = _validate_text(value, field_name)
    parsed = urlsplit(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReviewError(f"{field_name} evidence must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ReviewError(f"{field_name} evidence must not contain URL credentials")
    return result


def _validate_timezone(value: Any) -> str:
    result = _validate_text(value, "event_timezone")
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise ReviewError("event_timezone evidence must be a recognized IANA timezone") from exc
    return result


def _validate_date_evidence(value: Any, field_name: str) -> tuple[date | datetime, str]:
    if not isinstance(value, dict) or len(value) != 1:
        raise ReviewError(f"{field_name} evidence must contain exactly one 'on' or 'at' value")
    if "on" in value:
        raw = value["on"]
        if not isinstance(raw, str):
            raise ReviewError(f"{field_name} 'on' value must be an ISO date string")
        try:
            return date.fromisoformat(raw), "on"
        except ValueError as exc:
            raise ReviewError(f"{field_name} 'on' value must be an ISO date") from exc
    if "at" in value:
        raw = value["at"]
        if not isinstance(raw, str):
            raise ReviewError(f"{field_name} 'at' value must be an ISO datetime string")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewError(f"{field_name} 'at' value must be an ISO datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReviewError(f"{field_name} 'at' value must include a timezone offset")
        return parsed, "at"
    raise ReviewError(f"{field_name} evidence must contain exactly one 'on' or 'at' value")


_SCALAR_FIELD_MAPPINGS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "title": ("title", lambda value: _validate_text(value, "title")),
    "description": ("description", lambda value: _validate_text(value, "description")),
    "eligibility_summary": (
        "eligibility_summary",
        lambda value: _validate_text(value, "eligibility_summary"),
    ),
    "format": ("format", _validate_format),
    "event_timezone": ("event_timezone", _validate_timezone),
    "official_url": ("official_url", lambda value: _validate_url(value, "official_url")),
    "registration_url": (
        "registration_url",
        lambda value: _validate_url(value, "registration_url"),
    ),
}

_DATE_FIELD_COLUMNS = {
    "registration_opens": ("registration_opens_on", "registration_opens_at"),
    "registration_deadline": ("registration_deadline_on", "registration_deadline_at"),
    "event_starts": ("event_starts_on", "event_starts_at"),
    "event_ends": ("event_ends_on", "event_ends_at"),
}


def _scalar_mapping(field_name: str) -> tuple[str, Callable[[Any], Any]]:
    if field_name not in REVIEW_SUPPORTED_EVIDENCE_FIELDS or field_name == "category":
        raise UnsupportedEvidenceField(
            f"evidence field '{field_name}' is not supported for scalar acceptance"
        )
    mapping = _SCALAR_FIELD_MAPPINGS.get(field_name)
    if mapping is not None:
        return mapping
    columns = _DATE_FIELD_COLUMNS.get(field_name)
    if columns is not None:
        return "|".join(columns), lambda value: _validate_date_evidence(value, field_name)
    raise UnsupportedEvidenceField(f"evidence field '{field_name}' is not supported for acceptance")


def _apply_canonical_value(competition: Competition, evidence: FieldEvidence) -> None:
    field_name, validator = _scalar_mapping(evidence.field_name)
    value_key = evidence.field_name
    if field_name == "title":
        value_key = "title"
    if not isinstance(evidence.observed_value, dict) or value_key not in evidence.observed_value:
        raise ReviewError(f"evidence value must contain the '{value_key}' field")
    value = validator(evidence.observed_value[value_key])
    if "|" in field_name:
        on_column, at_column = field_name.split("|")
        value, precision = value
        setattr(competition, on_column, value if precision == "on" else None)
        setattr(competition, at_column, value if precision == "at" else None)
    else:
        setattr(competition, field_name, value)


def _transaction(session: Session):
    return nullcontext() if session.in_transaction() else session.begin()


def list_pending_evidence(
    session: Session, limit: int = 100, field_name: str | None = None
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    statement = (
        select(FieldEvidence, Competition.title, CompetitionSource.source_url,
               SourcePublisher.name)
        .join(Competition, Competition.id == FieldEvidence.competition_id)
        .join(CompetitionSource, CompetitionSource.id == FieldEvidence.source_id)
        .outerjoin(SourcePublisher, SourcePublisher.id == CompetitionSource.publisher_id)
        .where(FieldEvidence.review_state == "candidate")
    )
    if field_name is not None:
        statement = statement.where(FieldEvidence.field_name == field_name)
    statement = statement.order_by(FieldEvidence.observed_at, FieldEvidence.id).limit(limit)
    rows = session.execute(statement).all()
    return [
        {
            "evidence_id": evidence.id,
            "competition_id": evidence.competition_id,
            "canonical_title": canonical_title,
            "field_name": evidence.field_name,
            "field_key": evidence.field_key,
            "category_name": (
                evidence.observed_value.get("name")
                if evidence.field_name == "category"
                else None
            ),
            "category_slug": evidence.field_key if evidence.field_name == "category" else None,
            "observed_value": evidence.observed_value,
            "evidence_excerpt": evidence.evidence_excerpt,
            "observed_at": evidence.observed_at,
            "source_url": source_url,
            "publisher": publisher_name,
        }
        for evidence, canonical_title, source_url, publisher_name in rows
    ]


def get_competition_detail(session: Session, competition_id: int) -> dict[str, Any]:
    competition = session.get(Competition, competition_id)
    if competition is None:
        raise ReviewError(f"competition {competition_id} was not found")
    sources = session.scalars(
        select(CompetitionSource)
        .where(CompetitionSource.competition_id == competition_id)
        .order_by(CompetitionSource.id)
    ).all()
    result = {
        "competition": {
            "id": competition.id,
            "title": competition.title,
            "description": competition.description,
            "eligibility_summary": competition.eligibility_summary,
            "format": competition.format,
            "official_url": competition.official_url,
            "registration_url": competition.registration_url,
            "registration_opens_at": competition.registration_opens_at,
            "registration_opens_on": competition.registration_opens_on,
            "registration_deadline_at": competition.registration_deadline_at,
            "registration_deadline_on": competition.registration_deadline_on,
            "event_starts_at": competition.event_starts_at,
            "event_starts_on": competition.event_starts_on,
            "event_ends_at": competition.event_ends_at,
            "event_ends_on": competition.event_ends_on,
            "event_timezone": competition.event_timezone,
        },
        "sources": [],
    }
    for source in sources:
        result["sources"].append(
            {
                "id": source.id,
                "source_url": source.source_url,
                "normalized_url": source.normalized_url,
                "source_kind": source.source_kind,
                "publisher": source.publisher.name if source.publisher else None,
                "discovered_at": source.discovered_at,
                "check_status": source.check_status,
                "evidence": [
                    {
                        "id": evidence.id,
                        "field_name": evidence.field_name,
                        "field_key": evidence.field_key,
                        "category_name": (
                            evidence.observed_value.get("name")
                            if evidence.field_name == "category"
                            else None
                        ),
                        "category_slug": (
                            evidence.field_key if evidence.field_name == "category" else None
                        ),
                        "observed_value": evidence.observed_value,
                        "evidence_excerpt": evidence.evidence_excerpt,
                        "confidence": evidence.confidence,
                        "observed_at": evidence.observed_at,
                        "review_state": evidence.review_state,
                        "reviewed_at": evidence.reviewed_at,
                    }
                    for evidence in session.scalars(
                        select(FieldEvidence)
                        .where(FieldEvidence.source_id == source.id)
                        .order_by(FieldEvidence.observed_at, FieldEvidence.id)
                    ).all()
                ],
            }
        )
    return result


def accept_evidence(session: Session, evidence_id: int, *, commit: bool = True) -> FieldEvidence:
    """Accept candidate evidence, optionally leaving the root transaction to its caller."""
    if not commit and not session.in_transaction():
        raise ReviewError("caller-managed evidence acceptance requires an active transaction")
    transaction = _transaction(session) if commit else nullcontext()
    with transaction, session.begin_nested():
        evidence = session.scalar(
            select(FieldEvidence).where(FieldEvidence.id == evidence_id).with_for_update()
        )
        if evidence is None:
            raise ReviewError(f"evidence {evidence_id} was not found")
        if evidence.review_state != "candidate":
            raise ReviewError(f"evidence {evidence_id} is already {evidence.review_state}")
        competition = session.scalar(
            select(Competition)
            .where(Competition.id == evidence.competition_id)
            .with_for_update()
        )
        if competition is None:
            raise ReviewError(f"competition {evidence.competition_id} was not found")
        category: Category | None = None
        category_is_primary = False
        category_link: CompetitionCategory | None = None
        if evidence.field_name == "category":
            category, category_is_primary = _resolve_category_evidence(session, evidence)
            category_link = session.scalar(
                select(CompetitionCategory)
                .where(
                    CompetitionCategory.competition_id == competition.id,
                    CompetitionCategory.category_id == category.id,
                )
                .with_for_update()
            )
            if category_is_primary:
                another_primary = session.scalar(
                    select(CompetitionCategory.category_id).where(
                        CompetitionCategory.competition_id == competition.id,
                        CompetitionCategory.is_primary.is_(True),
                        CompetitionCategory.category_id != category.id,
                    )
                )
                if another_primary is not None:
                    raise ReviewError(
                        "competition already has a primary category; unassign it before "
                        "assigning a new primary category"
                    )
        else:
            # Keep scalar evidence behavior unchanged.
            _apply_canonical_value(competition, evidence)
        previous = session.scalars(
            select(FieldEvidence)
            .where(
                FieldEvidence.competition_id == evidence.competition_id,
                FieldEvidence.field_name == evidence.field_name,
                FieldEvidence.field_key == evidence.field_key,
                FieldEvidence.review_state == "selected",
                FieldEvidence.id != evidence.id,
            )
            .with_for_update()
        ).all()
        now = datetime.now(UTC)
        for previous_evidence in previous:
            previous_evidence.review_state = "superseded"
        if category is not None:
            # Release the selected-evidence unique key before selecting its replacement.
            session.flush()
            if category_link is None:
                session.add(
                    CompetitionCategory(
                        competition_id=competition.id,
                        category_id=category.id,
                        is_primary=category_is_primary,
                    )
                )
            elif category_is_primary:
                category_link.is_primary = True
        evidence.review_state = "selected"
        evidence.reviewed_at = now
        session.flush()
    return evidence


def _resolve_category_evidence(
    session: Session, evidence: FieldEvidence
) -> tuple[Category, bool]:
    slug = evidence.field_key
    if not slug or len(slug) > 100 or slug.strip() != slug:
        raise ReviewError("category evidence field_key must be a canonical category slug")
    value = evidence.observed_value
    if not isinstance(value, dict) or value.get("slug") != slug:
        raise ReviewError("category evidence value slug must match its field_key")
    is_primary = value.get("is_primary", False)
    if not isinstance(is_primary, bool):
        raise ReviewError("category evidence is_primary value must be a boolean")
    category = session.scalar(select(Category).where(Category.slug == slug))
    if category is None:
        raise ReviewError(f"canonical category '{slug}' does not exist")
    return category, is_primary


def create_category_proposal(
    session: Session,
    competition_id: int,
    category_slug: str,
    source_id: int,
    *,
    is_primary: bool = False,
) -> FieldEvidence:
    with _transaction(session), session.begin_nested():
        competition = session.scalar(
            select(Competition)
            .where(Competition.id == competition_id)
            .with_for_update()
        )
        if competition is None:
            raise ReviewError(f"competition {competition_id} was not found")
        category = session.scalar(select(Category).where(Category.slug == category_slug))
        if category is None:
            raise ReviewError(f"canonical category '{category_slug}' does not exist")
        source = session.scalar(
            select(CompetitionSource).where(
                CompetitionSource.id == source_id,
                CompetitionSource.competition_id == competition_id,
            )
        )
        if source is None:
            raise ReviewError(
                f"source {source_id} does not belong to competition {competition_id}"
            )
        proposal = FieldEvidence(
            competition_id=competition_id,
            source_id=source_id,
            field_name="category",
            field_key=category.slug,
            observed_value={
                "slug": category.slug,
                "name": category.name,
                "is_primary": is_primary,
            },
            evidence_excerpt=f"{category.name} ({category.slug})",
            review_state="candidate",
        )
        session.add(proposal)
        session.flush()
    return proposal


def unassign_category(session: Session, competition_id: int, category_slug: str) -> None:
    with _transaction(session), session.begin_nested():
        competition = session.scalar(
            select(Competition)
            .where(Competition.id == competition_id)
            .with_for_update()
        )
        if competition is None:
            raise ReviewError(f"competition {competition_id} was not found")
        category = session.scalar(select(Category).where(Category.slug == category_slug))
        if category is None:
            raise ReviewError(f"canonical category '{category_slug}' does not exist")
        link = session.scalar(
            select(CompetitionCategory)
            .where(
                CompetitionCategory.competition_id == competition_id,
                CompetitionCategory.category_id == category.id,
            )
            .with_for_update()
        )
        if link is None:
            raise ReviewError(
                f"category '{category_slug}' is not assigned to competition {competition_id}"
            )
        session.delete(link)
        selected_evidence = session.scalars(
            select(FieldEvidence)
            .where(
                FieldEvidence.competition_id == competition_id,
                FieldEvidence.field_name == "category",
                FieldEvidence.field_key == category.slug,
                FieldEvidence.review_state == "selected",
            )
            .with_for_update()
        ).all()
        now = datetime.now(UTC)
        for evidence in selected_evidence:
            evidence.review_state = "superseded"
            evidence.reviewed_at = now
        session.flush()


def reject_evidence(session: Session, evidence_id: int) -> FieldEvidence:
    with _transaction(session), session.begin_nested():
        evidence = session.scalar(
            select(FieldEvidence).where(FieldEvidence.id == evidence_id).with_for_update()
        )
        if evidence is None:
            raise ReviewError(f"evidence {evidence_id} was not found")
        if evidence.review_state != "candidate":
            raise ReviewError(f"evidence {evidence_id} is already {evidence.review_state}")
        evidence.review_state = "rejected"
        evidence.reviewed_at = datetime.now(UTC)
        session.flush()
    return evidence
