import logging
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.discovery.candidates import DiscoveryCandidate
from app.discovery.registry import RegistryError, authorize_source_operation
from app.discovery.urls import UnsafeSourceUrl, normalize_source_url
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
    *,
    registry_source_id: str | None = None,
) -> DiscoverySummary:
    if not isinstance(registry_source_id, str) or not registry_source_id.strip():
        raise RegistryError("trusted registry source identity is required")
    if registry_source_id != registry_source_id.strip():
        raise RegistryError("trusted registry source identity is invalid")

    # Persistence is an independent authorization boundary: a successful fetch check
    # does not grant either storage or internal-review permission.
    store_source = authorize_source_operation(registry_source_id, "store")
    review_source = authorize_source_operation(registry_source_id, "internal_review")
    if store_source.id != registry_source_id or review_source.id != registry_source_id:
        raise RegistryError("source authorization resolved an unexpected registry identity")
    if store_source != review_source:
        raise RegistryError("source registry changed during persistence authorization")

    if not isinstance(allowed_hosts, set) or any(
        not isinstance(host, str) or not host.strip() for host in allowed_hosts
    ):
        raise ValueError("allowed_hosts must be a set of non-empty host strings")

    candidate_list = list(candidates)
    trusted_hosts = set(store_source.access.get("allowed_hosts", ()))
    if not trusted_hosts:
        raise RegistryError("authorized source has no registered URL hosts")
    for candidate in candidate_list:
        if not isinstance(candidate, DiscoveryCandidate):
            raise TypeError("persistence accepts DiscoveryCandidate instances only")
        if (
            candidate.publisher_name.strip().casefold()
            != store_source.publisher_name.strip().casefold()
            or candidate.source_kind != store_source.source_kind
        ):
            raise RegistryError(
                "candidate publisher or source kind does not match the trusted registry source"
            )
        try:
            # Registry hosts are authoritative. The caller's existing allowlist remains
            # an additional URL restriction in the per-candidate persistence path below.
            normalize_source_url(candidate.source_url, trusted_hosts)
        except UnsafeSourceUrl as exc:
            raise RegistryError(
                f"candidate source URL violates trusted registry host restrictions: {exc}"
            ) from exc

    if not candidate_list:
        return DiscoverySummary()

    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError(
            "Discovery persistence requires PostgreSQL transaction-scoped advisory locks"
        )

    discovered = existing = failed = 0
    transaction = nullcontext() if session.in_transaction() else session.begin()
    with transaction:
        for candidate_index, candidate in enumerate(candidate_list, start=1):
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
