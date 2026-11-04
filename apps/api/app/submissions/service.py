"""Atomic creation of organizer-submitted events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.email_address import normalize_email
from app.auth.models import User
from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS
from app.models import (
    Category,
    Competition,
    CompetitionOrganizer,
    CompetitionSource,
    FieldEvidence,
    Organizer,
    OrganizerProfile,
    OrganizerProfileMember,
    OrganizerSubmission,
    OrganizerSubmissionCategory,
    SubmissionConsent,
    SubmissionReviewEvent,
)
from app.review.service import ReviewError, accept_evidence
from app.submissions.consent import (
    CURRENT_CONSENT_DECLARATION_TEXT,
    CURRENT_CONSENT_DECLARATION_VERSION,
)


class SubmissionValidationError(ValueError):
    """Submitted event information is invalid or unauthorized."""


class SubmissionAuthorizationError(SubmissionValidationError):
    """The authenticated user cannot submit under the selected organizer profile."""


class SubmissionNotFoundError(LookupError):
    """The requested submission does not exist."""


class SubmissionStateConflictError(ValueError):
    """The submission is not in a state that permits the requested review action."""


@dataclass(frozen=True)
class SubmittedEvent:
    title: str
    description: str
    organizer_name: str
    organizer_email: str
    official_url: str
    registration_url: str
    registration_deadline_at: datetime | None = None
    registration_deadline_on: date | None = None
    event_starts_at: datetime | None = None
    event_starts_on: date | None = None
    event_ends_at: datetime | None = None
    event_ends_on: date | None = None
    event_timezone: str | None = None
    format: str = ""
    location_name: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    location_country_code: str | None = None
    organizer_contact_name: str | None = None
    organizer_contact_email: str | None = None
    organizer_contact_phone: str | None = None
    registration_price_status: str = "unknown"
    registration_fee_amount: Decimal | None = None
    registration_fee_currency: str | None = None


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionValidationError(f"{field_name} is required")
    return value


def _optional_text(value: Any, field_name: str, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SubmissionValidationError(f"{field_name} must be non-empty text when provided")
    if max_length is not None and len(value) > max_length:
        raise SubmissionValidationError(f"{field_name} exceeds {max_length} characters")
    return value


def _validate_event(event: SubmittedEvent) -> dict[str, Any]:
    if not isinstance(event, SubmittedEvent):
        raise SubmissionValidationError("event must be SubmittedEvent")
    values = {field: getattr(event, field) for field in event.__dataclass_fields__}
    for field in (
        "title",
        "description",
        "organizer_name",
        "organizer_email",
        "official_url",
        "registration_url",
    ):
        values[field] = _required_text(values[field], field)
    try:
        values["organizer_email"] = normalize_email(values["organizer_email"])
        if values["organizer_contact_email"] is not None:
            if not isinstance(values["organizer_contact_email"], str):
                raise ValueError("organizer_contact_email must be text")
            values["organizer_contact_email"] = normalize_email(values["organizer_contact_email"])
    except ValueError as exc:
        raise SubmissionValidationError(str(exc)) from exc

    for field, max_length in (
        ("organizer_contact_name", None),
        ("organizer_contact_phone", 50),
        ("event_timezone", 64),
        ("location_name", None),
        ("location_city", None),
        ("location_region", None),
        ("location_country_code", 2),
    ):
        values[field] = _optional_text(values[field], field, max_length)

    if not isinstance(event.format, str) or event.format not in {"online", "offline", "hybrid"}:
        raise SubmissionValidationError("format must be online, offline, or hybrid")
    for field in (
        "registration_deadline",
        "event_starts",
        "event_ends",
    ):
        day_value = getattr(event, f"{field}_on")
        time_value = getattr(event, f"{field}_at")
        if (day_value is None) == (time_value is None):
            raise SubmissionValidationError(
                f"exactly one {field}_on or {field}_at value is required"
            )
        if day_value is not None and type(day_value) is not date:
            raise SubmissionValidationError(f"{field}_on must be a date")
        if time_value is not None:
            if not isinstance(time_value, datetime) or time_value.tzinfo is None:
                raise SubmissionValidationError(f"{field}_at must be a timezone-aware datetime")
            if time_value.utcoffset() is None:
                raise SubmissionValidationError(f"{field}_at must include a UTC offset")

    if event.event_starts_on and event.event_ends_on:
        if event.event_ends_on < event.event_starts_on:
            raise SubmissionValidationError("event end must not precede event start")
    if event.event_starts_at and event.event_ends_at:
        if event.event_ends_at < event.event_starts_at:
            raise SubmissionValidationError("event end must not precede event start")

    price_status = event.registration_price_status
    amount = event.registration_fee_amount
    currency = event.registration_fee_currency
    if not isinstance(price_status, str) or price_status not in {"free", "paid", "unknown"}:
        raise SubmissionValidationError("registration price status is invalid")
    if price_status == "paid":
        if amount is None or currency is None:
            raise SubmissionValidationError("paid registration requires amount and currency")
        try:
            amount = Decimal(amount)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SubmissionValidationError("registration fee amount is invalid") from exc
        if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99"):
            raise SubmissionValidationError("registration fee amount is out of range")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise SubmissionValidationError("registration fee currency must be a 3-letter code")
        values["registration_fee_amount"] = amount
        values["registration_fee_currency"] = currency.upper()
    elif amount is not None or currency is not None:
        raise SubmissionValidationError(
            "free or unknown registration cannot include a fee amount or currency"
        )

    if event.location_country_code is not None and (
        len(event.location_country_code) != 2 or not event.location_country_code.isalpha()
    ):
        raise SubmissionValidationError("location country code must contain two letters")
    return values


def create_submission(
    db: Session,
    *,
    user: User,
    event: SubmittedEvent,
    organizer_profile_id: UUID | None,
    category_ids: list[int] | tuple[int, ...],
    primary_category_id: int | None,
    declaration_version: str,
    declaration_text: str,
) -> OrganizerSubmission:
    """Create a pending submission, consent snapshot and category links atomically.

    The authenticated User object supplies ownership; identity is never accepted
    from SubmittedEvent or another request-controlled value.
    """
    try:
        if not isinstance(user, User) or user.id is None:
            raise SubmissionValidationError("an authenticated user is required")
        persisted_user = db.scalar(select(User).where(User.id == user.id, User.is_active.is_(True)))
        if persisted_user is None:
            raise SubmissionValidationError("authenticated user must exist and be active")
        event_values = _validate_event(event)

        if (
            declaration_version != CURRENT_CONSENT_DECLARATION_VERSION
            or declaration_text != CURRENT_CONSENT_DECLARATION_TEXT
        ):
            raise SubmissionValidationError("consent declaration is not current")

        if not isinstance(category_ids, (list, tuple)):
            raise SubmissionValidationError("category_ids must be a list of canonical IDs")
        if any(
            not isinstance(category_id, int) or isinstance(category_id, bool)
            for category_id in category_ids
        ):
            raise SubmissionValidationError("category IDs must be integers")
        if len(set(category_ids)) != len(category_ids):
            raise SubmissionValidationError("category_ids must not contain duplicates")
        if primary_category_id is not None and (
            not isinstance(primary_category_id, int) or isinstance(primary_category_id, bool)
        ):
            raise SubmissionValidationError("primary category ID must be an integer")
        if (category_ids and primary_category_id is None) or (
            primary_category_id is not None and primary_category_id not in category_ids
        ):
            raise SubmissionValidationError("primary category must be selected")
        existing_ids = (
            set(db.scalars(select(Category.id).where(Category.id.in_(category_ids))).all())
            if category_ids
            else set()
        )
        if existing_ids != set(category_ids):
            raise SubmissionValidationError("one or more selected categories do not exist")

        if organizer_profile_id is not None:
            membership = db.scalar(
                select(OrganizerProfileMember).where(
                    OrganizerProfileMember.organizer_profile_id == organizer_profile_id,
                    OrganizerProfileMember.user_id == persisted_user.id,
                )
            )
            if membership is None or db.get(OrganizerProfile, organizer_profile_id) is None:
                raise SubmissionAuthorizationError(
                    "user is not an authorized member of the organizer profile"
                )

        submission = OrganizerSubmission(
            submitter_user_id=persisted_user.id,
            organizer_profile_id=organizer_profile_id,
            status="pending",
            **event_values,
        )
        db.add(submission)
        db.flush()

        accepted_at = datetime.now(UTC)
        db.add(
            SubmissionConsent(
                submission_id=submission.id,
                submitter_user_id=persisted_user.id,
                submitter_email_snapshot=normalize_email(persisted_user.email),
                declaration_version=CURRENT_CONSENT_DECLARATION_VERSION,
                declaration_text=CURRENT_CONSENT_DECLARATION_TEXT,
                declaration_sha256=sha256(
                    CURRENT_CONSENT_DECLARATION_TEXT.encode("utf-8")
                ).hexdigest(),
                accepted_at=accepted_at,
            )
        )
        db.add_all(
            OrganizerSubmissionCategory(
                submission_id=submission.id,
                category_id=category_id,
                is_primary=(category_id == primary_category_id),
            )
            for category_id in category_ids
        )
        db.flush()
        db.commit()
        return submission
    except Exception:
        db.rollback()
        raise


def _admin_reviewer(db: Session, reviewer: User) -> User:
    if not isinstance(reviewer, User) or reviewer.id is None:
        raise SubmissionAuthorizationError("an authenticated administrator is required")
    persisted = db.scalar(
        select(User).where(
            User.id == reviewer.id,
            User.is_active.is_(True),
            User.is_admin.is_(True),
        )
    )
    if persisted is None:
        raise SubmissionAuthorizationError("an authenticated administrator is required")
    return persisted


def _locked_submission(db: Session, submission_id: int) -> OrganizerSubmission:
    submission = db.scalar(
        select(OrganizerSubmission).where(OrganizerSubmission.id == submission_id).with_for_update()
    )
    if submission is None:
        raise SubmissionNotFoundError("Submission not found")
    return submission


def start_submission_review(
    db: Session, *, submission_id: int, reviewer: User
) -> OrganizerSubmission:
    """Move a pending submission into review and append one admin audit event."""
    try:
        persisted_reviewer = _admin_reviewer(db, reviewer)
        submission = _locked_submission(db, submission_id)
        if submission.status != "pending":
            raise SubmissionStateConflictError("Only pending submissions can enter review")
        previous_status = submission.status
        submission.status = "under_review"
        db.add(
            SubmissionReviewEvent(
                submission_id=submission.id,
                reviewer_user_id=persisted_reviewer.id,
                previous_status=previous_status,
                new_status="under_review",
            )
        )
        db.flush()
        db.commit()
        return submission
    except Exception:
        db.rollback()
        raise


def reject_submission(
    db: Session, *, submission_id: int, reviewer: User, reason: str
) -> OrganizerSubmission:
    """Reject an under-review submission with an auditable admin reason."""
    try:
        persisted_reviewer = _admin_reviewer(db, reviewer)
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise SubmissionValidationError(
                "A rejection reason of at least 10 characters is required"
            )
        clean_reason = reason.strip()
        submission = _locked_submission(db, submission_id)
        if submission.status != "under_review":
            raise SubmissionStateConflictError("Only submissions under review can be rejected")
        previous_status = submission.status
        submission.status = "rejected"
        db.add(
            SubmissionReviewEvent(
                submission_id=submission.id,
                reviewer_user_id=persisted_reviewer.id,
                previous_status=previous_status,
                new_status="rejected",
                note=clean_reason,
            )
        )
        db.flush()
        db.commit()
        return submission
    except Exception:
        db.rollback()
        raise


def _date_evidence_value(day_value: date | None, time_value: datetime | None) -> dict[str, str]:
    if day_value is not None:
        return {"on": day_value.isoformat()}
    if time_value is not None:
        return {"at": time_value.isoformat()}
    raise SubmissionValidationError("required submission date is missing")


def _validate_approval_snapshot(
    db: Session, submission: OrganizerSubmission
) -> tuple[list[OrganizerSubmissionCategory], dict[int, Category]]:
    event = SubmittedEvent(
        title=submission.title,
        description=submission.description,
        organizer_name=submission.organizer_name,
        organizer_email=submission.organizer_email,
        organizer_contact_name=submission.organizer_contact_name,
        organizer_contact_email=submission.organizer_contact_email,
        organizer_contact_phone=submission.organizer_contact_phone,
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
    )
    _validate_event(event)

    consent = db.scalar(
        select(SubmissionConsent)
        .where(SubmissionConsent.submission_id == submission.id)
        .with_for_update()
    )
    if (
        consent is None
        or consent.submitter_user_id != submission.submitter_user_id
        or consent.submission_id != submission.id
        or not consent.declaration_version.strip()
        or not consent.declaration_text
        or consent.accepted_at is None
        or consent.accepted_at.tzinfo is None
        or sha256(consent.declaration_text.encode("utf-8")).hexdigest()
        != consent.declaration_sha256
    ):
        raise SubmissionValidationError("submission consent record is missing or invalid")
    try:
        normalize_email(consent.submitter_email_snapshot)
    except ValueError as exc:
        raise SubmissionValidationError("submission consent email snapshot is invalid") from exc

    links = db.scalars(
        select(OrganizerSubmissionCategory)
        .where(OrganizerSubmissionCategory.submission_id == submission.id)
        .with_for_update()
    ).all()
    category_ids = [link.category_id for link in links]
    if len(category_ids) != len(set(category_ids)):
        raise SubmissionValidationError("submission contains duplicate category assignments")
    if links and sum(bool(link.is_primary) for link in links) != 1:
        raise SubmissionValidationError("submission must have exactly one primary category")
    categories = (
        db.scalars(select(Category).where(Category.id.in_(category_ids))).all()
        if category_ids
        else []
    )
    categories_by_id = {category.id: category for category in categories}
    if set(categories_by_id) != set(category_ids):
        raise SubmissionValidationError("submission contains a missing canonical category")
    return links, categories_by_id


def _approval_evidence_specs(
    submission: OrganizerSubmission,
    category_links: list[OrganizerSubmissionCategory],
    categories: dict[int, Category],
) -> list[tuple[str, str, dict[str, Any], str | None]]:
    specs: list[tuple[str, str, dict[str, Any], str | None]] = [
        ("title", "", {"title": submission.title}, submission.title),
        ("description", "", {"description": submission.description}, submission.description),
        ("format", "", {"format": submission.format}, submission.format),
        (
            "official_url",
            "",
            {"official_url": submission.official_url},
            submission.official_url,
        ),
        (
            "registration_url",
            "",
            {"registration_url": submission.registration_url},
            submission.registration_url,
        ),
        (
            "registration_deadline",
            "",
            {
                "registration_deadline": _date_evidence_value(
                    submission.registration_deadline_on,
                    submission.registration_deadline_at,
                )
            },
            None,
        ),
        (
            "event_starts",
            "",
            {
                "event_starts": _date_evidence_value(
                    submission.event_starts_on, submission.event_starts_at
                )
            },
            None,
        ),
        (
            "event_ends",
            "",
            {
                "event_ends": _date_evidence_value(
                    submission.event_ends_on, submission.event_ends_at
                )
            },
            None,
        ),
    ]
    if submission.event_timezone is not None:
        specs.append(
            (
                "event_timezone",
                "",
                {"event_timezone": submission.event_timezone},
                submission.event_timezone,
            )
        )
    for link in category_links:
        category = categories[link.category_id]
        specs.append(
            (
                "category",
                category.slug,
                {
                    "slug": category.slug,
                    "name": category.name,
                    "is_primary": link.is_primary,
                },
                f"{category.name} ({category.slug})",
            )
        )
    _validate_approval_evidence_specs(specs)
    return specs


def _validate_approval_evidence_specs(
    specs: list[tuple[str, str, dict[str, Any], str | None]],
) -> None:
    unsupported = [field for field, *_ in specs if field not in REVIEW_SUPPORTED_EVIDENCE_FIELDS]
    if unsupported:
        raise SubmissionValidationError(
            f"approval evidence uses unsupported fields: {', '.join(unsupported)}"
        )
    required_fields = {
        "title",
        "description",
        "format",
        "official_url",
        "registration_url",
        "registration_deadline",
        "event_starts",
        "event_ends",
    }
    available_fields = {field for field, *_ in specs}
    missing_fields = required_fields - available_fields
    if missing_fields:
        raise SubmissionValidationError(
            f"required approval evidence is missing: {', '.join(sorted(missing_fields))}"
        )


def _record_approval_event(
    db: Session,
    *,
    submission: OrganizerSubmission,
    reviewer: User,
    previous_status: str,
) -> None:
    db.add(
        SubmissionReviewEvent(
            submission_id=submission.id,
            reviewer_user_id=reviewer.id,
            previous_status=previous_status,
            new_status="approved",
            note="Approved and published",
        )
    )


def _publish_approved_competition(competition: Competition) -> None:
    competition.is_published = True


def approve_submission(
    db: Session,
    *,
    submission_id: int,
    reviewer: User,
    existing_competition_id: int | None = None,
    existing_organizer_id: int | None = None,
) -> tuple[OrganizerSubmission, Competition]:
    """Approve a reviewed submission and publish its canonical competition atomically."""
    try:
        with db.begin_nested():
            persisted_reviewer = _admin_reviewer(db, reviewer)
            submission = _locked_submission(db, submission_id)
            if submission.status != "under_review":
                raise SubmissionStateConflictError("Only submissions under review can be approved")
            category_links, categories = _validate_approval_snapshot(db, submission)
            evidence_specs = _approval_evidence_specs(submission, category_links, categories)
            _validate_approval_evidence_specs(evidence_specs)

            if existing_competition_id is None:
                competition = Competition(is_published=False)
                db.add(competition)
                db.flush()
            else:
                competition = db.scalar(
                    select(Competition)
                    .where(Competition.id == existing_competition_id)
                    .with_for_update()
                )
                if competition is None:
                    raise SubmissionValidationError("selected competition does not exist")
                if not competition.is_published:
                    competition.is_published = False

            if existing_organizer_id is None:
                organizer = Organizer(name=submission.organizer_name)
                db.add(organizer)
                db.flush()
            else:
                organizer = db.scalar(
                    select(Organizer).where(Organizer.id == existing_organizer_id).with_for_update()
                )
                if organizer is None:
                    raise SubmissionValidationError("selected organizer does not exist")

            organizer_link = db.scalar(
                select(CompetitionOrganizer)
                .where(
                    CompetitionOrganizer.competition_id == competition.id,
                    CompetitionOrganizer.organizer_id == organizer.id,
                )
                .with_for_update()
            )
            if organizer_link is None:
                db.add(
                    CompetitionOrganizer(
                        competition_id=competition.id,
                        organizer_id=organizer.id,
                        role="organizer",
                    )
                )

            source = CompetitionSource(
                competition_id=competition.id,
                organizer_submission_id=submission.id,
                source_kind="organizer",
                source_url=submission.official_url,
                normalized_url=None,
            )
            db.add(source)
            db.flush()

            evidence_rows: list[FieldEvidence] = []
            for field_name, field_key, observed_value, excerpt in evidence_specs:
                evidence = FieldEvidence(
                    competition_id=competition.id,
                    source_id=source.id,
                    field_name=field_name,
                    field_key=field_key,
                    observed_value=observed_value,
                    evidence_excerpt=excerpt,
                )
                db.add(evidence)
                evidence_rows.append(evidence)
            db.flush()

            for evidence in evidence_rows:
                try:
                    accept_evidence(db, evidence.id, commit=False)
                except ReviewError as exc:
                    raise SubmissionValidationError(
                        f"approval evidence could not be accepted: {exc}"
                    ) from exc

            previous_status = submission.status
            submission.status = "approved"
            submission.approved_competition_id = competition.id
            _record_approval_event(
                db,
                submission=submission,
                reviewer=persisted_reviewer,
                previous_status=previous_status,
            )
            db.flush()
            _publish_approved_competition(competition)
            db.flush()

        db.commit()
        return submission, competition
    except Exception:
        db.rollback()
        raise
