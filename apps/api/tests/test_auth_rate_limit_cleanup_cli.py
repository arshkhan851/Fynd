import pytest

from app.auth import cleanup_cli


class _SessionContext:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True
        return False


@pytest.mark.parametrize("batch_size", [1, 250, 500])
def test_cli_accepts_valid_batch_size_and_calls_cleanup_once(
    monkeypatch, capsys, batch_size
):
    session = _SessionContext()
    calls = []
    monkeypatch.setattr(
        cleanup_cli,
        "_load_runtime_dependencies",
        lambda: (
            lambda: session,
            lambda actual_session, *, batch_size: calls.append(
                (actual_session, batch_size)
            )
            or 3,
        ),
    )

    assert cleanup_cli.main(["--batch-size", str(batch_size)]) == 0

    assert calls == [(session, batch_size)]
    assert session.closed
    captured = capsys.readouterr()
    assert captured.out == "deleted=3\n"
    assert captured.err == ""


def test_cli_defaults_to_one_batch_of_500(monkeypatch, capsys):
    session = _SessionContext()
    calls = []
    monkeypatch.setattr(
        cleanup_cli,
        "_load_runtime_dependencies",
        lambda: (
            lambda: session,
            lambda actual_session, *, batch_size: calls.append(
                (actual_session, batch_size)
            )
            or 0,
        ),
    )

    assert cleanup_cli.main([]) == 0

    assert calls == [(session, 500)]
    assert session.closed
    assert capsys.readouterr().out == "deleted=0\n"


@pytest.mark.parametrize(
    "arguments",
    [["--batch-size", "0"], ["--batch-size", "501"], ["--batch-size", "bad"]],
)
def test_cli_rejects_invalid_batch_size_before_database_work(
    monkeypatch, arguments
):
    monkeypatch.setattr(
        cleanup_cli,
        "_load_runtime_dependencies",
        lambda: pytest.fail("invalid arguments must not initialize database dependencies"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cleanup_cli.main(arguments)
    assert exc_info.value.code == 2


def test_cli_sanitizes_cleanup_failure_and_closes_session(monkeypatch, capsys):
    session = _SessionContext()

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db raw-email@example.org")

    monkeypatch.setattr(
        cleanup_cli,
        "_load_runtime_dependencies",
        lambda: (lambda: session, fail),
    )

    assert cleanup_cli.main([]) == 1

    assert session.closed
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Rate-limit counter cleanup failed.\n"
    assert "secret" not in captured.err
    assert "raw-email@example.org" not in captured.err


def test_cli_sanitizes_database_module_initialization_failure(monkeypatch, capsys):
    def fail_initialization():
        raise RuntimeError("database URL contains password=secret")

    monkeypatch.setattr(cleanup_cli, "_load_runtime_dependencies", fail_initialization)

    assert cleanup_cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Rate-limit counter cleanup failed.\n"
    assert "secret" not in captured.err
