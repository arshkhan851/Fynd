from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal


def _validate_excerpt(excerpt: str | None) -> None:
    if excerpt is not None and (not isinstance(excerpt, str) or not excerpt.strip()):
        raise ValueError("observation excerpt must be a non-empty string when supplied")


@dataclass(frozen=True, slots=True)
class DateOnlyObservation:
    on: date

    def __post_init__(self) -> None:
        # datetime subclasses date, so isinstance(on, date) would accept the wrong precision.
        if type(self.on) is not date:
            raise ValueError("date-only observation must contain a date, not a datetime")


@dataclass(frozen=True, slots=True)
class DateTimeObservation:
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.at, datetime):
            raise ValueError("datetime observation must contain a datetime")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("datetime observation must be timezone-aware")


DateObservation = DateOnlyObservation | DateTimeObservation


def parse_date_observation(value: object, field_name: str) -> DateObservation:
    """Parse the review contract's one-key date/datetime payload into a typed observation."""
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError(f"{field_name} evidence must contain exactly one 'on' or 'at' value")
    if "on" in value:
        raw = value["on"]
        if not isinstance(raw, str):
            raise ValueError(f"{field_name} 'on' value must be an ISO date string")
        try:
            return DateOnlyObservation(date.fromisoformat(raw))
        except ValueError as exc:
            raise ValueError(f"{field_name} 'on' value must be an ISO date") from exc
    if "at" in value:
        raw = value["at"]
        if not isinstance(raw, str):
            raise ValueError(f"{field_name} 'at' value must be an ISO datetime string")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} 'at' value must be an ISO datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field_name} 'at' value must include a timezone offset")
        return DateTimeObservation(parsed)
    raise ValueError(f"{field_name} evidence must contain exactly one 'on' or 'at' value")


@dataclass(frozen=True, slots=True)
class TextObservation:
    value: str
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("text observation value must be a non-empty string")
        _validate_excerpt(self.excerpt)


@dataclass(frozen=True, slots=True)
class UrlObservation:
    value: str
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("URL observation value must be a non-empty string")
        _validate_excerpt(self.excerpt)


@dataclass(frozen=True, slots=True)
class DateObservationField:
    value: DateObservation
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, (DateOnlyObservation, DateTimeObservation)):
            raise ValueError("date field observation must contain a date observation")
        _validate_excerpt(self.excerpt)


@dataclass(frozen=True, slots=True)
class FormatObservation:
    value: Literal["online", "offline", "hybrid"]
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or self.value not in {"online", "offline", "hybrid"}:
            raise ValueError("format observation must be online, offline, or hybrid")
        _validate_excerpt(self.excerpt)


class EventDateOrdering(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INDETERMINATE = "indeterminate"
    INCOMPLETE = "incomplete"


def event_date_ordering(
    start: DateObservationField | None,
    end: DateObservationField | None,
) -> EventDateOrdering:
    """Compare event endpoints without inventing a timezone for date-only values."""
    if start is None or end is None:
        return EventDateOrdering.INCOMPLETE
    if not isinstance(start, DateObservationField) or not isinstance(end, DateObservationField):
        raise ValueError("event endpoints must be date field observations")

    start_value = start.value
    end_value = end.value
    if isinstance(start_value, DateOnlyObservation) and isinstance(end_value, DateOnlyObservation):
        return (
            EventDateOrdering.INVALID if start_value.on > end_value.on else EventDateOrdering.VALID
        )
    if isinstance(start_value, DateTimeObservation) and isinstance(end_value, DateTimeObservation):
        start_utc = start_value.at.astimezone(UTC)
        end_utc = end_value.at.astimezone(UTC)
        return EventDateOrdering.INVALID if start_utc > end_utc else EventDateOrdering.VALID
    return EventDateOrdering.INDETERMINATE


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """Source observation for a competition; it is not verified competition data."""

    title: str
    source_url: str
    publisher_name: str
    source_kind: str = "aggregator"
    external_id: str | None = None
    description: TextObservation | None = None
    official_url: UrlObservation | None = None
    registration_url: UrlObservation | None = None
    registration_deadline: DateObservationField | None = None
    event_starts: DateObservationField | None = None
    event_ends: DateObservationField | None = None
    event_timezone: TextObservation | None = None
    format: FormatObservation | None = None
    eligibility_summary: TextObservation | None = None
    title_excerpt: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise ValueError("candidate title must be a string")
        if not isinstance(self.source_url, str):
            raise ValueError("candidate source URL must be a string")
        if not isinstance(self.publisher_name, str):
            raise ValueError("publisher name must be a string")
        if not isinstance(self.source_kind, str):
            raise ValueError("source kind must be a string")
        if self.external_id is not None and not isinstance(self.external_id, str):
            raise ValueError("external ID must be a string")
        if not self.title.strip():
            raise ValueError("candidate title must not be empty")
        _validate_excerpt(self.title_excerpt)
        if not self.publisher_name.strip():
            raise ValueError("publisher name must not be empty")
        if self.source_kind not in {"official", "organizer", "aggregator", "other"}:
            raise ValueError("unsupported discovery source kind")

        expected_types = {
            "description": TextObservation,
            "official_url": UrlObservation,
            "registration_url": UrlObservation,
            "registration_deadline": DateObservationField,
            "event_starts": DateObservationField,
            "event_ends": DateObservationField,
            "event_timezone": TextObservation,
            "format": FormatObservation,
            "eligibility_summary": TextObservation,
        }
        for field_name, expected_type in expected_types.items():
            observation = getattr(self, field_name)
            if observation is not None and not isinstance(observation, expected_type):
                raise ValueError(f"candidate {field_name} must be {expected_type.__name__} or None")

        if event_date_ordering(self.event_starts, self.event_ends) is EventDateOrdering.INVALID:
            raise ValueError("candidate event start must not be later than event end")
