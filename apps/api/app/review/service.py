from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import User
from app.discovery.candidates import (
    DateOnlyObservation,
    DateTimeObservation,
    parse_date_observation,
)
from app.discovery.registry import RegistryError, authorize_source_operation
from app.discovery.urls import UnsafeSourceUrl, validate_source_url
from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionSource,
    EvidenceReviewEvent,
    FieldEvidence,
    OrganizerSubmission,
    SourcePublisher,
    SubmissionConsent,
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
    try:
        observation = parse_date_observation(value, field_name)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
    if isinstance(observation, DateOnlyObservation):
        return observation.on, "on"
    if isinstance(observation, DateTimeObservation):
        return observation.at, "at"
    raise ReviewError(f"{field_name} evidence must contain a date observation")


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

_SCALAR_EVIDENCE_FIELDS = frozenset(_SCALAR_FIELD_MAPPINGS) | frozenset(
    _DATE_FIELD_COLUMNS
)


def _validate_scalar_field_key(field_name: str, field_key: str) -> None:
    """Scalar evidence has one canonical key; category evidence uses its own path."""
    if field_name in _SCALAR_EVIDENCE_FIELDS and field_key != "":
        raise ReviewError(f"scalar evidence field '{field_name}' must have an empty field_key")


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


def _validate_event_date_precision(competition: Competition, evidence: FieldEvidence) -> None:
    """Reject an event range whose resulting endpoints use different precision."""
    if evidence.field_name not in {"event_starts", "event_ends"}:
        return
    if (
        not isinstance(evidence.observed_value, dict)
        or evidence.field_name not in evidence.observed_value
    ):
        raise ReviewError(
            f"evidence value must contain the '{evidence.field_name}' field"
        )
    _, evidence_precision = _validate_date_evidence(
        evidence.observed_value[evidence.field_name], evidence.field_name
    )

    start_precision = _canonical_event_date_precision(competition, "event_starts")
    end_precision = _canonical_event_date_precision(competition, "event_ends")
    if evidence.field_name == "event_starts":
        start_precision = evidence_precision
    else:
        end_precision = evidence_precision

    if start_precision is not None and end_precision is not None:
        if start_precision != end_precision:
            raise ReviewError(
                "mixed-precision event range requires human resolution"
            )


def _canonical_event_date_precision(competition: Competition, field_name: str) -> str | None:
    date_value = getattr(competition, f"{field_name}_on")
    datetime_value = getattr(competition, f"{field_name}_at")
    if date_value is not None and datetime_value is not None:
        raise ReviewError(f"canonical {field_name} has conflicting date precision")
    if date_value is not None:
        return "on"
    if datetime_value is not None:
        return "at"
    return None


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
            "is_published": competition.is_published,
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
            "categories": [
                {
                    "name": link.category.name,
                    "slug": link.category.slug,
                    "is_primary": link.is_primary,
                }
                for link in sorted(
                    competition.category_links,
                    key=lambda item: (item.category.name.lower(), item.category.slug),
                )
            ],
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
                "registry_source_id": source.registry_source_id,
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


def get_evidence_detail(session: Session, evidence_id: int) -> dict[str, Any]:
    evidence = session.get(FieldEvidence, evidence_id)
    if evidence is None:
        raise ReviewError(f"evidence {evidence_id} was not found")

    competition_detail = get_competition_detail(session, evidence.competition_id)
    source_detail = next(
        (
            source
            for source in competition_detail["sources"]
            if source["id"] == evidence.source_id
        ),
        None,
    )
    if source_detail is None:
        raise ReviewError("evidence source no longer exists")
    evidence_detail = next(
        (item for item in source_detail["evidence"] if item["id"] == evidence_id),
        None,
    )
    if evidence_detail is None:
        raise ReviewError(f"evidence {evidence_id} was not found")

    source_context = {key: value for key, value in source_detail.items() if key != "evidence"}
    review_events = session.scalars(
        select(EvidenceReviewEvent)
        .where(EvidenceReviewEvent.evidence_id == evidence_id)
        .order_by(EvidenceReviewEvent.created_at, EvidenceReviewEvent.id)
    ).all()
    return {
        "evidence": evidence_detail,
        "competition": competition_detail["competition"],
        "source": source_context,
        "review_events": [
            {
                "id": event.id,
                "reviewer_user_id": event.reviewer_user_id,
                "action": event.action,
                "previous_state": event.previous_state,
                "new_state": event.new_state,
                "created_at": event.created_at,
            }
            for event in review_events
        ],
    }


