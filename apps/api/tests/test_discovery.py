import json
from pathlib import Path

import httpx
import pytest

from app.discovery import DiscoveryCandidate
from app.discovery.cli import _load_candidates
from app.discovery.http import BoundedHttpClient, FetchError
from app.discovery.urls import UnsafeSourceUrl, normalize_source_url, validate_source_url

FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_candidates_load_deterministically():
    candidates, skipped = _load_candidates(FIXTURES / "discovery_candidates.json")
    assert skipped == 0
    assert len(candidates) == 2
    assert candidates[0] == DiscoveryCandidate(
        title="Example Climate Hackathon",
        source_url="https://events.example.test/2027/climate?utm_source=fixture",
        publisher_name="Example Events",
        source_kind="aggregator",
    )


def test_url_normalization_removes_tracking_and_fragment():
    actual = normalize_source_url(
        "HTTPS://Events.Example.Test:443/2027//climate/?tag=x&other=y&"
        "utm_medium=email#details",
        {"example.test"},
    )
    assert actual == "https://events.example.test/2027//climate/?tag=x&other=y"


@pytest.mark.parametrize(
    "url",
    [
        "http://events.example.test/event",
        "https://events.other.test/event",
        "https://user:password@events.example.test/event",
        "https://127.0.0.1/event",
        "https://10.2.3.4/event",
        "https://[::1]/event",
        "https://[fe80::1]/event",
        "https://events.example.test:8443/event",
        " https://events.example.test/event",
        "https://events.example.test/event\n",
    ],
)
def test_url_validation_rejects_unsafe_or_unapproved_urls(url):
    with pytest.raises(UnsafeSourceUrl):
        validate_source_url(url, {"example.test"})


def test_http_client_reads_fixture_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'{"items": []}')
    )
    with BoundedHttpClient(
        {"example.test"}, transport=transport, min_request_interval=0
    ) as client:
        assert client.get("https://api.example.test/list") == b'{"items": []}'


def test_http_client_validates_redirect_before_following():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "https://attacker.test/steal"}
        )
    )
    with BoundedHttpClient(
        {"example.test"}, transport=transport, min_request_interval=0
    ) as client:
        with pytest.raises(UnsafeSourceUrl):
            client.get("https://api.example.test/list")


@pytest.mark.parametrize("location", ["http://api.example.test/private", "https://127.0.0.1/"])
def test_http_client_rejects_unsafe_redirect_targets(location):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    with BoundedHttpClient(
        {"example.test"},
        transport=httpx.MockTransport(respond),
        min_request_interval=0,
    ) as client:
        with pytest.raises(UnsafeSourceUrl):
            client.get("https://api.example.test/list")
    assert len(requests) == 1


def test_http_client_enforces_response_size_limit():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"12345"))
    with BoundedHttpClient(
        {"example.test"},
        transport=transport,
        max_bytes=4,
        min_request_interval=0,
    ) as client:
        with pytest.raises(FetchError, match="size limit"):
            client.get("https://api.example.test/list")


def test_http_client_retries_429_and_respects_retry_after():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    waits = []
    transport = httpx.MockTransport(lambda request: next(responses))
    with BoundedHttpClient(
        {"example.test"},
        transport=transport,
        max_retries=1,
        min_request_interval=0,
        sleep=waits.append,
    ) as client:
        assert client.get("https://api.example.test/list") == b"ok"
    assert 2.0 in waits


def test_http_client_does_not_retry_before_long_retry_after():
    requests = []
    waits = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "60"})

    with BoundedHttpClient(
        {"example.test"},
        transport=httpx.MockTransport(respond),
        min_request_interval=0,
        sleep=waits.append,
    ) as client:
        with pytest.raises(FetchError, match="Retry-After"):
            client.get("https://api.example.test/list")
    assert len(requests) == 1
    assert waits == []


def test_http_client_retries_transient_timeout():
    attempts = 0
    waits = []

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(respond)
    with BoundedHttpClient(
        {"example.test"},
        transport=transport,
        max_retries=1,
        min_request_interval=0,
        sleep=waits.append,
    ) as client:
        assert client.get("https://api.example.test/list") == b"ok"
    assert attempts == 2
    assert waits == [0.5]


def test_http_client_does_not_retry_permanent_http_errors():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(403)

    transport = httpx.MockTransport(respond)
    with BoundedHttpClient(
        {"example.test"}, transport=transport, min_request_interval=0
    ) as client:
        with pytest.raises(FetchError, match="403"):
            client.get("https://api.example.test/list")
    assert len(requests) == 1


def test_candidate_loader_counts_malformed_entries(tmp_path):
    source = tmp_path / "candidates.json"
    source.write_text(
        json.dumps(
            [
                {"title": "Valid"},
                2,
                {},
                {"title": 42, "source_url": "https://x.test", "publisher_name": "x"},
                {
                    "title": "Invalid kind",
                    "source_url": "https://x.test",
                    "publisher_name": "x",
                    "source_kind": {},
                },
            ]
        ),
        encoding="utf-8",
    )
    candidates, skipped = _load_candidates(source)
    assert candidates == []
    assert skipped == 5
