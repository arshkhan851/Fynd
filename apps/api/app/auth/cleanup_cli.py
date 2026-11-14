"""Manually remove one bounded batch of expired authentication rate-limit counters."""

import argparse
import sys
from collections.abc import Sequence

_MAX_BATCH_SIZE = 500


def _load_runtime_dependencies():
    # These imports transitively initialize the database engine. Keep them inside
    # main's sanitized error boundary so configuration failures are not printed.
    from app.auth.rate_limit import cleanup_expired_rate_limit_counters
    from app.database import SessionLocal

    return SessionLocal, cleanup_expired_rate_limit_counters


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete one bounded batch of expired authentication rate-limit counters"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_MAX_BATCH_SIZE,
        help=(
            f"maximum rows to delete (1-{_MAX_BATCH_SIZE}; "
            f"default: {_MAX_BATCH_SIZE})"
        ),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= _MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {_MAX_BATCH_SIZE}")

    try:
        session_factory, cleanup = _load_runtime_dependencies()
        with session_factory() as session:
            deleted = cleanup(session, batch_size=args.batch_size)
    except Exception:
        # Exception text can contain SQL, parameters, or connection details.
        print("Rate-limit counter cleanup failed.", file=sys.stderr)
        return 1

    print(f"deleted={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
