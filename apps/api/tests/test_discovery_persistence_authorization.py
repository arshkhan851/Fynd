from types import SimpleNamespace

import pytest

from app.discovery import DiscoveryCandidate, persistence
from app.discovery.persistence import (
    DiscoverySummary,
    _validate_existing_source_identity,
    persist_candidates,
)
from app.discovery.registry import RegistryError

SOURCE_ID = "synthetic-source"
PUBLISHER = "Synthetic Publisher"
CANDIDATE_URL = "https://events.example.test/competition"
ALL_FIELDS = {
    "title",
    "description",
    "official_url",
    "registration_url",
    "registration_deadline",
    "event_starts",
    "event_ends",
    "event_timezone",
    "format",
    "eligibility_summary",
}


class NoDatabaseSession:
    def get_bind(self):
        raise AssertionError("database session was accessed before authorization")


def _source(source_id=SOURCE_ID):
    return SimpleNamespace(
        id=source_id,
        publisher_name=PUBLISHER,
        source_kind="aggregator",
        access={"allowed_hosts": ["example.test"]},
        use={"fields": sorted(ALL_FIELDS)},
    )


def _candidate(**changes):
    values = {
        "title": "Synthetic competition",
        "source_url": CANDIDATE_URL,
        "publisher_name": PUBLISHER,
        "source_kind": "aggregator",
    }
    values.update(changes)
    return DiscoveryCandidate(**values)


def _authorization(monkeypatch, *, allowed=("store", "internal_review")):
    calls = []

    def authorize(source_id, operation):
        calls.append((source_id, operation))
        if source_id != SOURCE_ID:
            raise RegistryError(f"source {source_id} is not registered")
        if operation not in allowed:
            raise RegistryError(f"{operation} permission is denied")
        return _source(source_id)

    monkeypatch.setattr(persistence, "authorize_source_operation", authorize)
    return calls


def test_missing_trusted_source_identity_is_rejected_before_authorization(monkeypatch):
    calls = _authorization(monkeypatch)

    with pytest.raises(RegistryError, match="trusted registry source identity is required"):
        persist_candidates(NoDatabaseSession(), [], {"example.test"})

    assert calls == []


def test_invalid_trusted_source_identity_is_rejected_before_database_access(monkeypatch):
    calls = _authorization(monkeypatch)

    with pytest.raises(RegistryError, match="not registered"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate()],
            {"example.test"},
            registry_source_id="unregistered-source",
        )

    assert calls == [("unregistered-source", "store")]


def test_fetch_permission_does_not_substitute_for_store_permission(monkeypatch):
    calls = _authorization(monkeypatch, allowed=("fetch",))

    with pytest.raises(RegistryError, match="store permission is denied"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate()],
            {"example.test"},
            registry_source_id=SOURCE_ID,
        )

    assert calls == [(SOURCE_ID, "store")]


def test_store_permission_does_not_substitute_for_internal_review(monkeypatch):
    calls = _authorization(monkeypatch, allowed=("store",))

    with pytest.raises(RegistryError, match="internal_review permission is denied"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate()],
            {"example.test"},
            registry_source_id=SOURCE_ID,
        )

    assert calls == [(SOURCE_ID, "store"), (SOURCE_ID, "internal_review")]


@pytest.mark.parametrize(
    "metadata_change",
    [
        {"publisher_name": "Forged Publisher"},
        {"source_kind": "official"},
    ],
)
def test_source_metadata_mismatch_rejects_whole_batch_before_database_access(
    monkeypatch, metadata_change
):
    calls = _authorization(monkeypatch)

    with pytest.raises(RegistryError, match="does not match the trusted registry source"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate(), _candidate(**metadata_change)],
            {"example.test"},
            registry_source_id=SOURCE_ID,
        )

    assert calls == [(SOURCE_ID, "store"), (SOURCE_ID, "internal_review")]


def test_candidate_publisher_cannot_forge_trusted_source_identity():
    with pytest.raises(TypeError, match="registry_source_id"):
        DiscoveryCandidate(
            title="Synthetic competition",
            source_url=CANDIDATE_URL,
            publisher_name=PUBLISHER,
            registry_source_id=SOURCE_ID,
        )


@pytest.mark.parametrize("existing_identity", [None, SOURCE_ID])
def test_deduplicated_source_with_null_or_matching_identity_is_not_rebound(existing_identity):
    source = SimpleNamespace(registry_source_id=existing_identity)

    _validate_existing_source_identity(source, SOURCE_ID)

    assert source.registry_source_id is existing_identity


def test_deduplicated_source_with_conflicting_identity_is_rejected():
    source = SimpleNamespace(registry_source_id="another-source")

    with pytest.raises(RegistryError, match="different registry source identity"):
        _validate_existing_source_identity(source, SOURCE_ID)

    assert source.registry_source_id == "another-source"


def test_registry_host_restriction_rejects_candidate_before_database_access(monkeypatch):
    calls = _authorization(monkeypatch)

    with pytest.raises(RegistryError, match="trusted registry host restrictions"):
        persist_candidates(
            NoDatabaseSession(),
            [_candidate(source_url="https://attacker.example.org/competition")],
            {"example.org"},
            registry_source_id=SOURCE_ID,
        )

    assert calls == [(SOURCE_ID, "store"), (SOURCE_ID, "internal_review")]


def test_both_permissions_are_checked_before_database_access(monkeypatch):
    calls = _authorization(monkeypatch)

    class NoPostgresSession:
        def get_bind(self):
            assert calls == [(SOURCE_ID, "store"), (SOURCE_ID, "internal_review")]
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        persist_candidates(
            NoPostgresSession(),
            [_candidate()],
            {"example.test"},
            registry_source_id=SOURCE_ID,
        )


def test_empty_authorized_batch_does_not_access_database(monkeypatch):
    calls = _authorization(monkeypatch)

    result = persist_candidates(
        NoDatabaseSession(),
        [],
        {"example.test"},
        registry_source_id=SOURCE_ID,
    )

    assert result == DiscoverySummary()
    assert calls == [(SOURCE_ID, "store"), (SOURCE_ID, "internal_review")]
