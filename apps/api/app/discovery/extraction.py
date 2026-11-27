"""Strict, source-grounded validation for structured discovery extraction results."""

import json
from typing import TypeAlias
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.discovery.candidates import (
    DateObservationField,
    EventDateOrdering,
    FormatObservation,
    TextObservation,
    UrlObservation,
    event_date_ordering,
    parse_date_observation,
)

ExtractionObservation: TypeAlias = (
    TextObservation | UrlObservation | DateObservationField | FormatObservation
)

_FIELD_NAMES = frozenset(
    {
        "title",
        "description",
        "eligibility_summary",
        "format",
        "event_timezone",
        "official_url",
        "registration_url",
        "registration_deadline",
        "event_starts",
        "event_ends",
    }
)
_TEXT_FIELDS = frozenset({"title", "description", "eligibility_summary"})
_URL_FIELDS = frozenset({"official_url", "registration_url"})
_DATE_FIELDS = frozenset(
    {"registration_deadline", "event_starts", "event_ends"}
)


class ExtractionValidationError(ValueError):
    """An extraction result is malformed, invalid, or unsupported by its source text."""


def _reject_constant(value: str) -> None:
    raise ExtractionValidationError(f"non-standard JSON constant '{value}' is not allowed")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionValidationError(f"duplicate JSON key '{key}' is not allowed")
        result[key] = value
    return result


def _parse_result(result_json: str | bytes) -> object:
    if isinstance(result_json, bytes):
        try:
            result_json = result_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionValidationError("extraction result must be UTF-8 JSON") from exc
    if not isinstance(result_json, str) or not result_json.strip():
        raise ExtractionValidationError("extraction result must be non-empty JSON text")
    try:
        return json.loads(
            result_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ExtractionValidationError("extraction result is not valid JSON") from exc


def _validate_url(value: object, field_name: str, evidence: str) -> UrlObservation:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionValidationError(f"{field_name} must be a non-empty URL string")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ExtractionValidationError(
            f"{field_name} URL contains whitespace or control characters"
        )
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise ExtractionValidationError(f"{field_name} is not a valid URL") from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ExtractionValidationError(
            f"{field_name} must be an absolute HTTP(S) URL without credentials"
        )
    return UrlObservation(value, evidence)


def _validate_date(value: object, field_name: str, evidence: str) -> DateObservationField:
    try:
        observation = parse_date_observation(value, field_name)
    except ValueError as exc:
        raise ExtractionValidationError(str(exc)) from exc
    return DateObservationField(observation, evidence)


def _validate_value(field_name: str, value: object, evidence: str) -> ExtractionObservation:
    if field_name in _TEXT_FIELDS:
        if not isinstance(value, str):
            raise ExtractionValidationError(f"{field_name} must be a string")
        return TextObservation(value, evidence)
    if field_name in _URL_FIELDS:
        return _validate_url(value, field_name, evidence)
    if field_name in _DATE_FIELDS:
        return _validate_date(value, field_name, evidence)
    if field_name == "format":
        if not isinstance(value, str):
            raise ExtractionValidationError("format must be a string")
        try:
            return FormatObservation(value, evidence)
        except ValueError as exc:
            raise ExtractionValidationError(str(exc)) from exc
    if field_name == "event_timezone":
        if not isinstance(value, str) or not value.strip():
            raise ExtractionValidationError("event_timezone must be a non-empty IANA timezone")
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ExtractionValidationError(
                "event_timezone must be a recognized IANA timezone"
            ) from exc
        return TextObservation(value, evidence)
    raise ExtractionValidationError(f"unsupported extraction field '{field_name}'")


def validate_extraction_result(
    result_json: str | bytes,
    source_text: str,
) -> dict[str, ExtractionObservation]:
    """Validate strict JSON and exact quote grounding, returning candidate observations.

    Grounding uses case-sensitive exact substring matching. No whitespace, case, or Unicode
    normalization is applied. The result intentionally contains no source identity, reviewer,
    confidence, authorization, or persistence metadata.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        raise ExtractionValidationError("source text must be a non-empty string")

    payload = _parse_result(result_json)
    if not isinstance(payload, dict) or set(payload) != {"fields"}:
        raise ExtractionValidationError("extraction result must contain only a 'fields' array")
    fields = payload["fields"]
    if not isinstance(fields, list) or not fields:
        raise ExtractionValidationError("extraction result fields must be a non-empty array")

    observations: dict[str, ExtractionObservation] = {}
    for index, item in enumerate(fields):
        if not isinstance(item, dict) or set(item) != {"field", "value", "evidence"}:
            raise ExtractionValidationError(
                f"field entry {index} must contain only field, value, and evidence"
            )
        field_name = item["field"]
        if not isinstance(field_name, str) or field_name not in _FIELD_NAMES:
            raise ExtractionValidationError(f"field entry {index} names an unsupported field")
        if field_name in observations:
            raise ExtractionValidationError(f"field '{field_name}' appears more than once")

        evidence = item["evidence"]
        if not isinstance(evidence, str) or not evidence.strip():
            raise ExtractionValidationError(
                f"field '{field_name}' requires a non-empty evidence quote"
            )
        if evidence not in source_text:
            raise ExtractionValidationError(
                f"field '{field_name}' evidence quote does not occur in source text"
            )

        observation = _validate_value(field_name, item["value"], evidence)
        observations[field_name] = observation

    if "title" not in observations:
        raise ExtractionValidationError("extraction result must include a grounded title")

    start = observations.get("event_starts")
    end = observations.get("event_ends")
    if isinstance(start, DateObservationField) and isinstance(end, DateObservationField):
        if event_date_ordering(start, end) is EventDateOrdering.INVALID:
            raise ExtractionValidationError("event start must not be later than event end")

    return observations
