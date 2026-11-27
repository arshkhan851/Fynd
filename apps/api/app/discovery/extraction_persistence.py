"""Persistence boundary for source-grounded extraction observations."""

from sqlalchemy.orm import Session

from app.discovery.candidates import (
    DateObservationField,
    DiscoveryCandidate,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.extraction import ExtractionObservation
from app.discovery.orchestration import AuthorizedExtractionResult
from app.discovery.persistence import DiscoverySummary, persist_candidates

_CANDIDATE_FIELDS = frozenset(
    {
        "description",
        "official_url",
        "registration_url",
        "registration_deadline",
        "event_starts",
        "event_ends",
        "event_timezone",
        "format",
        "eligibility_summary",
    }
)
_OBSERVATION_TYPES = (
    TextObservation,
    UrlObservation,
    DateObservationField,
    FormatObservation,
)


def _candidate_from_extraction(result: AuthorizedExtractionResult) -> DiscoveryCandidate:
    if not isinstance(result, AuthorizedExtractionResult):
        raise TypeError("extraction persistence requires an AuthorizedExtractionResult")
    if (
        not isinstance(result.registry_source_id, str)
        or not result.registry_source_id.strip()
        or not isinstance(result.source_url, str)
        or not isinstance(result.publisher_name, str)
        or not isinstance(result.source_kind, str)
    ):
        raise ValueError("extraction source context is malformed")
    if (
        not isinstance(result.allowed_hosts, tuple)
        or not result.allowed_hosts
        or any(not isinstance(host, str) or not host.strip() for host in result.allowed_hosts)
    ):
        raise ValueError("extraction source host context is malformed")
    observations = result.observations
    if not isinstance(observations, dict):
        raise TypeError("extraction observations must be a mapping")
    if any(not isinstance(field_name, str) for field_name in observations):
        raise ValueError("extraction observation names must be strings")
    unknown = observations.keys() - _CANDIDATE_FIELDS - {"title"}
    if unknown:
        raise ValueError(f"unsupported extraction observations: {', '.join(sorted(unknown))}")

    title = observations.get("title")
    if not isinstance(title, TextObservation) or not title.excerpt:
        raise ValueError("extracted title must include its supporting evidence excerpt")

    candidate_fields: dict[str, ExtractionObservation] = {}
    for field_name, observation in observations.items():
        if not isinstance(observation, _OBSERVATION_TYPES):
            raise TypeError(f"extraction observation for {field_name} has an invalid type")
        if not observation.excerpt:
            raise ValueError(f"extraction observation for {field_name} has no evidence excerpt")
        if field_name != "title":
            candidate_fields[field_name] = observation

    return DiscoveryCandidate(
        title=title.value,
        title_excerpt=title.excerpt,
        source_url=result.source_url,
        publisher_name=result.publisher_name,
        source_kind=result.source_kind,
        **candidate_fields,
    )


def persist_extraction_candidate(
    session: Session,
    result: AuthorizedExtractionResult,
) -> DiscoverySummary:
    """Persist validated extraction as unpublished candidate evidence.

    ``persist_candidates`` independently reauthorizes store and internal_review at this
    boundary, then retains its URL deduplication, locking, savepoint and transaction rules.
    """
    candidate = _candidate_from_extraction(result)
    return persist_candidates(
        session,
        [candidate],
        set(result.allowed_hosts),
        registry_source_id=result.registry_source_id,
    )
