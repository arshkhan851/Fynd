import json
from datetime import date, datetime

import pytest

from app.discovery.candidates import (
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.extraction import (
    ExtractionValidationError,
    validate_extraction_result,
)
from app.review.service import ReviewError, _validate_date_evidence


def _entry(field, value, evidence):
    return {"field": field, "value": value, "evidence": evidence}


def _result(*fields):
    return json.dumps({"fields": list(fields)})


def test_valid_text_and_url_extractions_retain_exact_evidence_quotes():
    source_text = "The Fynd Demo is open to students. Register at https://fynd.example/register"
    result = _result(
        _entry("title", "Fynd Demo", "The Fynd Demo"),
        _entry("eligibility_summary", "open to students", "open to students"),
        _entry("registration_url", "https://fynd.example/register", "https://fynd.example/register"),
    )

    observations = validate_extraction_result(result, source_text)

    assert observations == {
        "title": TextObservation("Fynd Demo", "The Fynd Demo"),
        "eligibility_summary": TextObservation("open to students", "open to students"),
        "registration_url": UrlObservation(
            "https://fynd.example/register", "https://fynd.example/register"
        ),
    }


def test_valid_date_only_and_timezone_aware_datetime_observations():
    source_text = "Opens on 2027-05-10. The event starts 2027-05-11T09:30:00+05:30."
    observations = validate_extraction_result(
        _result(
            _entry("title", "Fynd Event", "The event"),
            _entry("registration_deadline", {"on": "2027-05-10"}, "2027-05-10"),
            _entry(
                "event_starts",
                {"at": "2027-05-11T09:30:00+05:30"},
                "2027-05-11T09:30:00+05:30",
            ),
        ),
        source_text,
    )

    assert observations["registration_deadline"] == DateObservationField(
        DateOnlyObservation(date(2027, 5, 10)), "2027-05-10"
    )
    assert observations["event_starts"] == DateObservationField(
        DateTimeObservation(datetime.fromisoformat("2027-05-11T09:30:00+05:30")),
        "2027-05-11T09:30:00+05:30",
    )


@pytest.mark.parametrize(
    "field,value,evidence,expected_type",
    [
        ("description", "A useful description", "useful description", TextObservation),
        (
            "official_url",
            "http://events.example/official",
            "events.example/official",
            UrlObservation,
        ),
        ("format", "hybrid", "hybrid event", FormatObservation),
        ("event_timezone", "Asia/Kolkata", "All times Asia/Kolkata", TextObservation),
        (
            "registration_deadline",
            {"on": "2027-06-01"},
            "Deadline: 2027-06-01",
            DateObservationField,
        ),
        (
            "event_ends",
            {"at": "2027-06-02T18:00:00Z"},
            "Ends 2027-06-02T18:00:00Z",
            DateObservationField,
        ),
    ],
)
def test_supported_fields_convert_to_existing_observation_types(
    field, value, evidence, expected_type
):
    observations = validate_extraction_result(
        _result(_entry("title", "Fynd event", "Fynd event"), _entry(field, value, evidence)),
        f"Fynd event; {evidence}",
    )

    assert isinstance(observations[field], expected_type)
    assert observations[field].excerpt == evidence


@pytest.mark.parametrize(
    "result_json",
    ["{not json", '{"fields": [], "fields": []}', '{"fields": [NaN]}'],
)
def test_invalid_or_non_standard_json_is_rejected(result_json):
    with pytest.raises(ExtractionValidationError):
        validate_extraction_result(result_json, "source text")


@pytest.mark.parametrize(
    "result_json",
    [
        _result(_entry("invented_field", "value", "value")),
        json.dumps({"fields": [_entry("title", "Title", "Title")], "source_id": "trusted"}),
        json.dumps(
            {
                "fields": [_entry("title", "Title", "Title")],
                "reviewer_user_id": "not model-controlled",
            }
        ),
        json.dumps(
            {
                "fields": [
                    _entry("title", "Title", "Title"),
                    _entry("title", "Title", "Title"),
                ]
            }
        ),
        json.dumps({"fields": [_entry("title", None, "Title")]}),
        json.dumps(
            {"fields": [{"field": "title", "value": "Title", "evidence": "Title", "extra": 1}]}
        ),
    ],
)
def test_unknown_duplicate_null_and_extra_contract_data_are_rejected(result_json):
    with pytest.raises(ExtractionValidationError):
        validate_extraction_result(result_json, "Title")


@pytest.mark.parametrize(
    "field,value,evidence",
    [
        ("title", 42, "42"),
        ("description", ["not", "text"], "not text"),
        ("format", "virtual", "virtual"),
        ("event_timezone", "Not/AZone", "Not/AZone"),
        ("registration_deadline", {"on": "2027-02-30"}, "2027-02-30"),
        ("event_starts", {"at": "2027-05-10T09:00:00"}, "2027-05-10T09:00:00"),
        ("official_url", "javascript:alert(1)", "javascript:alert(1)"),
        ("registration_url", "https://user:pass@example.org", "https://user:pass@example.org"),
    ],
)
def test_invalid_field_values_are_rejected(field, value, evidence):
    with pytest.raises(ExtractionValidationError):
        validate_extraction_result(
            _result(_entry("title", "Title", "Title"), _entry(field, value, evidence)),
            f"Title {evidence}",
        )


def test_missing_title_empty_results_and_missing_evidence_are_rejected():
    for result_json in (
        '{"fields": []}',
        _result(_entry("description", "Description", "Description")),
        _result(_entry("title", "Title", "")),
        json.dumps(
            {
                "fields": [
                    _entry("title", "Title", "Title"),
                    {"field": "description", "value": "Description"},
                ]
            }
        ),
    ):
        with pytest.raises(ExtractionValidationError):
            validate_extraction_result(result_json, "Title Description")


def test_fabricated_quote_and_non_exact_whitespace_are_rejected():
    for quote in ("A fabricated claim", "  Fynd Demo"):
        with pytest.raises(ExtractionValidationError, match="does not occur"):
            validate_extraction_result(
                _result(_entry("title", "Fynd Demo", quote)),
                "Fynd Demo is open",
            )


def test_event_end_before_start_is_rejected_but_mixed_precision_is_supported():
    with pytest.raises(ExtractionValidationError, match="must not be later"):
        validate_extraction_result(
            _result(
                _entry("title", "Fynd event", "Fynd event"),
                _entry("event_starts", {"on": "2027-06-02"}, "2027-06-02"),
                _entry("event_ends", {"on": "2027-06-01"}, "2027-06-01"),
            ),
            "Fynd event runs 2027-06-02 to 2027-06-01",
        )

    observations = validate_extraction_result(
        _result(
            _entry("title", "Fynd event", "Fynd event"),
            _entry("event_starts", {"on": "2027-06-01"}, "2027-06-01"),
            _entry("event_ends", {"at": "2027-06-02T18:00:00Z"}, "2027-06-02T18:00:00Z"),
        ),
        "Fynd event 2027-06-01 through 2027-06-02T18:00:00Z",
    )
    assert isinstance(observations["event_starts"].value, DateOnlyObservation)
    assert isinstance(observations["event_ends"].value, DateTimeObservation)


def test_grounding_is_pure_and_does_not_accept_model_supplied_identity():
    observations = validate_extraction_result(
        _result(_entry("title", "Fynd event", "Fynd event")),
        "Fynd event",
    )

    assert set(observations) == {"title"}
    assert observations["title"] == TextObservation("Fynd event", "Fynd event")
    with pytest.raises(ExtractionValidationError, match="only a 'fields' array"):
        validate_extraction_result(
            json.dumps(
                {
                    "fields": [_entry("title", "Fynd event", "Fynd event")],
                    "registry_source_id": "trusted-source",
                }
            ),
            "Fynd event",
        )


def test_empty_source_text_is_rejected():
    with pytest.raises(ExtractionValidationError, match="source text"):
        validate_extraction_result(_result(_entry("title", "Title", "Title")), " \n")


@pytest.mark.parametrize(
    "value,precision",
    [
        ({"on": "2027-05-10"}, "on"),
        ({"at": "2027-05-10T09:30:00Z"}, "at"),
    ],
)
def test_review_date_validation_uses_the_shared_observation_parser(value, precision):
    parsed, actual_precision = _validate_date_evidence(value, "event_starts")
    extracted = validate_extraction_result(
        _result(
            _entry("title", "Fynd event", "Fynd event"),
            _entry("event_starts", value, value[precision]),
        ),
        f"Fynd event {value[precision]}",
    )["event_starts"].value

    assert actual_precision == precision
    assert parsed == (extracted.on if precision == "on" else extracted.at)


def test_review_date_validation_preserves_review_error_contract():
    with pytest.raises(ReviewError, match="include a timezone offset"):
        _validate_date_evidence({"at": "2027-05-10T09:30:00"}, "event_starts")
