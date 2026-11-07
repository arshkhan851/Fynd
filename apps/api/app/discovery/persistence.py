import logging
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.discovery.candidates import DiscoveryCandidate
from app.discovery.urls import normalize_source_url
from app.models import Competition, CompetitionSource, FieldEvidence, SourcePublisher

logger = logging.getLogger(__name__)


def _source_url_lock_key(normalized_url: str) -> int:
    """Map a normalized URL to a deterministic signed PostgreSQL bigint key.

    The first 64 bits of SHA-256 are used. A collision only makes unrelated URLs
    share a lock; the exact normalized URL query below remains the deduplication
    authority, so collisions cannot cause records to be treated as duplicates.
    """
    digest = sha256(normalized_url.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _acquire_source_url_lock(session: Session, normalized_url: str) -> None:
    lock_key = _source_url_lock_key(normalized_url)
    # Use the Session's transaction connection. Transaction-scoped advisory locks
    # survive candidate savepoint rollback and release only at outer commit/rollback.
    session.connection().execute(select(func.pg_advisory_xact_lock(lock_key)))


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    discovered: int = 0
    skipped: int = 0
    existing: int = 0
    failed: int = 0


def persist_candidates(
    session: Session,
    candidates: Iterable[DiscoveryCandidate],
    allowed_hosts: set[str],
) -> DiscoverySummary:
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError(
            "Discovery persistence requires PostgreSQL transaction-scoped advisory locks"
        )

    discovered = existing = failed = 0
    transaction = nullcontext() if session.in_transaction() else session.begin()
    with transaction:
        for candidate_index, candidate in enumerate(candidates, start=1):
            try:
                normalized = normalize_source_url(candidate.source_url, allowed_hosts)
                # Acquire before the global lookup and outside the candidate savepoint,
                # so rollback of one candidate cannot release the lock before the
                # caller's transaction finishes.
                _acquire_source_url_lock(session, normalized)
                with session.begin_nested():
                    seen = session.scalar(
                        select(CompetitionSource.id).where(
                            CompetitionSource.normalized_url == normalized
                        ).limit(1)
                    )
                    if seen is not None:
                        existing += 1
                        continue

                    publisher = session.scalar(
                        select(SourcePublisher)
                        .where(
                            func.lower(SourcePublisher.name)
                            == candidate.publisher_name.strip().lower()
                        )
                        .limit(1)
                    )
                    if publisher is None:
                        publisher = SourcePublisher(name=candidate.publisher_name.strip())
                        session.add(publisher)
                        session.flush()

                    # Discovery and manual-import records remain private even
                    # after evidence review. This persistence path does not
                    # make publication decisions.
                    competition = Competition(is_published=False)
                    session.add(competition)
                    session.flush()
                    source = CompetitionSource(
                        competition_id=competition.id,
                        publisher_id=publisher.id,
                        source_kind=candidate.source_kind,
                        source_url=candidate.source_url,
                        normalized_url=normalized,
                    )
                    session.add(source)
                    session.flush()
                    observed_value = {"title": candidate.title.strip()}
                    if candidate.external_id:
                        observed_value["source_external_id"] = candidate.external_id
                    session.add(
                        FieldEvidence(
                            competition_id=competition.id,
                            source_id=source.id,
                            field_name="title",
                            observed_value=observed_value,
                            evidence_excerpt=candidate.title.strip(),
                            review_state="candidate",
                        )
                    )
                    session.flush()
                    discovered += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Discovery candidate %d failed persistence (%s)",
                    candidate_index,
                    type(exc).__name__,
                )
    return DiscoverySummary(discovered=discovered, existing=existing, failed=failed)
