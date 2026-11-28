from collections import Counter

import httpx
import pytest

from app.database import engine
from app.discovery import evaluation
from app.discovery.extraction import validate_extraction_result


def test_fixture_benchmark_matches_hand_computed_metrics():
    report = evaluation.run_fixture_benchmark()

    assert report.dataset_version == 1
    assert report.accepted_count == 3
    assert report.rejected_count == 5
    assert report.field_precision == evaluation.Rate(6, 6)
    assert report.field_recall == evaluation.Rate(6, 12)
    assert report.exact_match_accuracy == evaluation.Rate(5, 12)
    assert report.evidence_grounding_rate == evaluation.Rate(12, 13)
    assert dict(report.rejection_categories) == {
        "invalid_date": 1,
        "invalid_url": 1,
        "malformed_json": 1,
        "ungrounded_evidence": 1,
        "unsupported_field": 1,
    }
    assert dict(report.invalid_dates_by_field) == {"registration_deadline": 1}
    assert dict(report.invalid_urls_by_field) == {"official_url": 1}
    assert report.expected_outcomes_match


def test_valid_missing_and_incorrect_value_cases_are_scored_by_field_and_value():
    _, cases = evaluation.load_evaluation_cases()
    by_id = {case.fixture_id: case for case in cases}

    complete = validate_extraction_result(
        by_id["grounded_complete"].model_output,
        by_id["grounded_complete"].source_text,
    )
    assert set(complete) == set(by_id["grounded_complete"].expected_fields)

    missing = validate_extraction_result(
        by_id["missing_supported_description"].model_output,
        by_id["missing_supported_description"].source_text,
    )
    assert set(missing) == {"title"}
    assert "description" in by_id["missing_supported_description"].expected_fields

    incorrect = validate_extraction_result(
        by_id["incorrect_title_value"].model_output,
        by_id["incorrect_title_value"].source_text,
    )
    assert incorrect["title"].value == "Fynd Solar Marathon"
    assert by_id["incorrect_title_value"].expected_fields["title"] == "Fynd Solar Sprint"


def test_invalid_cases_are_rejected_in_their_expected_categories():
    report = evaluation.run_fixture_benchmark()
    by_id = {outcome.fixture_id: outcome for outcome in report.case_outcomes}

    assert by_id["fabricated_evidence_quote"].rejection_category == "ungrounded_evidence"
    assert by_id["malformed_json"].rejection_category == "malformed_json"
    assert by_id["invalid_registration_date"].rejection_category == "invalid_date"
    assert by_id["invalid_official_url"].rejection_category == "invalid_url"
    assert by_id["unsupported_prize_field"].rejection_category == "unsupported_field"
    assert all(outcome.expected_outcome_matches for outcome in report.case_outcomes)


def test_benchmark_is_deterministic_and_summary_is_concise(capsys):
    first = evaluation.run_fixture_benchmark()
    second = evaluation.run_fixture_benchmark()

    assert first == second
    assert evaluation.main() == 0
    output = capsys.readouterr().out
    assert "Offline extraction benchmark (dataset v1)" in output
    assert "Field precision: 100.0% (6/6)" in output
    assert "Evidence grounding: 92.3% (12/13)" in output
    assert "invalid_date=1" in output
    assert "source_text" not in output
    assert "model_output" not in output


def test_benchmark_does_not_invoke_model_pipeline_or_persistence(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline evaluation invoked an external or stateful component")

    monkeypatch.setattr("app.discovery.ollama.OllamaExtractionClient.extract", forbidden)
    monkeypatch.setattr("app.discovery.pipeline.run_authorized_extraction_pipeline", forbidden)
    monkeypatch.setattr("app.discovery.execution.execute_selected_sources", forbidden)
    monkeypatch.setattr("app.discovery.persistence.persist_candidates", forbidden)
    monkeypatch.setattr(engine, "connect", forbidden)
    monkeypatch.setattr(engine, "raw_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "stream", forbidden)

    assert evaluation.run_fixture_benchmark().expected_outcomes_match


def test_rejection_counts_are_stable_on_each_case():
    report = evaluation.run_fixture_benchmark()
    rejected = Counter(
        outcome.rejection_category
        for outcome in report.case_outcomes
        if outcome.status == "rejected"
    )
    assert rejected == Counter(dict(report.rejection_categories))
