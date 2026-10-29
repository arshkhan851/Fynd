from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Competition, CompetitionSource, FieldEvidence, SourcePublisher


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


def _scalar_mapping(field_name: str) -> tuple[str, Callable[[Any], Any]]:
    mappings: dict[str, tuple[str, Callable[[Any], Any]]] = {
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
    mapping = mappings.get(field_name)
    if mapping is not None:
        return mapping
    date_columns = {
        "registration_opens": ("registration_opens_on", "registration_opens_at"),
        "registration_deadline": ("registration_deadline_on", "registration_deadline_at"),
        "event_starts": ("event_starts_on", "event_starts_at"),
        "event_ends": ("event_ends_on", "event_ends_at"),
    }
    columns = date_columns.get(field_name)
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


def list_pending_evidence(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    rows = session.execute(
        select(FieldEvidence, Competition.title, CompetitionSource.source_url,
               SourcePublisher.name)
        .join(Competition, Competition.id == FieldEvidence.competition_id)
        .join(CompetitionSource, CompetitionSource.id == FieldEvidence.source_id)
        .outerjoin(SourcePublisher, SourcePublisher.id == CompetitionSource.publisher_id)
        .where(FieldEvidence.review_state == "candidate")
        .order_by(FieldEvidence.observed_at, FieldEvidence.id)
        .limit(limit)
    ).all()
    return [
        {
            "evidence_id": evidence.id,
            "competition_id": evidence.competition_id,
            "canonical_title": canonical_title,
            "field_name": evidence.field_name,
            "field_key": evidence.field_key,
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


def accept_evidence(session: Session, evidence_id: int) -> FieldEvidence:
    with _transaction(session), session.begin_nested():
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
        # Validate support and value before changing any review state.
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
        evidence.review_state = "selected"
        evidence.reviewed_at = now
        session.flush()
    return evidence


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
