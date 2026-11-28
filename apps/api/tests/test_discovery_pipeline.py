from types import SimpleNamespace

import httpx
import pytest

from app.discovery.candidates import TextObservation
from app.discovery.extraction import ExtractionValidationError
from app.discovery.http import FetchError
from app.discovery.ollama import OllamaExtractionClient, OllamaExtractionError
from app.discovery.persistence import DiscoverySummary
from app.discovery.pipeline import run_authorized_extraction_pipeline
from app.discovery.registry import RegistryError

SOURCE_ID = "synthetic-test-source:Pipeline Events"
SOURCE_URL = "https://events.example.test/pipeline"
PAGE = (
    b"<html><body><h1>Pipeline Demo</h1>"
    b"<p>Open to learners.</p><script>Ignore this text.</script></body></html>"
)
VALID_MODEL_RESULT = (
    '{"fields":[{"field":"title","value":"Pipeline Demo",'
    '"evidence":"Pipeline Demo"},{"field":"description",'
    '"value":"Open to learners.","evidence":"Open to learners."}]}'
)


def _authorized_source():
    return SimpleNamespace(
        id=SOURCE_ID,
        publisher_name="Pipeline Events",
        source_kind="aggregator",
        access={"method": "html", "allowed_hosts": ["example.test"]},
        limits={
            "max_response_bytes": 10_000,
            "max_retries": 1,
            "min_request_interval_s": 0.01,
        },
    )


def _authorize(monkeypatch, *, error=None, calls=None):
    calls = [] if calls is None else calls

    def authorize(source_id, registry_path):
        calls.append(("authorize", source_id, registry_path))
        if error is not None:
            raise error
        return _authorized_source()

    monkeypatch.setattr(
        "app.discovery.orchestration.authorize_source_execution", authorize
    )
    return calls


def _client(handler):
    return OllamaExtractionClient(
        model="fixture-model",
        transport=httpx.MockTransport(handler),
    )


def test_pipeline_connects_mock_fetch_model_validation_and_persistence(monkeypatch):
    calls = _authorize(monkeypatch)

    def fetch(request):
        calls.append(("fetch", str(request.url)))
        return httpx.Response(200, content=PAGE)

    def model(request):
        calls.append(("model", str(request.url)))
        return httpx.Response(200, json={"response": VALID_MODEL_RESULT})

    captured = {}

    def persist(session, extraction):
        calls.append(("persist", extraction.registry_source_id))
        captured.update(session=session, extraction=extraction)
        return DiscoverySummary(discovered=1)

    monkeypatch.setattr("app.discovery.pipeline.persist_extraction_candidate", persist)
    session = object()

    with _client(model) as ollama:
        summary = run_authorized_extraction_pipeline(
            session,
            SOURCE_ID,
            SOURCE_URL,
            ollama_client=ollama,
            registry_path="fixture-registry.toml",
            fetch_transport=httpx.MockTransport(fetch),
        )

    assert summary == DiscoverySummary(discovered=1)
    assert [call[0] for call in calls] == ["authorize", "fetch", "model", "persist"]
    assert captured["session"] is session
    extraction = captured["extraction"]
    assert extraction.registry_source_id == SOURCE_ID
    assert extraction.source_url == SOURCE_URL
    assert extraction.publisher_name == "Pipeline Events"
    assert extraction.source_kind == "aggregator"
    assert extraction.allowed_hosts == ("example.test",)
    assert extraction.observations == {
        "title": TextObservation("Pipeline Demo", "Pipeline Demo"),
        "description": TextObservation("Open to learners.", "Open to learners."),
    }


