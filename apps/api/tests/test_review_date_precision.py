from contextlib import nullcontext
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.review.service import ReviewError, _apply_accepted_evidence


class _NoSelectedEvidence:
    def all(self):
        return []


class _ReviewSession:
    def __init__(self, evidence, competition):
        self._scalar_results = iter((evidence, competition))
        self.flush_count = 0

    def in_transaction(self):
        return True

    def begin_nested(self):
        return nullcontext()

    def scalar(self, _statement):
        return next(self._scalar_results)

    def scalars(self, _statement):
        return _NoSelectedEvidence()

    def flush(self):
        self.flush_count += 1

    def add(self, _item):
        pass


def _competition(*, starts_on=None, starts_at=None, ends_on=None, ends_at=None):
    return SimpleNamespace(
        event_starts_on=starts_on,
        event_starts_at=starts_at,
        event_ends_on=ends_on,
        event_ends_at=ends_at,
    )


def _evidence(field_name, value):
    return SimpleNamespace(
        id=1,
        competition_id=10,
        field_name=field_name,
        field_key="",
        observed_value={field_name: value},
        review_state="candidate",
        reviewed_at=None,
    )


def _accept(evidence, competition):
    session = _ReviewSession(evidence, competition)
    reviewer = SimpleNamespace(id="reviewer-id")
    _apply_accepted_evidence(session, evidence, competition, reviewer)
    result = evidence
    return result, session


@pytest.mark.parametrize(
    ("field_name", "value", "expected_column", "expected_value"),
    [
        (
            "event_starts",
            {"on": "2027-05-10"},
            "event_starts_on",
            date(2027, 5, 10),
        ),
        (
            "event_starts",
            {"at": "2027-05-10T09:00:00+05:30"},
            "event_starts_at",
            datetime.fromisoformat("2027-05-10T09:00:00+05:30"),
        ),
    ],
)
def test_accepts_either_start_precision_when_end_is_absent(
    field_name, value, expected_column, expected_value
):
    competition = _competition()
    evidence = _evidence(field_name, value)

    accepted, session = _accept(evidence, competition)

    assert accepted.review_state == "selected"
    assert getattr(competition, expected_column) == expected_value
    assert competition.event_ends_on is None
    assert competition.event_ends_at is None
    assert session.flush_count == 1


@pytest.mark.parametrize(
    ("starts_on", "starts_at", "ends_on", "ends_at", "field_name", "value"),
    [
        (date(2027, 5, 10), None, None, None, "event_ends", {"at": "2027-05-12T09:00:00Z"}),
        (
            None,
            datetime.fromisoformat("2027-05-10T09:00:00+00:00"),
            None,
            None,
            "event_ends",
            {"on": "2027-05-12"},
        ),
        (
            None,
            None,
            None,
            datetime.fromisoformat("2027-05-12T09:00:00+00:00"),
            "event_starts",
            {"on": "2027-05-10"},
        ),
        (None, None, date(2027, 5, 12), None, "event_starts", {"at": "2027-05-10T09:00:00Z"}),
    ],
)
def test_rejects_mixed_precision_in_either_acceptance_order(
    starts_on, starts_at, ends_on, ends_at, field_name, value
):
    competition = _competition(
        starts_on=starts_on,
        starts_at=starts_at,
        ends_on=ends_on,
        ends_at=ends_at,
    )
    evidence = _evidence(field_name, value)
    canonical_before = (
        competition.event_starts_on,
        competition.event_starts_at,
        competition.event_ends_on,
        competition.event_ends_at,
    )
    session = _ReviewSession(evidence, competition)

    with pytest.raises(ReviewError, match="mixed-precision event range requires human resolution"):
        _apply_accepted_evidence(
            session, evidence, competition, SimpleNamespace(id="reviewer-id")
        )

    assert (
        competition.event_starts_on,
        competition.event_starts_at,
        competition.event_ends_on,
        competition.event_ends_at,
    ) == canonical_before
    assert evidence.review_state == "candidate"
    assert evidence.reviewed_at is None
    assert session.flush_count == 0


@pytest.mark.parametrize(
    ("field_name", "value", "competition_values", "expected_precision"),
    [
        (
            "event_ends",
            {"on": "2027-05-12"},
            {"starts_on": date(2027, 5, 10)},
            "on",
        ),
        (
            "event_starts",
            {"at": "2027-05-10T09:00:00Z"},
            {"ends_at": datetime.fromisoformat("2027-05-12T09:00:00+00:00")},
            "at",
        ),
    ],
)
def test_accepts_valid_same_precision_event_pairs(
    field_name, value, competition_values, expected_precision
):
    competition = _competition(**competition_values)
    evidence = _evidence(field_name, value)

    accepted, _ = _accept(evidence, competition)

    assert accepted.review_state == "selected"
    if expected_precision == "on":
        assert competition.event_starts_on == date(2027, 5, 10)
        assert competition.event_ends_on == date(2027, 5, 12)
        assert competition.event_starts_at is None
        assert competition.event_ends_at is None
    else:
        assert competition.event_starts_at == datetime.fromisoformat("2027-05-10T09:00:00+00:00")
        assert competition.event_ends_at == datetime.fromisoformat("2027-05-12T09:00:00+00:00")
        assert competition.event_starts_on is None
        assert competition.event_ends_on is None
