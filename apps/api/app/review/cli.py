import argparse
import json
import sys
from typing import Any

from app.database import SessionLocal
from app.review import (
    ReviewError,
    get_competition_detail,
    list_pending_evidence,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, default=str, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Review discovered competition field evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    pending_parser = commands.add_parser("list", help="list pending candidate evidence")
    pending_parser.add_argument("--limit", type=int, default=100)
    pending_parser.add_argument(
        "--field", choices=("category",), help="show only proposals for this evidence field"
    )
    show_parser = commands.add_parser("show", help="inspect a competition and its sources")
    show_parser.add_argument("competition_id", type=int)
    for name in ("propose-category", "unassign-category", "accept", "reject"):
        command_parser = commands.add_parser(
            name, help=f"disabled: use the future authenticated admin API ({name})"
        )
        command_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command not in {"list", "show"}:
        parser.error(
            "review mutations are disabled in this CLI; use the future authenticated admin API"
        )

    try:
        with SessionLocal() as session:
            if args.command == "list":
                _print_json(list_pending_evidence(session, args.limit, args.field))
            else:
                _print_json(get_competition_detail(session, args.competition_id))
    except (ReviewError, ValueError) as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Review failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