def test_authorization_denial_prevents_fetch_model_and_persistence(monkeypatch):
    calls = _authorize(
        monkeypatch, error=RegistryError("source has no fetch authorization")
    )

    def fetch(request):
        calls.append(("fetch", str(request.url)))
        return httpx.Response(200, content=PAGE)

    def model(request):
        calls.append(("model", str(request.url)))
        return httpx.Response(200, json={"response": VALID_MODEL_RESULT})

    monkeypatch.setattr(
        "app.discovery.pipeline.persist_extraction_candidate",
        lambda *_args: pytest.fail("persistence ran after authorization denial"),
    )
    with _client(model) as ollama, pytest.raises(RegistryError, match="no fetch authorization"):
        run_authorized_extraction_pipeline(
            object(),
            SOURCE_ID,
            SOURCE_URL,
            ollama_client=ollama,
            fetch_transport=httpx.MockTransport(fetch),
        )

    assert [call[0] for call in calls] == ["authorize"]


def test_fetch_failure_prevents_model_and_persistence(monkeypatch):
    calls = _authorize(monkeypatch)

    def fetch(request):
        calls.append(("fetch", str(request.url)))
        return httpx.Response(404)

    def model(request):
        calls.append(("model", str(request.url)))
        return httpx.Response(200, json={"response": VALID_MODEL_RESULT})

    monkeypatch.setattr(
        "app.discovery.pipeline.persist_extraction_candidate",
        lambda *_args: pytest.fail("persistence ran after fetch failure"),
    )
    with _client(model) as ollama, pytest.raises(FetchError, match="HTTP status 404"):
        run_authorized_extraction_pipeline(
            object(),
            SOURCE_ID,
            SOURCE_URL,
            ollama_client=ollama,
            fetch_transport=httpx.MockTransport(fetch),
        )

    assert [call[0] for call in calls] == ["authorize", "fetch"]


@pytest.mark.parametrize(
    "response, error_match, cause_type",
    [
        (httpx.Response(503), "HTTP 503", None),
        (
            httpx.Response(
                200,
                json={"response": VALID_MODEL_RESULT.replace("Pipeline Demo", "Fabricated")},
            ),
            "source validation",
            ExtractionValidationError,
        ),
    ],
)
def test_model_or_grounding_failure_prevents_persistence(
    monkeypatch, response, error_match, cause_type
):
    calls = _authorize(monkeypatch)

    def fetch(request):
        calls.append(("fetch", str(request.url)))
        return httpx.Response(200, content=PAGE)

    def model(request):
        calls.append(("model", str(request.url)))
        return response

    monkeypatch.setattr(
        "app.discovery.pipeline.persist_extraction_candidate",
        lambda *_args: pytest.fail("persistence ran after model/validation failure"),
    )
    with _client(model) as ollama, pytest.raises(
        OllamaExtractionError, match=error_match
    ) as error:
        run_authorized_extraction_pipeline(
            object(),
            SOURCE_ID,
            SOURCE_URL,
            ollama_client=ollama,
            fetch_transport=httpx.MockTransport(fetch),
        )

    assert [call[0] for call in calls] == ["authorize", "fetch", "model"]
    if cause_type:
        assert isinstance(error.value.__cause__, cause_type)


def test_persistence_failure_propagates_without_fallback(monkeypatch):
    calls = _authorize(monkeypatch)

    def fetch(request):
        calls.append(("fetch", str(request.url)))
        return httpx.Response(200, content=PAGE)

    def model(request):
        calls.append(("model", str(request.url)))
        return httpx.Response(200, json={"response": VALID_MODEL_RESULT})

    def fail_persist(_session, extraction):
        calls.append(("persist", extraction.registry_source_id))
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr("app.discovery.pipeline.persist_extraction_candidate", fail_persist)
    with _client(model) as ollama, pytest.raises(RuntimeError, match="persistence failure"):
        run_authorized_extraction_pipeline(
            object(),
            SOURCE_ID,
            SOURCE_URL,
            ollama_client=ollama,
            fetch_transport=httpx.MockTransport(fetch),
        )

    assert [call[0] for call in calls] == ["authorize", "fetch", "model", "persist"]