def accept_evidence(
    session: Session,
    evidence_id: int,
    *,
    reviewer: User | None = None,
    commit: bool = True,
) -> FieldEvidence:
    """Accept candidate evidence, optionally leaving the root transaction to its caller."""
    if not commit and not session.in_transaction():
        raise ReviewError("caller-managed evidence acceptance requires an active transaction")
    transaction = _transaction(session) if commit else nullcontext()
    with transaction, session.begin_nested():
        evidence = session.get(FieldEvidence, evidence_id)
        if evidence is None:
            raise ReviewError(f"evidence {evidence_id} was not found")
        competition = session.scalar(
            select(Competition)
            .where(Competition.id == evidence.competition_id)
            .with_for_update()
        )
        if competition is None:
            raise ReviewError(f"competition {evidence.competition_id} was not found")
        evidence = session.scalar(
            select(FieldEvidence).where(FieldEvidence.id == evidence_id).with_for_update()
        )
        if evidence is None or evidence.review_state != "candidate":
            state = "missing" if evidence is None else evidence.review_state
            raise ReviewError(f"evidence {evidence_id} is already {state}")
        source = session.scalar(
            select(CompetitionSource)
            .where(CompetitionSource.id == evidence.source_id)
            .with_for_update()
        )
        if source is None:
            raise ReviewError("evidence source no longer exists")
        if not source.registry_source_id:
            raise ReviewError(
                "legacy evidence without trusted registry identity cannot be accepted"
            )
        try:
            registered = authorize_source_operation(source.registry_source_id, "internal_review")
        except RegistryError as exc:
            raise ReviewError(str(exc)) from exc
        try:
            validate_source_url(source.source_url or "", set(registered.access["allowed_hosts"]))
        except UnsafeSourceUrl as exc:
            raise ReviewError("persisted source URL is not allowed for internal review") from exc
        if not isinstance(reviewer, User) or reviewer.id is None:
            raise ReviewError("a persisted reviewer identity is required")
        persisted_reviewer = session.scalar(
            select(User).where(User.id == reviewer.id, User.is_active.is_(True))
        )
        if persisted_reviewer is None:
            raise ReviewError("reviewer does not exist or is inactive")
        _apply_accepted_evidence(session, evidence, competition, persisted_reviewer)
    return evidence


def accept_submission_evidence(
    session: Session,
    evidence_id: int,
    *,
    submission_id: int,
    source_id: int,
    competition_id: int,
    validated_evidence_specs: dict[
        int, tuple[str, str, dict[str, Any], str | None]
    ],
    reviewer: User,
) -> FieldEvidence:
    """Accept only evidence matching a validated, locked organizer submission snapshot."""
    if not session.in_transaction():
        raise ReviewError("submission evidence acceptance requires the approval transaction")
    submission = session.scalar(
        select(OrganizerSubmission)
        .where(OrganizerSubmission.id == submission_id)
        .with_for_update()
    )
    if submission is None or submission.status != "under_review":
        raise ReviewError("submission is not locked in an approvable review state")
    consent = session.scalar(
        select(SubmissionConsent).where(
            SubmissionConsent.submission_id == submission.id,
            SubmissionConsent.submitter_user_id == submission.submitter_user_id,
        )
    )
    if consent is None:
        raise ReviewError("submission consent is missing or does not match its submitter")
    if not isinstance(reviewer, User) or reviewer.id is None:
        raise ReviewError("an authenticated administrator is required")
    persisted_reviewer = session.scalar(
        select(User).where(
            User.id == reviewer.id,
            User.is_active.is_(True),
            User.is_admin.is_(True),
        )
    )
    if persisted_reviewer is None:
        raise ReviewError("an authenticated administrator is required")
    if not session.in_transaction():
        raise ReviewError("submission approval transaction is no longer active")
    competition = session.scalar(
        select(Competition).where(Competition.id == competition_id).with_for_update()
    )
    if competition is None:
        raise ReviewError("submission evidence competition was not found")
    evidence = session.scalar(
        select(FieldEvidence)
        .where(
            FieldEvidence.id == evidence_id,
            FieldEvidence.competition_id == competition_id,
            FieldEvidence.source_id == source_id,
        )
        .with_for_update()
    )
    if evidence is None or evidence.review_state != "candidate":
        raise ReviewError("evidence is not a candidate in this submission source")
    expected_spec = validated_evidence_specs.get(evidence_id)
    if expected_spec is None:
        raise ReviewError("evidence ID is not part of the validated submission snapshot")
    source = session.scalar(
        select(CompetitionSource).where(
            CompetitionSource.id == source_id,
            CompetitionSource.competition_id == competition_id,
            CompetitionSource.organizer_submission_id == submission.id,
            CompetitionSource.source_kind == "organizer",
            CompetitionSource.source_url == submission.official_url,
        )
    )
    if source is None:
        raise ReviewError("evidence source is not linked to the locked submission")
    field_name, field_key, observed_value, excerpt = expected_spec
    if (
        evidence.field_name != field_name
        or evidence.field_key != field_key
        or evidence.observed_value != observed_value
        or evidence.evidence_excerpt != excerpt
    ):
        raise ReviewError("evidence is outside the validated submission snapshot")
    _apply_accepted_evidence(session, evidence, competition, persisted_reviewer)
    return evidence


