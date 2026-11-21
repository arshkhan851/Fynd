from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.discovery.candidates import (
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    DiscoveryCandidate,
    EventDateOrdering,
    FormatObservation,
    TextObservation,
    UrlObservation,
    event_date_ordering,
)


def test_text_and_url_observations_accept_values_and_excerpts():
    assert TextObservation("A clear description", "Published overview").excerpt
    assert UrlObservation("https://events.example.test/event", "Official page")


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_text_and_url_observations_reject_empty_values(value):
    with pytest.raises(ValueError):
        TextObservation(value)
    with pytest.raises(ValueError):
        UrlObservation(value)


@pytest.mark.parametrize("excerpt", ["", " ", "\t\n"])
def test_observations_reject_empty_excerpts(excerpt):
    with pytest.raises(ValueError):
        TextObservation("value", excerpt)
    with pytest.raises(ValueError):
        UrlObservation("https://events.example.test", excerpt)
    with pytest.raises(ValueError):
        DateObservationField(DateOnlyObservation(date(2027, 5, 10)), excerpt)
    with pytest.raises(ValueError):
        FormatObservation("online", excerpt)


def test_date_observation_precision_and_timezone_validation():
    assert DateOnlyObservation(date(2027, 5, 10)).on == date(2027, 5, 10)
    with pytest.raises(ValueError, match="not a datetime"):
        DateOnlyObservation(datetime(2027, 5, 10))

    aware = datetime(2027, 5, 10, 9, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert DateTimeObservation(aware).at == aware
    with pytest.raises(ValueError, match="timezone-aware"):
        DateTimeObservation(datetime(2027, 5, 10, 9))


@pytest.mark.parametrize("value", ["in-person", "", [], None])
def test_format_observation_rejects_unsupported_formats(value):
    with pytest.raises(ValueError):
        FormatObservation(value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("description", "not an observation"),
        ("official_url", TextObservation("not a URL observation")),
        ("registration_deadline", DateOnlyObservation(date(2027, 5, 10))),
        ("format", TextObservation("online")),
    ],
)
def test_candidate_rejects_wrong_observation_types(field_name, value):
    with pytest.raises(ValueError, match=f"candidate {field_name}"):
        DiscoveryCandidate("Title", "https://example.test", "Publisher", **{field_name: value})


def test_candidate_preserves_original_constructors_and_defaults():
    keyword_candidate = DiscoveryCandidate("Title", "https://example.test", "Publisher")
    positional_candidate = DiscoveryCandidate(
        "Title", "https://example.test", "Publisher", "aggregator", "external-1"
    )

    assert keyword_candidate.description is None
    assert keyword_candidate.official_url is None
    assert keyword_candidate.registration_url is None
    assert keyword_candidate.registration_deadline is None
    assert keyword_candidate.event_starts is None
    assert keyword_candidate.event_ends is None
    assert keyword_candidate.event_timezone is None
    assert keyword_candidate.format is None
    assert keyword_candidate.eligibility_summary is None
    assert positional_candidate.external_id == "external-1"


@pytest.mark.parametrize(
    ("title", "publisher_name", "message"),
    [
        (" ", "Publisher", "candidate title must not be empty"),
        ("Title", "\t", "publisher name must not be empty"),
    ],
)
def test_candidate_preserves_title_and_publisher_validation(title, publisher_name, message):
    with pytest.raises(ValueError, match=message):
        DiscoveryCandidate(title, "https://example.test", publisher_name)


def test_candidate_accepts_all_supported_observation_fields():
    candidate = DiscoveryCandidate(
        title="Climate Hackathon",
        source_url="https://events.example.test/competition",
        publisher_name="Example Events",
        description=TextObservation("Build climate solutions"),
        official_url=UrlObservation("https://organizer.example.test/event"),
        registration_url=UrlObservation("https://register.example.test/event"),
        registration_deadline=DateObservationField(DateOnlyObservation(date(2027, 5, 10))),
        event_starts=DateObservationField(
            DateTimeObservation(
                datetime(2027, 5, 12, 9, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            )
        ),
        event_ends=DateObservationField(
            DateTimeObservation(
                datetime(2027, 5, 12, 17, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            )
        ),
        event_timezone=TextObservation("Asia/Kolkata"),
        format=FormatObservation("hybrid"),
        eligibility_summary=TextObservation("Open to university students"),
    )

    assert candidate.format == FormatObservation("hybrid")
    assert (
        event_date_ordering(candidate.event_starts, candidate.event_ends) is EventDateOrdering.VALID
    )


def test_event_ordering_for_date_only_endpoints():
    start = DateObservationField(DateOnlyObservation(date(2027, 5, 10)))
    end = DateObservationField(DateOnlyObservation(date(2027, 5, 11)))
    later_start = DateObservationField(DateOnlyObservation(date(2027, 5, 12)))

    assert event_date_ordering(start, end) is EventDateOrdering.VALID
    assert event_date_ordering(later_start, end) is EventDateOrdering.INVALID
    assert event_date_ordering(end, end) is EventDateOrdering.VALID
    with pytest.raises(ValueError, match="event start must not be later"):
        DiscoveryCandidate(
            "Title",
            "https://example.test",
            "Publisher",
            event_starts=later_start,
            event_ends=end,
        )


def test_event_ordering_compares_datetimes_as_utc_instants():
    # 10:00 UTC is earlier than 10:30 UTC, despite its later local clock reading.
    start = DateObservationField(
        DateTimeObservation(
            datetime(2027, 5, 10, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        )
    )
    end = DateObservationField(DateTimeObservation(datetime(2027, 5, 10, 10, 30, tzinfo=UTC)))
    later_start = DateObservationField(DateTimeObservation(datetime(2027, 5, 10, 11, tzinfo=UTC)))

    assert event_date_ordering(start, end) is EventDateOrdering.VALID
    assert event_date_ordering(later_start, end) is EventDateOrdering.INVALID
    assert event_date_ordering(end, end) is EventDateOrdering.VALID
    with pytest.raises(ValueError, match="event start must not be later"):
        DiscoveryCandidate(
            "Title",
            "https://example.test",
            "Publisher",
            event_starts=later_start,
            event_ends=end,
        )


def test_event_ordering_marks_mixed_precision_indeterminate_and_missing_incomplete():
    date_value = DateObservationField(DateOnlyObservation(date(2027, 5, 10)))
    datetime_value = DateObservationField(DateTimeObservation(datetime(2027, 5, 10, 9, tzinfo=UTC)))

    assert event_date_ordering(date_value, datetime_value) is EventDateOrdering.INDETERMINATE
    assert event_date_ordering(datetime_value, date_value) is EventDateOrdering.INDETERMINATE
    assert event_date_ordering(date_value, None) is EventDateOrdering.INCOMPLETE
    assert event_date_ordering(None, datetime_value) is EventDateOrdering.INCOMPLETE
    assert DiscoveryCandidate(
        "Title",
        "https://example.test",
        "Publisher",
        event_starts=date_value,
        event_ends=datetime_value,
    )
