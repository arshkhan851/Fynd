from types import SimpleNamespace

import httpx
import pytest

from app.discovery.extraction import ExtractionValidationError
from app.discovery.http import FetchError
from app.discovery.ollama import OllamaExtractionClient, OllamaExtractionError
from app.discovery.orchestration import (
    AuthorizedSourceExtraction,
    SourceContentError,
)
from app.discovery.registry import RegistryError
from app.discovery.urls import UnsafeSourceUrl

SOURCE_ID = "fixture-source"
SOURCE_URL = "https://events.example.test/competition"
PAGE = (
    b"<html><head><title>Fynd Demo</title></head><body>"
    b"<p>The Fynd Demo is open.</p><script>ignore</script></body></html>"
)
MODEL_RESULT = (
    '{"fields":[{"field":"title","value":"Fynd Demo",'
    '"evidence":"Fynd Demo"}]}'
)


def _source(*, hosts=None, max_bytes=20_000):
    return SimpleNamespace(
        id=SOURCE_ID,
        publisher_name="Fixture Publisher",
        access={
            "method": "html",
            "allowed_hosts": hosts or ["example.test"],
        },
        limits={
            "max_response_bytes": max_bytes,
            "max_retries": 1,
            "min_request_interval_s": 0.01,
        },
    )


class _FakeOllama:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.received_text = None

    def extract(self, source_text):
        self.received_text = source_text
        if self.error:
            raise self.error
        return self.result


def _orchestrator(handler, ollama, *, max_text_characters=100_000):
    return AuthorizedSourceExtraction(
        ollama,
        registry_path="fixture-registry.toml",
        transport=httpx.MockTransport(handler),
        max_extracted_text_characters=max_text_characters,
    )


def _authorize(monkeypatch, source=None, denial="fetch refused"):
    calls = []

    def authorize(source_id, registry_path):
        calls.append((source_id, registry_path))
        if source is None:
            raise RegistryError(f"source {source_id} {denial}")
        return source

    monkeypatch.setattr(
        "app.discovery.orchestration.authorize_source_execution", authorize
    )
    return calls


@pytest.mark.parametrize("denial", ["is not registered", "fetch permission revoked"])
def test_authorization_denial_stops_before_fetch_or_model(monkeypatch, denial):
    calls = _authorize(monkeypatch, denial=denial)
    fetch_calls = []
    ollama = _FakeOllama()
    orchestrator = _orchestrator(
        lambda request: fetch_calls.append(request), ollama
    )

    with pytest.raises(RegistryError, match=denial):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)

    assert calls == [(SOURCE_ID, "fixture-registry.toml")]
    assert fetch_calls == []
    assert ollama.received_text is None


def test_non_html_registry_method_is_not_fetched(monkeypatch):
    source = _source()
    source.access["method"] = "official_api"
    _authorize(monkeypatch, source)
    fetch_calls = []
    orchestrator = _orchestrator(
        lambda request: fetch_calls.append(request), _FakeOllama()
    )

    with pytest.raises(RegistryError, match="not enabled for HTML extraction"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert fetch_calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://events.example.test/competition",
        "https://attacker.test/competition",
        "https://user:pass@events.example.test/competition",
    ],
)
def test_unsafe_source_urls_are_rejected_before_fetch(monkeypatch, url):
    _authorize(monkeypatch, _source())
    fetch_calls = []
    orchestrator = _orchestrator(
        lambda request: fetch_calls.append(request), _FakeOllama()
    )

    with pytest.raises(UnsafeSourceUrl):
        orchestrator.extract(SOURCE_ID, url)

    assert fetch_calls == []


def test_redirect_is_rejected_without_following_even_to_an_allowed_host(monkeypatch):
    _authorize(monkeypatch, _source(hosts=["example.test", "cdn.example.test"]))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://cdn.example.test/elsewhere"},
        )

    orchestrator = _orchestrator(respond, _FakeOllama())
    with pytest.raises(FetchError, match="redirect limit"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert len(requests) == 1


def test_registry_response_byte_limit_is_enforced(monkeypatch):
    _authorize(monkeypatch, _source(max_bytes=24))
    ollama = _FakeOllama()
    orchestrator = _orchestrator(
        lambda request: httpx.Response(200, content=PAGE), ollama
    )

    with pytest.raises(FetchError, match="size limit"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text is None


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>\xff</body></html>",
        b"<html><script>x</script></html>",
        b"plain text only",
    ],
)
def test_malformed_or_empty_html_is_rejected_before_model(monkeypatch, body):
    _authorize(monkeypatch, _source())
    ollama = _FakeOllama()
    orchestrator = _orchestrator(
        lambda request: httpx.Response(200, content=body), ollama
    )

    with pytest.raises(SourceContentError):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text is None


def test_http_fetch_failure_is_not_sent_to_model(monkeypatch):
    _authorize(monkeypatch, _source())
    ollama = _FakeOllama()
    orchestrator = _orchestrator(lambda request: httpx.Response(404), ollama)

    with pytest.raises(FetchError, match="HTTP status 404"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text is None


def test_network_fetch_failure_is_not_sent_to_model(monkeypatch):
    _authorize(monkeypatch, _source())
    ollama = _FakeOllama()

    def fail(request):
        raise httpx.ConnectError("offline", request=request)

    orchestrator = _orchestrator(fail, ollama)
    with pytest.raises(FetchError, match="network retry exhausted"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text is None


def test_extracted_text_limit_is_enforced_before_model(monkeypatch):
    _authorize(monkeypatch, _source())
    ollama = _FakeOllama()
    orchestrator = _orchestrator(
        lambda request: httpx.Response(200, content=PAGE),
        ollama,
        max_text_characters=12,
    )

    with pytest.raises(SourceContentError, match="text exceeds size limit"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text is None


def test_model_failure_propagates_without_persistence_or_fallback(monkeypatch):
    _authorize(monkeypatch, _source())
    ollama = _FakeOllama(error=OllamaExtractionError("model unavailable"))
    orchestrator = _orchestrator(
        lambda request: httpx.Response(200, content=PAGE), ollama
    )

    with pytest.raises(OllamaExtractionError, match="unavailable"):
        orchestrator.extract(SOURCE_ID, SOURCE_URL)
    assert ollama.received_text == "Fynd Demo The Fynd Demo is open."


def test_success_returns_registry_context_and_validated_observations(monkeypatch):
    _authorize(monkeypatch, _source())
    model_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": MODEL_RESULT})
    )
    with OllamaExtractionClient(model="test-model", transport=model_transport) as ollama:
        orchestrator = _orchestrator(
            lambda request: httpx.Response(200, content=PAGE), ollama
        )
        result = orchestrator.extract(SOURCE_ID, SOURCE_URL)

    assert result.registry_source_id == SOURCE_ID
    assert result.source_url == SOURCE_URL
    assert result.publisher_name == "Fixture Publisher"
    assert result.observations["title"].value == "Fynd Demo"


def test_task_6a_validation_rejection_is_not_bypassed(monkeypatch):
    _authorize(monkeypatch, _source())
    invalid_result = MODEL_RESULT.replace("Fynd Demo", "Fabricated")
    model_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"response": invalid_result})
    )
    with OllamaExtractionClient(model="test-model", transport=model_transport) as ollama:
        orchestrator = _orchestrator(
            lambda request: httpx.Response(200, content=PAGE), ollama
        )
        with pytest.raises(OllamaExtractionError) as error:
            orchestrator.extract(SOURCE_ID, SOURCE_URL)

    assert isinstance(error.value.__cause__, ExtractionValidationError)
