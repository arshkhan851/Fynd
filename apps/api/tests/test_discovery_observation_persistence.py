from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.discovery import persistence
from app.discovery.candidates import (
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    DiscoveryCandidate,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.persistence import _candidate_evidence_specs, persist_candidates
from app.discovery.registry import RegistryError

SOURCE_ID = "synthetic-observation-source"
PUBLISHER = "Synthetic Publisher"
ALL_FIELDS = {
    "title",
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


def _candidate(**changes):
    values = {
        "title": "Synthetic title",
        "source_url": "https://events.example.test/competition",
        "publisher_name": PUBLISHER,
        "source_kind": "aggregator",
    }
    values.update(changes)
    return DiscoveryCandidate(**values)


def test_all_nine_structured_observations_serialize_with_field_key_payloads_and_excerpts():
    observations = {
        "description": TextObservation("Description value", "Description excerpt"),
        "official_url": UrlObservation("https://example.org/official", "Official excerpt"),
        "registration_url": UrlObservation("https://example.org/register", "Registration excerpt"),
        "registration_deadline": DateObservationField(
            DateOnlyObservation(date(2027, 5, 10)), "Deadline excerpt"
        ),
        "event_starts": DateObservationField(
            DateTimeObservation(datetime(2027, 5, 11, 9, 30, tzinfo=UTC)), "Start excerpt"
        ),
        "event_ends": DateObservationField(DateOnlyObservation(date(2027, 5, 12)), "End excerpt"),
        "event_timezone": TextObservation("Asia/Kolkata", "Timezone excerpt"),
        "format": FormatObservation("online", "Format excerpt"),
        "eligibility_summary": TextObservation("Open to students", "Eligibility excerpt"),
    }
    candidate = _candidate(**observations)

    specs, skipped = _candidate_evidence_specs(candidate, ALL_FIELDS)
    by_name = {name: (value, excerpt) for name, value, excerpt in specs}

    assert set(by_name) == ALL_FIELDS
    assert by_name["title"] == ({"title": "Synthetic title"}, "Synthetic title")
    assert by_name["description"] == (
        {"description": "Description value"},
        "Description excerpt",
    )
    assert by_name["official_url"] == (
        {"official_url": "https://example.org/official"},
        "Official excerpt",
    )
    assert by_name["registration_url"] == (
        {"registration_url": "https://example.org/register"},
        "Registration excerpt",
    )
    assert by_name["registration_deadline"] == (
        {"registration_deadline": {"on": "2027-05-10"}},
        "Deadline excerpt",
    )
    assert by_name["event_starts"] == (
        {"event_starts": {"at": "2027-05-11T09:30:00+00:00"}},
        "Start excerpt",
    )
    assert by_name["event_ends"] == (
        {"event_ends": {"on": "2027-05-12"}},
        "End excerpt",
    )
    assert by_name["event_timezone"] == (
        {"event_timezone": "Asia/Kolkata"},
        "Timezone excerpt",
    )
    assert by_name["format"] == ({"format": "online"}, "Format excerpt")
    assert by_name["eligibility_summary"] == (
        {"eligibility_summary": "Open to students"},
        "Eligibility excerpt",
    )
    assert skipped == ()


def test_missing_observations_create_no_optional_evidence_specs():
    specs, skipped = _candidate_evidence_specs(_candidate(), ALL_FIELDS)

    assert specs == [("title", {"title": "Synthetic title"}, "Synthetic title")]
    assert skipped == ()


def test_disallowed_observations_are_skipped_and_named():
    candidate = _candidate(
        description=TextObservation("Description"),
        official_url=UrlObservation("https://example.org/official"),
    )

    specs, skipped = _candidate_evidence_specs(candidate, {"title", "description"})

    assert [name for name, _, _ in specs] == ["title", "description"]
    assert skipped == ("official_url",)


def test_date_precision_is_preserved_for_mixed_event_endpoints():
    candidate = _candidate(
        event_starts=DateObservationField(DateOnlyObservation(date(2027, 5, 10))),
        event_ends=DateObservationField(DateTimeObservation(datetime(2027, 5, 11, 12, tzinfo=UTC))),
    )

    specs, _ = _candidate_evidence_specs(candidate, ALL_FIELDS)
    values = {name: observed for name, observed, _ in specs}

    assert values["event_starts"] == {"event_starts": {"on": "2027-05-10"}}
    assert values["event_ends"] == {"event_ends": {"at": "2027-05-11T12:00:00+00:00"}}


def test_title_scope_is_required_before_database_access(monkeypatch):
    source = SimpleNamespace(
        id=SOURCE_ID,
        publisher_name=PUBLISHER,
        source_kind="aggregator",
        access={"allowed_hosts": ["example.test"]},
        use={"fields": ["description"]},
    )
    monkeypatch.setattr(persistence, "authorize_source_operation", lambda *_args: source)

    class NoDatabaseSession:
        def get_bind(self):
            raise AssertionError("database accessed before title-scope rejection")

    with pytest.raises(RegistryError, match="does not permit required title evidence"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate()],
            {"example.test"},
            registry_source_id=SOURCE_ID,
        )
