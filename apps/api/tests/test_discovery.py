import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
from kagglesdk.competitions.types.competition_api_service import (
    ApiCompetition,
    ApiListCompetitionsResponse,
)

from app.discovery import DiscoveryCandidate
from app.discovery.cli import _load_candidates
from app.discovery.http import BoundedHttpClient, FetchError
from app.discovery.kaggle import (
    KaggleAuthenticationError,
    KaggleDiscoveryAdapter,
    KaggleMalformedResponseError,
    KaggleRateLimitError,
    KaggleTimeoutError,
)
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


def _kaggle_competition(title, url, ref):
    competition = ApiCompetition()
    competition.title = title
    competition.url = url
    competition.ref = ref
    return competition


def _kaggle_response(competitions, next_page_token=None):
    response = ApiListCompetitionsResponse()
    response.competitions = competitions
    response.next_page_token = next_page_token
    return response


def test_kaggle_adapter_maps_documented_fields_and_paginates():
    first = _kaggle_response(
        [
            _kaggle_competition(
                "First Challenge", "https://www.kaggle.com/competitions/first", "first"
            )
        ],
        "next-page-token",
    )
    second = _kaggle_response(
        [
            _kaggle_competition(
                "Second Challenge", "https://www.kaggle.com/competitions/second", "second"
            )
        ]
    )
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    calls = []

    def competitions_list(**kwargs):
        calls.append(kwargs)
        return [first, second][len(calls) - 1]

    fake_api.competitions_list = competitions_list
    batch = KaggleDiscoveryAdapter(
        api=fake_api, page_size=1, max_pages=2, result_limit=2
    ).discover()

    assert [candidate.title for candidate in batch.candidates] == [
        "First Challenge",
        "Second Challenge",
    ]
    assert [candidate.external_id for candidate in batch.candidates] == ["first", "second"]
    assert [candidate.publisher_name for candidate in batch.candidates] == ["Kaggle", "Kaggle"]
    assert batch.pages_fetched == 2
    assert batch.skipped == 0
    assert calls[0]["page_token"] is None
    assert calls[1]["page_token"] == "next-page-token"
    assert all(call["group"] == "general" for call in calls)


def test_kaggle_adapter_caps_overfull_page_at_result_limit():
    response = _kaggle_response(
        [
            _kaggle_competition(
                f"Challenge {n}",
                f"https://www.kaggle.com/competitions/{n}",
                str(n),
            )
            for n in range(3)
        ],
        "should-not-fetch",
    )
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    calls = []

    def competitions_list(**kwargs):
        calls.append(kwargs)
        return response

    fake_api.competitions_list = competitions_list
    batch = KaggleDiscoveryAdapter(
        api=fake_api, page_size=5, max_pages=3, result_limit=2
    ).discover()

    assert [candidate.title for candidate in batch.candidates] == ["Challenge 0", "Challenge 1"]
    assert batch.pages_fetched == 1
    assert len(calls) == 1
    assert calls[0]["page_size"] == 2


def test_kaggle_adapter_never_fetches_more_than_max_pages():
    responses = [
        _kaggle_response(
            [
                _kaggle_competition(
                    f"Challenge {n}",
                    f"https://www.kaggle.com/competitions/{n}",
                    str(n),
                )
            ],
            f"token-{n}" if n == 0 else "another-token",
        )
        for n in range(3)
    ]
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    calls = []

    def competitions_list(**kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    fake_api.competitions_list = competitions_list
    batch = KaggleDiscoveryAdapter(
        api=fake_api, page_size=2, max_pages=2, result_limit=4
    ).discover()

    assert len(batch.candidates) == 2
    assert batch.pages_fetched == len(calls) == 2
    assert calls[0]["page_token"] is None
    assert calls[1]["page_token"] == "token-0"


def test_kaggle_adapter_skips_unconfirmed_or_untrusted_rows():
    response = _kaggle_response(
        [
            _kaggle_competition("", "https://www.kaggle.com/competitions/no-title", "no-title"),
            _kaggle_competition("Unsafe", "https://attacker.test/competition/unsafe", "unsafe"),
            _kaggle_competition("Valid", "https://www.kaggle.com/competitions/valid", "valid"),
        ]
    )
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    fake_api.competitions_list = lambda **kwargs: response

    batch = KaggleDiscoveryAdapter(
        api=fake_api, page_size=5, max_pages=1, result_limit=5
    ).discover()

    assert [candidate.title for candidate in batch.candidates] == ["Valid"]
    assert batch.skipped == 2


def test_kaggle_adapter_handles_empty_and_malformed_responses():
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    fake_api.competitions_list = lambda **kwargs: _kaggle_response([])
    batch = KaggleDiscoveryAdapter(
        api=fake_api, page_size=2, max_pages=1, result_limit=2
    ).discover()
    assert batch.candidates == ()
    assert batch.pages_fetched == 1

    fake_api.competitions_list = lambda **kwargs: None
    with pytest.raises(KaggleMalformedResponseError):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=2, max_pages=1, result_limit=2
        ).discover()


