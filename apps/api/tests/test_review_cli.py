import sys

import pytest

from app.review import cli


@pytest.mark.parametrize("command", ["list", "show"])
def test_read_only_commands_remain_available(monkeypatch, command, capsys):
    class SessionFactory:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli, "SessionLocal", SessionFactory)
    monkeypatch.setattr(cli, "list_pending_evidence", lambda *_args: [{"evidence_id": 1}])
    monkeypatch.setattr(cli, "get_competition_detail", lambda *_args: {"competition": {"id": 1}})
    argv = ["fynd-review", "show", "1"] if command == "show" else ["fynd-review", "list"]
    monkeypatch.setattr(sys, "argv", argv)

    assert cli.main() == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize(
    "command,args",
    [
        ("accept", ["1"]),
        ("reject", ["1"]),
        ("propose-category", ["1", "test", "--source-id", "1"]),
        ("unassign-category", ["1", "test"]),
    ],
)
def test_mutation_commands_are_disabled_with_admin_api_direction(
    monkeypatch, capsys, command, args
):
    monkeypatch.setattr(sys, "argv", ["fynd-review", command, *args])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "future authenticated admin API" in capsys.readouterr().err
