import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.discovery import cli
from app.discovery.kaggle import KaggleDiscoveryAdapter, KaggleDiscoveryError
from app.discovery.registry import RegistryError


def test_cli_refuses_before_adapter_or_request(monkeypatch):
    called = []
    def deny(*args, **kwargs):
        raise RegistryError("denied")

    monkeypatch.setattr(cli, "authorize_source_execution", deny)
    monkeypatch.setattr(
        cli.KaggleDiscoveryAdapter,
        "discover",
        lambda self: called.append("discover"),
    )
    monkeypatch.setattr(sys, "argv", ["fynd", "--kaggle"])

    assert cli.main() == 1
    assert called == []


def test_direct_adapter_refuses_before_authentication_or_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.discovery.kaggle.authorize_source_execution",
        lambda *a, **k: (_ for _ in ()).throw(RegistryError("denied")),
    )
    api = type("FakeApi", (), {})()
    api.authenticate = lambda: calls.append("authenticate")
    api.competitions_list = lambda **kwargs: calls.append("request")

    with pytest.raises(KaggleDiscoveryError, match="denied"):
        KaggleDiscoveryAdapter(api=api).discover()
    assert calls == []


def test_missing_registry_stops_direct_adapter_before_client(monkeypatch, tmp_path: Path):
    calls = []
    api = type("FakeApi", (), {})()
    api.authenticate = lambda: calls.append("authenticate")
    def deny(*args, **kwargs):
        raise RegistryError("cannot load registry")

    monkeypatch.setattr("app.discovery.kaggle.authorize_source_execution", deny)

    with pytest.raises(KaggleDiscoveryError, match="cannot load registry"):
        KaggleDiscoveryAdapter(api=api, registry_path=tmp_path / "missing.toml").discover()
    assert calls == []


def test_guard_precedes_authentication_when_guard_allows(monkeypatch):
    order = []
    monkeypatch.setattr(
        "app.discovery.kaggle.authorize_source_execution",
        lambda *a, **k: order.append("guard") or object(),
    )
    api = type("FakeApi", (), {})()
    api.authenticate = lambda: order.append("authenticate")
    api.competitions_list = lambda **kwargs: type(
        "Response", (), {"competitions": [], "next_page_token": None}
    )()

    KaggleDiscoveryAdapter(api=api).discover()
    assert order == ["guard", "authenticate"]


def test_cli_passes_adapter_registry_identity_separately_to_persistence(monkeypatch, capsys):
    captured = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        def discover(self):
            return SimpleNamespace(
                candidates=(),
                skipped=2,
                registry_source_id="kaggle",
            )

    monkeypatch.setattr(sys, "argv", ["fynd-discovery", "--kaggle"])
    monkeypatch.setattr(cli, "authorize_source_execution", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "KaggleDiscoveryAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "SessionLocal", FakeSession)

    def persist(_session, candidates, allowed_hosts, *, registry_source_id):
        captured.update(
            candidates=candidates,
            allowed_hosts=allowed_hosts,
            registry_source_id=registry_source_id,
        )
        return SimpleNamespace(
            discovered=0,
            skipped=1,
            skipped_fields=("format",),
            existing=0,
            failed=0,
        )

    monkeypatch.setattr(cli, "persist_candidates", persist)

    assert cli.main() == 0
    assert captured == {
        "candidates": [],
        "allowed_hosts": {"kaggle.com"},
        "registry_source_id": "kaggle",
    }
    assert json.loads(capsys.readouterr().out) == {
        "discovered": 0,
        "skipped": 2,
        "skipped_observations": 1,
        "skipped_fields": ["format"],
        "existing": 0,
        "failed": 0,
    }