def test_kaggle_adapter_suppresses_client_output(capsys):
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None

    def competitions_list(**kwargs):
        print("KAGGLE_SECRET_MUST_NOT_PRINT")
        return _kaggle_response([])

    fake_api.competitions_list = competitions_list
    KaggleDiscoveryAdapter(
        api=fake_api, page_size=2, max_pages=1, result_limit=2
    ).discover()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_kaggle_adapter_reports_missing_authentication_without_client_output(capsys):
    fake_api = type("FakeKaggleApi", (), {})()

    def unauthenticated():
        raise SystemExit(1)

    fake_api.authenticate = unauthenticated
    with pytest.raises(KaggleAuthenticationError, match="credentials"):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=2, max_pages=1, result_limit=2
        ).discover()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.Timeout("timeout"), KaggleTimeoutError),
        (requests.HTTPError("rate limited", response=requests.Response()), KaggleRateLimitError),
    ],
)
def test_kaggle_adapter_maps_timeouts_and_rate_limits(error, expected):
    if isinstance(error, requests.HTTPError):
        error.response.status_code = 429
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    fake_api.competitions_list = lambda **kwargs: (_ for _ in ()).throw(error)

    with pytest.raises(expected):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=2, max_pages=1, result_limit=2
        ).discover()


@pytest.mark.parametrize("status", [401, 403])
def test_kaggle_adapter_maps_authentication_statuses(status):
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("authentication rejected", response=response)
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    fake_api.competitions_list = lambda **kwargs: (_ for _ in ()).throw(error)

    with pytest.raises(KaggleAuthenticationError):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=2, max_pages=1, result_limit=2
        ).discover()


@pytest.mark.parametrize("token", [123, "   ", [], {}])
def test_kaggle_adapter_rejects_malformed_pagination_tokens(token):
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None
    fake_api.competitions_list = lambda **kwargs: SimpleNamespace(
        competitions=[
            _kaggle_competition(
                "Challenge",
                "https://www.kaggle.com/competitions/challenge",
                "challenge",
            )
        ],
        next_page_token=token,
    )

    with pytest.raises(KaggleMalformedResponseError, match="pagination token"):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=2, max_pages=2, result_limit=2
        ).discover()


def test_kaggle_adapter_rejects_repeated_pagination_token():
    responses = [
        _kaggle_response(
            [_kaggle_competition("A", "https://www.kaggle.com/competitions/a", "a")],
            "token-a",
        ),
        _kaggle_response(
            [_kaggle_competition("B", "https://www.kaggle.com/competitions/b", "b")],
            "token-b",
        ),
        _kaggle_response(
            [_kaggle_competition("C", "https://www.kaggle.com/competitions/c", "c")],
            "token-a",
        ),
    ]
    calls = []
    fake_api = type("FakeKaggleApi", (), {})()
    fake_api.authenticate = lambda: None

    def competitions_list(**kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    fake_api.competitions_list = competitions_list
    with pytest.raises(KaggleMalformedResponseError, match="pagination token"):
        KaggleDiscoveryAdapter(
            api=fake_api, page_size=1, max_pages=4, result_limit=4
        ).discover()

    assert [call["page_token"] for call in calls] == [None, "token-a", "token-b"]
