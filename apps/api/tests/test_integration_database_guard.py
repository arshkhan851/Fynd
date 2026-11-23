from types import SimpleNamespace

import pytest
from integration_database_guard import (
    EXPECTED_DATABASE_TARGET,
    guard_isolated_postgres_integration_tests,
    validate_database_target,
    verify_live_database_identity,
)
from sqlalchemy.engine import make_url


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "not-a-url",
        "postgresql://fynd_test_user:secret@localhost:56000/fynd_test",
        "postgresql://fynd_test_user:secret@127.0.0.1:5432/fynd_test",
        "postgresql://fynd_test_user:secret@127.0.0.1:56000/fynd",
        "postgresql://fynd:secret@127.0.0.1:56000/fynd_test",
        "sqlite:///fynd_test.db",
    ],
)
def test_guard_rejects_missing_or_non_isolated_database_urls(database_url):
    with pytest.raises(RuntimeError, match="Integration tests"):
        validate_database_target(database_url, None, "172.17.0.2")


def test_guard_rejects_engine_target_mismatch():
    configured = "postgresql://fynd_test_user:secret@127.0.0.1:56000/fynd_test"
    wrong_engine = make_url("postgresql://fynd:secret@127.0.0.1:5432/fynd")

    with pytest.raises(RuntimeError, match="SQLAlchemy engine target"):
        validate_database_target(configured, wrong_engine, "172.17.0.2")


@pytest.mark.parametrize("expected_address", [None, "", "not-an-ip"])
def test_guard_rejects_missing_or_invalid_expected_server_address(expected_address):
    configured = "postgresql://fynd_test_user:secret@127.0.0.1:56000/fynd_test"

    with pytest.raises(RuntimeError, match="expected .*server address"):
        validate_database_target(configured, None, expected_address)


def test_module_guard_rejects_missing_url_before_application_database_import(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FYND_TEST_PG_SERVER_ADDR", "172.17.0.2")

    with pytest.raises(RuntimeError, match="explicit isolated DATABASE_URL"):
        guard_isolated_postgres_integration_tests()


def test_guard_accepts_only_the_expected_test_target_and_canonicalizes_server_ip():
    configured = "postgresql://fynd_test_user:secret@127.0.0.1:56000/fynd_test"

    assert validate_database_target(configured, make_url(configured), "172.17.0.2") == (
        "172.17.0.2"
    )
    assert EXPECTED_DATABASE_TARGET == ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")


class _FakeConnection:
    def __init__(self, identity):
        self.identity = identity
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec_driver_sql(self, statement):
        assert statement == "BEGIN READ ONLY"

    def execute(self, _statement):
        return SimpleNamespace(one=lambda: self.identity)

    def rollback(self):
        self.rolled_back = True


class _FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def test_live_identity_check_uses_read_only_transaction_and_rolls_back():
    connection = _FakeConnection(("fynd_test", "fynd_test_user", "172.17.0.2", 5432, "on"))

    verify_live_database_identity(_FakeEngine(connection), "172.17.0.2")

    assert connection.rolled_back is True


def test_live_identity_accepts_host_cidr_form_of_expected_server_address():
    connection = _FakeConnection(("fynd_test", "fynd_test_user", "172.17.0.2/32", 5432, "on"))

    verify_live_database_identity(_FakeEngine(connection), "172.17.0.2")

    assert connection.rolled_back is True


def test_live_identity_rejects_a_different_server_address():
    connection = _FakeConnection(("fynd_test", "fynd_test_user", "172.17.0.3", 5432, "on"))

    with pytest.raises(RuntimeError, match="cannot verify isolated PostgreSQL identity"):
        verify_live_database_identity(_FakeEngine(connection), "172.17.0.2")

    assert connection.rolled_back is True


def test_live_identity_mismatch_fails_closed_and_rolls_back():
    connection = _FakeConnection(("fynd", "fynd", "127.0.0.1", 5432, "off"))

    with pytest.raises(RuntimeError, match="cannot verify isolated PostgreSQL identity"):
        verify_live_database_identity(_FakeEngine(connection), "172.17.0.2")

    assert connection.rolled_back is True
