import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.discovery import DiscoveryCandidate, persist_candidates


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
            "Validate and persist discovery candidates from a JSON file. "
            "This command does not fetch websites or perform extraction."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="JSON array of candidates")
    parser.add_argument(
        "--allowed-host",
        action="append",
        required=True,
        help="Allowed source hostname or parent domain; repeat as needed",
    )
    args = parser.parse_args()

    try:
        candidates, skipped = _load_candidates(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        with SessionLocal() as session:
            summary = persist_candidates(session, candidates, set(args.allowed_host))
    except Exception as exc:
        print(f"Discovery persistence failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    result = {
        "discovered": summary.discovered,
        "skipped": skipped,
        "existing": summary.existing,
        "failed": summary.failed,
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
