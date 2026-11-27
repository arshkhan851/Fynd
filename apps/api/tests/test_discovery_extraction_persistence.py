from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.discovery.candidates import (
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.extraction_persistence import (
    _candidate_from_extraction,
    persist_extraction_candidate,
)
from app.discovery.orchestration import AuthorizedExtractionResult
from app.discovery.persistence import DiscoverySummary, _candidate_evidence_specs
from app.discovery.registry import RegistryError

SOURCE_ID = "synthetic-test-source:Extracted Events"
SOURCE_URL = "https://events.example.test/extracted"


def _result(observations=None):
    return AuthorizedExtractionResult(
        registry_source_id=SOURCE_ID,
        source_url=SOURCE_URL,
        publisher_name="Extracted Events",
        source_kind="aggregator",
        allowed_hosts=("example.test",),
        observations=observations
        if observations is not None
        else {"title": TextObservation("Extracted Demo", "The Extracted Demo")},
    )


def test_extraction_is_converted_to_candidate_with_all_evidence_quotes_preserved():
    result = _result(
        {
            "title": TextObservation("Extracted Demo", "The Extracted Demo"),
            "description": TextObservation("A demo", "A two-day demo"),
            "official_url": UrlObservation(
                "https://events.example.test/home", "Visit the official page"
            ),
            "registration_url": UrlObservation(
                "https://events.example.test/register", "Registration is open here"
            ),
            "registration_deadline": DateObservationField(
                DateOnlyObservation(date(2027, 6, 1)), "Register by June 1, 2027"
            ),
            "event_starts": DateObservationField(
                DateTimeObservation(datetime(2027, 6, 2, 9, tzinfo=UTC)),
                "Starts June 2 at 09:00 UTC",
            ),
            "event_ends": DateObservationField(
                DateOnlyObservation(date(2027, 6, 3)), "Ends June 3, 2027"
            ),
            "event_timezone": TextObservation("UTC", "All times are in UTC"),
            "format": FormatObservation("online", "The event is online"),
            "eligibility_summary": TextObservation(
                "Open to students", "Students from any college may apply"
            ),
        }
    )

    candidate = _candidate_from_extraction(result)

    assert candidate.title == "Extracted Demo"
    assert candidate.title_excerpt == "The Extracted Demo"
    title_specs, _ = _candidate_evidence_specs(candidate, {"title"})
    assert title_specs == [
        ("title", {"title": "Extracted Demo"}, "The Extracted Demo")
    ]
    assert candidate.source_url == SOURCE_URL
    assert candidate.publisher_name == "Extracted Events"
    assert candidate.source_kind == "aggregator"
    assert candidate.description.excerpt == "A two-day demo"
    assert candidate.official_url.excerpt == "Visit the official page"
    assert candidate.registration_url.excerpt == "Registration is open here"
    assert candidate.registration_deadline.excerpt == "Register by June 1, 2027"
    assert candidate.event_starts.excerpt == "Starts June 2 at 09:00 UTC"
    assert candidate.event_ends.excerpt == "Ends June 3, 2027"
    assert candidate.event_timezone.excerpt == "All times are in UTC"
    assert candidate.format.excerpt == "The event is online"
    assert candidate.eligibility_summary.excerpt == "Students from any college may apply"


@pytest.mark.parametrize(
    "observations, message",
    [
        ({}, "supporting evidence excerpt"),
        ({"title": TextObservation("No quote")}, "supporting evidence excerpt"),
        (
            {
                "title": TextObservation("Valid title", "Title quote"),
                "description": TextObservation("Missing quote"),
            },
            "has no evidence excerpt",
        ),
        (
            {
                "title": TextObservation("Valid title", "Title quote"),
                "unsupported": TextObservation("Value", "Quote"),
            },
            "unsupported extraction observations",
        ),
        (
            {"title": "not an observation"},
            "supporting evidence excerpt",
        ),
    ],
)
def test_malformed_extraction_observations_are_rejected_before_persistence(
    observations, message
):
    with pytest.raises((TypeError, ValueError), match=message):
        _candidate_from_extraction(_result(observations))


def test_persistence_rechecks_authorization_and_propagates_denial(monkeypatch):
    calls = []

    def deny(session, candidates, allowed_hosts, *, registry_source_id):
        calls.append((session, list(candidates), allowed_hosts, registry_source_id))
        raise RegistryError("store permission denied")

    monkeypatch.setattr(
        "app.discovery.extraction_persistence.persist_candidates", deny
    )
    session = object()

    with pytest.raises(RegistryError, match="store permission denied"):
        persist_extraction_candidate(session, _result())

    assert len(calls) == 1
    assert calls[0][0] is session
    assert calls[0][1][0].title_excerpt == "The Extracted Demo"
    assert calls[0][2:] == ({"example.test"}, SOURCE_ID)


def test_successful_integration_uses_existing_persistence_function(monkeypatch):
    captured = {}

    def persist(session, candidates, allowed_hosts, *, registry_source_id):
        captured.update(
            session=session,
            candidates=list(candidates),
            allowed_hosts=allowed_hosts,
            registry_source_id=registry_source_id,
        )
        return DiscoverySummary(discovered=1)

    monkeypatch.setattr(
        "app.discovery.extraction_persistence.persist_candidates", persist
    )
    session = SimpleNamespace()

    summary = persist_extraction_candidate(session, _result())

    assert summary == DiscoverySummary(discovered=1)
    assert captured["session"] is session
    assert captured["registry_source_id"] == SOURCE_ID
    assert captured["allowed_hosts"] == {"example.test"}
    assert captured["candidates"][0].title_excerpt == "The Extracted Demo"
