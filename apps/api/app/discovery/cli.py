import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.discovery import DiscoveryCandidate, persist_candidates
from app.discovery.kaggle import KaggleDiscoveryAdapter, KaggleDiscoveryError


def _load_candidates(path: Path) -> tuple[list[DiscoveryCandidate], int]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("input JSON must be an array of candidate objects")
    candidates: list[DiscoveryCandidate] = []
    skipped = 0
    for item in payload:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            candidates.append(DiscoveryCandidate(**item))
        except (TypeError, ValueError):
            skipped += 1
    return candidates, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Persist candidates from JSON or Kaggle's official competition API. "
            "This command does not perform field extraction or verification."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", type=Path, help="JSON array of candidates")
    source_group.add_argument(
        "--kaggle", action="store_true", help="Fetch Kaggle competitions with its official client"
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        help="Allowed source hostname or parent domain for --input; repeat as needed",
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Maximum Kaggle candidates (default: 50)"
    )
    parser.add_argument("--page-size", type=int, default=20, help="Kaggle page size (max: 200)")
    args = parser.parse_args()

    source_skipped = 0
    allowed_hosts: set[str]
    try:
        if args.kaggle:
            if args.limit < 1 or not 1 <= args.page_size <= 200:
                parser.error("--limit must be positive and --page-size must be between 1 and 200")
            max_pages = (args.limit + args.page_size - 1) // args.page_size
            batch = KaggleDiscoveryAdapter(
                page_size=args.page_size,
                max_pages=max_pages,
                result_limit=args.limit,
            ).discover()
            candidates = list(batch.candidates)
            source_skipped = batch.skipped
            allowed_hosts = {"kaggle.com"}
        else:
            if not args.allowed_host:
                parser.error("--allowed-host is required with --input")
            candidates, source_skipped = _load_candidates(args.input)
            allowed_hosts = set(args.allowed_host)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    except KaggleDiscoveryError as exc:
        print(f"Kaggle discovery failed: {exc}", file=sys.stderr)
        return 1
    try:
        with SessionLocal() as session:
            summary = persist_candidates(session, candidates, allowed_hosts)
    except Exception as exc:
        print(f"Discovery persistence failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    result = {
        "discovered": summary.discovered,
        "skipped": source_skipped,
        "existing": summary.existing,
        "failed": summary.failed,
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
