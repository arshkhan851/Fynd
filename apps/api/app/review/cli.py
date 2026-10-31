import argparse
import json
import sys
from typing import Any

from app.database import SessionLocal
from app.review import (
    ReviewError,
    accept_evidence,
    create_category_proposal,
    get_competition_detail,
    list_pending_evidence,
    reject_evidence,
    unassign_category,
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
    proposal_parser = commands.add_parser(
        "propose-category", help="propose a canonical category for a competition"
    )
    proposal_parser.add_argument("competition_id", type=int)
    proposal_parser.add_argument("category_slug")
    proposal_parser.add_argument("--source-id", type=int, required=True)
    proposal_parser.add_argument(
        "--primary", action="store_true", help="propose this as the primary category"
    )
    unassign_parser = commands.add_parser(
        "unassign-category", help="remove an assigned category and supersede its approval"
    )
    unassign_parser.add_argument("competition_id", type=int)
    unassign_parser.add_argument("category_slug")
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
                _print_json(list_pending_evidence(session, args.limit, args.field))
            elif args.command == "show":
                _print_json(get_competition_detail(session, args.competition_id))
            elif args.command == "propose-category":
                evidence = create_category_proposal(
                    session,
                    args.competition_id,
                    args.category_slug,
                    args.source_id,
                    is_primary=args.primary,
                )
                _print_json(
                    {
                        "evidence_id": evidence.id,
                        "competition_id": evidence.competition_id,
                        "category_name": evidence.observed_value["name"],
                        "category_slug": evidence.field_key,
                        "is_primary": evidence.observed_value["is_primary"],
                        "review_state": evidence.review_state,
                    }
                )
            elif args.command == "unassign-category":
                unassign_category(session, args.competition_id, args.category_slug)
                _print_json(
                    {
                        "competition_id": args.competition_id,
                        "category_slug": args.category_slug,
                        "status": "unassigned",
                    }
                )
            elif args.command == "accept":
                evidence = accept_evidence(session, args.evidence_id)
                _print_json(
                    {
                        "evidence_id": evidence.id,
                        "competition_id": evidence.competition_id,
                        "field_name": evidence.field_name,
                        "category_name": evidence.observed_value.get("name")
                        if evidence.field_name == "category"
                        else None,
                        "category_slug": evidence.field_key
                        if evidence.field_name == "category"
                        else None,
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
                        "category_name": evidence.observed_value.get("name")
                        if evidence.field_name == "category"
                        else None,
                        "category_slug": evidence.field_key
                        if evidence.field_name == "category"
                        else None,
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
