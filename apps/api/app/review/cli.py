import argparse
import json
import sys
from typing import Any

from app.database import SessionLocal
from app.review import (
    ReviewError,
    accept_evidence,
    get_competition_detail,
    list_pending_evidence,
    reject_evidence,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, default=str, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Review discovered competition field evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    pending_parser = commands.add_parser("list", help="list pending candidate evidence")
    pending_parser.add_argument("--limit", type=int, default=100)
    show_parser = commands.add_parser("show", help="inspect a competition and its sources")
    show_parser.add_argument("competition_id", type=int)
    for name, help_text in (
        ("accept", "accept an evidence record and apply its supported canonical value"),
        ("reject", "reject a candidate evidence record"),
    ):
        command_parser = commands.add_parser(name, help=help_text)
        command_parser.add_argument("evidence_id", type=int)
    args = parser.parse_args()

    try:
        with SessionLocal() as session:
            if args.command == "list":
                _print_json(list_pending_evidence(session, args.limit))
            elif args.command == "show":
                _print_json(get_competition_detail(session, args.competition_id))
            elif args.command == "accept":
                evidence = accept_evidence(session, args.evidence_id)
                _print_json(
                    {
                        "evidence_id": evidence.id,
                        "competition_id": evidence.competition_id,
                        "field_name": evidence.field_name,
                        "review_state": evidence.review_state,
                        "reviewed_at": evidence.reviewed_at,
                    }
                )
            else:
                evidence = reject_evidence(session, args.evidence_id)
                _print_json(
                    {
                        "evidence_id": evidence.id,
                        "competition_id": evidence.competition_id,
                        "field_name": evidence.field_name,
                        "review_state": evidence.review_state,
                        "reviewed_at": evidence.reviewed_at,
                    }
                )
    except (ReviewError, ValueError) as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Review failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
