import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.discovery import DiscoveryCandidate, persist_candidates
from app.discovery.kaggle import KaggleDiscoveryAdapter, KaggleDiscoveryError
from app.discovery.registry import RegistryError, authorize_source_execution


def _load_candidates(path: Path) -> tuple[list[DiscoveryCandidate], int]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("input JSON must be an array of candidate objects")
    candidates: list[DiscoveryCandidate] = []
    skipped = 0
    for candidate_index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            candidate = DiscoveryCandidate(**item)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if candidate.source_kind == "organizer":
            raise ValueError(
                f"candidate {candidate_index} cannot claim source_kind='organizer'; "
                "use the organizer submission workflow"
            )
        candidates.append(candidate)
    return candidates, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and persist candidates from Kaggle's official competition API. "
            "External-source JSON persistence is currently disabled."
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
        help="URL safety allowlist for JSON input (does not grant source permission)",
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Maximum Kaggle candidates (default: 50)"
    )
    parser.add_argument("--page-size", type=int, default=20, help="Kaggle page size (max: 200)")
    args = parser.parse_args()

    if args.input is not None:
        parser.error(
            "external-source JSON imports are disabled until the authorization system "
            "can establish the required permissions"
        )

    source_skipped = 0
    registry_source_id: str | None = None
    allowed_hosts: set[str]
    try:
        if args.kaggle:
            if args.limit < 1 or not 1 <= args.page_size <= 200:
                parser.error("--limit must be positive and --page-size must be between 1 and 200")
            max_pages = (args.limit + args.page_size - 1) // args.page_size
            # Keep an explicit CLI boundary; the adapter repeats this guard for direct callers.
            authorize_source_execution("kaggle")
            batch = KaggleDiscoveryAdapter(
                page_size=args.page_size,
                max_pages=max_pages,
                result_limit=args.limit,
            ).discover()
            candidates = list(batch.candidates)
            registry_source_id = batch.registry_source_id
            source_skipped = batch.skipped
            allowed_hosts = {"kaggle.com"}
        else:
            if not args.allowed_host:
                parser.error("--allowed-host is required with --input")
            candidates, source_skipped = _load_candidates(args.input)
            allowed_hosts = set(args.allowed_host)
    except RegistryError as exc:
        print(f"Discovery authorization refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    except KaggleDiscoveryError as exc:
        print(f"Kaggle discovery failed: {exc}", file=sys.stderr)
        return 1
    try:
        with SessionLocal() as session:
            summary = persist_candidates(
                session,
                candidates,
                allowed_hosts,
                registry_source_id=registry_source_id,
            )
    except RegistryError as exc:
        print(f"Discovery authorization refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Discovery persistence failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    result = {
        "discovered": summary.discovered,
        "skipped": source_skipped,
        "skipped_observations": summary.skipped,
        "skipped_fields": list(summary.skipped_fields),
        "existing": summary.existing,
        "failed": summary.failed,
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
