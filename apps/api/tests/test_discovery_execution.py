import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.discovery import cli, execution
from app.discovery.kaggle import KaggleDiscoveryAdapter, KaggleDiscoveryError
from app.discovery.persistence import DiscoverySummary
from app.discovery.registry import RegistryError


def _source(source_id):
    return SimpleNamespace(
        id=source_id,
        homepage_url=f"https://{source_id}.example.test/events",
    )


def _install_registry(monkeypatch, source_ids):
    sources = {source_id: _source(source_id) for source_id in source_ids}
    monkeypatch.setattr(
        execution,
        "authorize_source_execution",
        lambda source_id, _path: sources[source_id],
    )
    return sources


def test_selected_source_execution_returns_structured_success(monkeypatch):
    calls = []
    sources = _install_registry(monkeypatch, ["alpha"])

    def authorize(source_id, registry_path):
        calls.append(("authorize", source_id, registry_path))
        return sources[source_id]

    monkeypatch.setattr(execution, "authorize_source_execution", authorize)

    def run(session, source_id, source_url, **kwargs):
        calls.append(("pipeline", session, source_id, source_url, kwargs))
        return DiscoverySummary(discovered=1, skipped=2, skipped_fields=("format",))

    monkeypatch.setattr(execution, "run_authorized_extraction_pipeline", run)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session, session.begin():
        result = execution.execute_selected_sources(
            session, ["alpha"], ollama_client=object(), registry_path="fixture.toml"
        )

    assert result.outcomes == (
        execution.SourceExecutionOutcome(
            "alpha",
            "succeeded",
            sources["alpha"].homepage_url,
            DiscoverySummary(discovered=1, skipped=2, skipped_fields=("format",)),
        ),
    )
    assert calls[0] == ("authorize", "alpha", "fixture.toml")
    assert calls[1][0:1] == ("pipeline",)
    assert calls[1][2:4] == ("alpha", sources["alpha"].homepage_url)
    assert calls[1][4]["registry_path"] == "fixture.toml"


def test_unauthorized_and_missing_sources_have_outcomes_without_pipeline(monkeypatch):
    sources = {"denied": _source("denied")}

    def authorize(source_id, _path):
        if source_id not in sources:
            raise RegistryError(f"source {source_id} is not registered")
        raise RegistryError(f"source {source_id} fetch refused: permission denied")

    monkeypatch.setattr(execution, "authorize_source_execution", authorize)
    monkeypatch.setattr(
        execution,
        "run_authorized_extraction_pipeline",
        lambda *_args, **_kwargs: pytest.fail("pipeline ran for unavailable source"),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session, session.begin():
        result = execution.execute_selected_sources(
            session, ["denied", "missing"], ollama_client=object()
        )

    assert [(item.source_id, item.status) for item in result.outcomes] == [
        ("denied", "unauthorized"),
        ("missing", "not_found"),
    ]
    assert "fetch refused" in result.outcomes[0].reason
    assert result.outcomes[1].reason == "source missing is not registered"


@pytest.mark.parametrize(
    "source_ids, error",
    [
        (["a", "b", "c", "d", "e", "f"], "at most 5"),
        (["a", "a"], "duplicate source IDs"),
        ([], "at least one"),
        (["a", ""], "non-empty strings"),
    ],
)
def test_selection_validation_happens_before_authorization_or_pipeline(
    monkeypatch, source_ids, error
):
    monkeypatch.setattr(
        execution,
        "authorize_source_execution",
        lambda *_args: pytest.fail("authorization ran for invalid selection"),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session, pytest.raises(ValueError, match=error):
        execution.execute_selected_sources(
            session, source_ids, ollama_client=object()
        )


def test_source_failure_rolls_back_its_savepoint_and_later_source_succeeds(monkeypatch):
    _install_registry(monkeypatch, ["first", "second"])
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE execution_rows (source_id TEXT NOT NULL)"))

    def run(session, source_id, _source_url, **_kwargs):
        session.execute(
            text("INSERT INTO execution_rows (source_id) VALUES (:source_id)"),
            {"source_id": source_id},
        )
        if source_id == "first":
            raise RuntimeError("simulated pipeline failure")
        return DiscoverySummary(discovered=1)

    monkeypatch.setattr(execution, "run_authorized_extraction_pipeline", run)
    with Session(engine) as session, session.begin():
        result = execution.execute_selected_sources(
            session, ["first", "second"], ollama_client=object()
        )
        persisted = session.scalars(text("SELECT source_id FROM execution_rows")).all()

    assert [(item.source_id, item.status) for item in result.outcomes] == [
        ("first", "failed"),
        ("second", "succeeded"),
    ]
    assert result.outcomes[0].reason == (
        "execution failed (RuntimeError): simulated pipeline failure"
    )
    assert persisted == ["second"]


def test_persistence_failure_summary_is_not_reported_as_success(monkeypatch):
    _install_registry(monkeypatch, ["alpha"])
    monkeypatch.setattr(
        execution,
        "run_authorized_extraction_pipeline",
        lambda *_args, **_kwargs: DiscoverySummary(failed=1),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session, session.begin():
        result = execution.execute_selected_sources(
            session, ["alpha"], ollama_client=object()
        )
    assert result.outcomes[0].status == "failed"
    assert result.outcomes[0].persistence == DiscoverySummary(failed=1)
    assert result.outcomes[0].reason == "persistence reported a candidate failure"


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
