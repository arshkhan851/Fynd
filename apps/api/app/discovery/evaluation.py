"""Offline fixture benchmark for extraction quality and validator outcomes.

Field precision/recall measure field presence among validator-accepted outputs; rejected
outputs contribute no accepted predictions and their expected fields count as missed for
recall. Exact-match accuracy is correct expected field values divided by all expected
field/value pairs, including fields from rejected cases. Evidence grounding is exact,
case-sensitive substring matches divided by evidence members in JSON-parseable outputs;
malformed JSON contributes no quote denominator. The acceptance/rejection rate must be read
alongside field precision because rejected outputs cannot add false-positive predictions.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from app.discovery.candidates import (
    DateObservationField,
    DateOnlyObservation,
    DateTimeObservation,
    FormatObservation,
    TextObservation,
    UrlObservation,
)
from app.discovery.extraction import ExtractionValidationError, validate_extraction_result

_DATASET_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / (
    "extraction_evaluation_v1.json"
)
_DATE_FIELDS = frozenset({"registration_deadline", "event_starts", "event_ends"})
_URL_FIELDS = frozenset({"official_url", "registration_url"})

EvaluationStatus = Literal["accepted", "rejected"]


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    fixture_id: str
    source_text: str
    expected_fields: dict[str, object]
    model_output: str
    expected_status: EvaluationStatus
    expected_rejection_category: str | None


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    fixture_id: str
    status: EvaluationStatus
    rejection_category: str | None
    expected_outcome_matches: bool


@dataclass(frozen=True, slots=True)
class Rate:
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    dataset_version: int
    case_outcomes: tuple[CaseOutcome, ...]
    accepted_count: int
    rejected_count: int
    field_precision: Rate
    field_recall: Rate
    exact_match_accuracy: Rate
    evidence_grounding_rate: Rate
    rejection_categories: tuple[tuple[str, int], ...]
    invalid_dates_by_field: tuple[tuple[str, int], ...]
    invalid_urls_by_field: tuple[tuple[str, int], ...]

    @property
    def expected_outcomes_match(self) -> bool:
        return all(outcome.expected_outcome_matches for outcome in self.case_outcomes)


def load_evaluation_cases(path: Path = _DATASET_PATH) -> tuple[int, tuple[EvaluationCase, ...]]:
    """Load the versioned JSON corpus without loading model, network, or persistence clients."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot load extraction evaluation dataset") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported extraction evaluation dataset version")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("evaluation dataset must contain a non-empty cases array")

    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError("evaluation cases must be objects")
        fixture_id = item.get("fixture_id")
        source_text = item.get("source_text")
        expected_fields = item.get("expected_fields")
        model_output = item.get("model_output")
        expected = item.get("expected_outcome")
        if (
            not isinstance(fixture_id, str)
            or not fixture_id
            or fixture_id in seen_ids
            or not isinstance(source_text, str)
            or not isinstance(expected_fields, dict)
            or any(not isinstance(field, str) for field in expected_fields)
            or not isinstance(model_output, str)
            or not isinstance(expected, dict)
        ):
            raise ValueError("evaluation case has invalid or duplicate required fields")
        status = expected.get("status")
        category = expected.get("rejection_category")
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"evaluation case {fixture_id} has an invalid expected status")
        if (status == "accepted" and category is not None) or (
            status == "rejected" and (not isinstance(category, str) or not category)
        ):
            raise ValueError(f"evaluation case {fixture_id} has an invalid rejection category")
        seen_ids.add(fixture_id)
        cases.append(
            EvaluationCase(
                fixture_id,
                source_text,
                expected_fields,
                model_output,
                status,
                category,
            )
        )
    return payload["schema_version"], tuple(cases)


def _submitted_quotes(model_output: str) -> tuple[object, ...]:
    """Read quote members for scoring only; all contract validation remains in Task 6A."""
    try:
        payload = json.loads(model_output)
    except (json.JSONDecodeError, RecursionError):
        return ()
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list):
        return ()
    return tuple(
        item["evidence"]
        for item in payload["fields"]
        if isinstance(item, dict) and "evidence" in item
    )


def _rejection_category(error: ExtractionValidationError) -> tuple[str, str | None]:
    message = str(error).casefold()
    if "json" in message:
        return "malformed_json", None
    if "does not occur in source text" in message:
        return "ungrounded_evidence", None
    if "unsupported field" in message:
        return "unsupported_field", None
    if "requires a non-empty evidence quote" in message:
        return "missing_evidence", None
    if "must include a grounded title" in message:
        return "missing_required_field", "title"
    for field in sorted(_DATE_FIELDS, key=len, reverse=True):
        if field in message:
            return "invalid_date", field
    if "event start" in message or "event end" in message:
        return "invalid_date", "event_starts" if "start" in message else "event_ends"
    for field in sorted(_URL_FIELDS, key=len, reverse=True):
        if field in message:
            return "invalid_url", field
    return "invalid_value", None


