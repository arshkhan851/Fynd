"""Fail-closed target checks for PostgreSQL integration test modules."""

import ipaddress
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

EXPECTED_DATABASE_TARGET = ("127.0.0.1", 56000, "fynd_test", "fynd_test_user")
EXPECTED_INTERNAL_SERVER_PORT = 5432


def _normalize_actual_server_address(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        actual_ip = ipaddress.ip_address(value)
    except ValueError:
        interface = ipaddress.ip_interface(value)
        if interface.network.prefixlen != interface.ip.max_prefixlen:
            raise ValueError("server address CIDR must identify exactly one host") from None
        actual_ip = interface.ip
    return actual_ip.compressed


def validate_database_target(
    database_url: str | None,
    engine_url: URL | None,
    expected_server_address: str | None,
) -> str:
    """Validate configured URLs and return a canonical expected server IP."""
    if not database_url:
        raise RuntimeError("Integration tests require an explicit isolated DATABASE_URL")
    try:
        configured_url = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError("Integration tests require a valid isolated DATABASE_URL") from None

    if configured_url.get_backend_name() != "postgresql":
        raise RuntimeError("Integration tests require the isolated PostgreSQL target")
    configured_target = (
        configured_url.host,
        configured_url.port,
        configured_url.database,
        configured_url.username,
    )
    if configured_target != EXPECTED_DATABASE_TARGET:
        raise RuntimeError("Integration tests refuse a non-isolated database target")

    if engine_url is not None:
        engine_target = (
            engine_url.host,
            engine_url.port,
            engine_url.database,
            engine_url.username,
        )
        if (
            engine_url.get_backend_name() != "postgresql"
            or engine_target != EXPECTED_DATABASE_TARGET
        ):
            raise RuntimeError("Integration tests refuse a non-isolated SQLAlchemy engine target")

    if not expected_server_address:
        raise RuntimeError("Integration tests require the expected test PostgreSQL server address")
    try:
        return ipaddress.ip_address(expected_server_address).compressed
    except ValueError:
        raise RuntimeError(
            "Integration tests require a valid expected PostgreSQL server address"
        ) from None


def verify_live_database_identity(connectable: Any, expected_server_address: str) -> None:
    """Verify the server identity in a read-only transaction and always roll it back."""
    try:
        with connectable.connect() as connection:
            try:
                connection.exec_driver_sql("BEGIN READ ONLY")
                identity = connection.execute(
                    text(
                        "SELECT current_database(), current_user, inet_server_addr()::text, "
                        "inet_server_port(), current_setting('transaction_read_only')"
                    )
                ).one()
                # Accept host-sized CIDRs as their equivalent bare address.
                actual_address = _normalize_actual_server_address(identity[2])
                expected_identity = (
                    "fynd_test",
                    "fynd_test_user",
                    expected_server_address,
                    EXPECTED_INTERNAL_SERVER_PORT,
                    "on",
                )
                actual_identity = (*identity[:2], actual_address, *identity[3:])
                if actual_identity != expected_identity:
                    raise RuntimeError(
                        "PostgreSQL identity does not match the isolated test target"
                    )
            finally:
                connection.rollback()
    except Exception:
        raise RuntimeError("Integration tests cannot verify isolated PostgreSQL identity") from None


def guard_isolated_postgres_integration_tests() -> None:
    """Validate process configuration before importing and connecting the app engine."""
    database_url = os.environ.get("DATABASE_URL")
    expected_server_address = os.environ.get("FYND_TEST_PG_SERVER_ADDR")

    # Reject absent or wrong process configuration before app.database can use
    # its development defaults to construct the engine.
    canonical_server_address = validate_database_target(database_url, None, expected_server_address)

    from app.database import engine

    validate_database_target(database_url, engine.url, canonical_server_address)
    verify_live_database_identity(engine, canonical_server_address)
