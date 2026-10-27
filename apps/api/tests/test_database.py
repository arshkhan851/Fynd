from sqlalchemy.engine import URL

from app.database import get_database_url


def test_database_url_prefers_database_url_environment_variable(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://app:secret@db.example:5433/fynd_test"
    )

    url = get_database_url()

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "app"
    assert url.password == "secret"
    assert url.host == "db.example"
    assert url.port == 5433
    assert url.database == "fynd_test"


def test_database_url_builds_safely_from_postgres_environment(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "fynd user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:/word")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5544")
    monkeypatch.setenv("POSTGRES_DB", "fynd db")

    url = get_database_url()

    assert isinstance(url, URL)
    assert url.username == "fynd user"
    assert url.password == "p@ss:/word"
    assert url.host == "localhost"
    assert url.port == 5544
    assert url.database == "fynd db"
    assert "p%40ss%3A%2Fword" in url.render_as_string(hide_password=False)


def test_database_url_uses_documented_local_defaults(monkeypatch):
    for name in (
        "DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    url = get_database_url()

    assert url.render_as_string(hide_password=False) == (
        "postgresql+psycopg://fynd:fynd_local_dev_password@127.0.0.1:5432/fynd"
    )