def _apply_accepted_evidence(
    session: Session,
    evidence: FieldEvidence,
    competition: Competition,
    reviewer: User,
) -> None:
    if evidence.field_name != "category":
        _validate_scalar_field_key(evidence.field_name, evidence.field_key)
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
        _validate_event_date_precision(competition, evidence)
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
        .order_by(FieldEvidence.id)
        .with_for_update()
    ).all()
    for previous_evidence in previous:
        session.add(EvidenceReviewEvent(
            evidence_id=previous_evidence.id,
            reviewer_user_id=reviewer.id,
            action="superseded",
            previous_state="selected",
            new_state="superseded",
        ))
    now = datetime.now(UTC)
    for previous_evidence in previous:
        previous_evidence.review_state = "superseded"
    if category is not None:
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
    session.add(EvidenceReviewEvent(
        evidence_id=evidence.id,
        reviewer_user_id=reviewer.id,
        action="accepted",
        previous_state="candidate",
        new_state="selected",
    ))
    session.flush()


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


def unassign_category(
    session: Session,
    competition_id: int,
    category_slug: str,
    *,
    reviewer: User | None = None,
) -> None:
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
        if not isinstance(reviewer, User) or reviewer.id is None:
            raise ReviewError("a persisted reviewer identity is required")
        persisted_reviewer = session.scalar(
            select(User).where(User.id == reviewer.id, User.is_active.is_(True))
        )
        if persisted_reviewer is None:
            raise ReviewError("reviewer does not exist or is inactive")

        session.delete(link)
        now = datetime.now(UTC)
        for evidence in selected_evidence:
            evidence.review_state = "superseded"
            evidence.reviewed_at = now
            session.add(
                EvidenceReviewEvent(
                    evidence_id=evidence.id,
                    reviewer_user_id=persisted_reviewer.id,
                    action="superseded",
                    previous_state="selected",
                    new_state="superseded",
                )
            )
        session.flush()


def reject_evidence(
    session: Session, evidence_id: int, *, reviewer: User | None = None
) -> FieldEvidence:
    with _transaction(session), session.begin_nested():
        evidence = session.scalar(
            select(FieldEvidence).where(FieldEvidence.id == evidence_id).with_for_update()
        )
        if evidence is None:
            raise ReviewError(f"evidence {evidence_id} was not found")
        if evidence.review_state != "candidate":
            raise ReviewError(f"evidence {evidence_id} is already {evidence.review_state}")
        if not isinstance(reviewer, User) or reviewer.id is None:
            raise ReviewError("a persisted reviewer identity is required")
        persisted_reviewer = session.scalar(
            select(User).where(User.id == reviewer.id, User.is_active.is_(True))
        )
        if persisted_reviewer is None:
            raise ReviewError("reviewer does not exist or is inactive")
        evidence.review_state = "rejected"
        evidence.reviewed_at = datetime.now(UTC)
        session.add(EvidenceReviewEvent(
            evidence_id=evidence.id,
            reviewer_user_id=persisted_reviewer.id,
            action="rejected",
            previous_state="candidate",
            new_state="rejected",
        ))
        session.flush()
    return evidence