def _normalize_observation(value: object) -> object:
    if isinstance(value, (TextObservation, UrlObservation, FormatObservation)):
        return value.value
    if isinstance(value, DateObservationField):
        if isinstance(value.value, DateOnlyObservation):
            return {"on": value.value.on.isoformat()}
        if isinstance(value.value, DateTimeObservation):
            return {"at": value.value.at.isoformat()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def evaluate_cases(
    cases: tuple[EvaluationCase, ...], *, dataset_version: int = 1
) -> EvaluationReport:
    """Replay fixed outputs through the existing validator and compute labeled metrics."""
    accepted = rejected = predicted_field_count = field_true_positives = 0
    expected_field_count = exact_value_matches = 0
    grounded_quotes = submitted_quote_count = 0
    category_counts: Counter[str] = Counter()
    date_counts: Counter[str] = Counter()
    url_counts: Counter[str] = Counter()
    outcomes: list[CaseOutcome] = []

    for case in cases:
        expected = case.expected_fields
        expected_field_count += len(expected)
        for quote in _submitted_quotes(case.model_output):
            submitted_quote_count += 1
            if isinstance(quote, str) and quote and quote in case.source_text:
                grounded_quotes += 1

        try:
            observations = validate_extraction_result(case.model_output, case.source_text)
        except ExtractionValidationError as error:
            rejected += 1
            category, field = _rejection_category(error)
            category_counts[category] += 1
            if category == "invalid_date" and field:
                date_counts[field] += 1
            elif category == "invalid_url" and field:
                url_counts[field] += 1
            actual_status: EvaluationStatus = "rejected"
            actual_category: str | None = category
        else:
            accepted += 1
            predicted_field_count += len(observations)
            field_true_positives += len(observations.keys() & expected.keys())
            exact_value_matches += sum(
                field in observations
                and _normalize_observation(observations[field]) == expected_value
                for field, expected_value in expected.items()
            )
            actual_status = "accepted"
            actual_category = None

        outcomes.append(
            CaseOutcome(
                case.fixture_id,
                actual_status,
                actual_category,
                actual_status == case.expected_status
                and actual_category == case.expected_rejection_category,
            )
        )

    return EvaluationReport(
        dataset_version=dataset_version,
        case_outcomes=tuple(outcomes),
        accepted_count=accepted,
        rejected_count=rejected,
        field_precision=Rate(field_true_positives, predicted_field_count),
        field_recall=Rate(field_true_positives, expected_field_count),
        exact_match_accuracy=Rate(exact_value_matches, expected_field_count),
        evidence_grounding_rate=Rate(grounded_quotes, submitted_quote_count),
        rejection_categories=tuple(sorted(category_counts.items())),
        invalid_dates_by_field=tuple(sorted(date_counts.items())),
        invalid_urls_by_field=tuple(sorted(url_counts.items())),
    )


def run_fixture_benchmark(path: Path = _DATASET_PATH) -> EvaluationReport:
    version, cases = load_evaluation_cases(path)
    return evaluate_cases(cases, dataset_version=version)


def _format_rate(rate: Rate) -> str:
    if rate.value is None:
        return "n/a (0/0)"
    return f"{rate.value:.1%} ({rate.numerator}/{rate.denominator})"


def main() -> int:
    report = run_fixture_benchmark()
    print(f"Offline extraction benchmark (dataset v{report.dataset_version})")
    print(
        f"Cases: {len(report.case_outcomes)}; accepted: {report.accepted_count}; "
        f"rejected: {report.rejected_count}; expected outcomes matched: "
        f"{sum(item.expected_outcome_matches for item in report.case_outcomes)}"
        f"/{len(report.case_outcomes)}"
    )
    print(
        "Field precision: "
        f"{_format_rate(report.field_precision)}; field recall: "
        f"{_format_rate(report.field_recall)}; exact-match accuracy: "
        f"{_format_rate(report.exact_match_accuracy)}"
    )
    print(f"Evidence grounding: {_format_rate(report.evidence_grounding_rate)}")
    print(
        "Rejections: "
        + (
            ", ".join(f"{name}={count}" for name, count in report.rejection_categories)
            or "none"
        )
    )
    if report.invalid_dates_by_field:
        print(
            "Invalid dates by field: "
            + ", ".join(f"{name}={count}" for name, count in report.invalid_dates_by_field)
        )
    if report.invalid_urls_by_field:
        print(
            "Invalid URLs by field: "
            + ", ".join(f"{name}={count}" for name, count in report.invalid_urls_by_field)
        )
    return 0 if report.expected_outcomes_match else 1


if __name__ == "__main__":
    raise SystemExit(main())