import sys
from pathlib import Path

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
