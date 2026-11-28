"""Bounded execution of explicitly selected authorized discovery sources."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from app.discovery.ollama import OllamaExtractionClient
from app.discovery.persistence import DiscoverySummary
from app.discovery.pipeline import run_authorized_extraction_pipeline
from app.discovery.registry import (
    RegistryError,
    SourceRecord,
    authorize_source_execution,
)

MAX_SOURCES_PER_EXECUTION = 5

ExecutionStatus = Literal["succeeded", "existing", "unauthorized", "not_found", "failed"]


@dataclass(frozen=True, slots=True)
class SourceExecutionOutcome:
    source_id: str
    status: ExecutionStatus
    source_url: str | None = None
    persistence: DiscoverySummary | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryExecutionResult:
    outcomes: tuple[SourceExecutionOutcome, ...]


def _validate_source_ids(source_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Sequence):
        raise TypeError("source_ids must be an explicit sequence of source IDs")
    if not source_ids:
        raise ValueError("at least one source ID must be selected")
    if len(source_ids) > MAX_SOURCES_PER_EXECUTION:
        raise ValueError(
            f"at most {MAX_SOURCES_PER_EXECUTION} sources may be executed at once"
        )
    if any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids):
        raise ValueError("source IDs must be non-empty strings")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("duplicate source IDs are not allowed in one execution")
    return tuple(source_ids)


def _resolve_authorized_source(
    source_id: str, registry_path: str | Path | None
) -> tuple[SourceRecord | None, SourceExecutionOutcome | None]:
    try:
        authorized = authorize_source_execution(source_id, registry_path)
    except RegistryError as exc:
        message = str(exc)
        if "not registered" in message:
            status: ExecutionStatus = "not_found"
        elif f"source {source_id} fetch refused:" in message:
            status = "unauthorized"
        else:
            status = "failed"
        return None, SourceExecutionOutcome(source_id, status, reason=message)
    except Exception as exc:
        return None, SourceExecutionOutcome(
            source_id,
            "failed",
            reason=f"authorization check failed ({type(exc).__name__})",
        )

    if authorized.id != source_id:
        return None, SourceExecutionOutcome(
            source_id,
            "failed",
            reason="authorization resolved a different source identity",
        )
    return authorized, None


def execute_selected_sources(
    session: Session,
    source_ids: Sequence[str],
    *,
    ollama_client: OllamaExtractionClient,
    registry_path: str | Path | None = None,
    fetch_transport: httpx.BaseTransport | None = None,
) -> DiscoveryExecutionResult:
    """Execute one bounded batch of explicitly selected registry sources.

    Each source uses its registry homepage URL and is authorized before pipeline invocation;
    the pipeline rechecks fetch authorization and persistence rechecks store/review permission.
    A nested transaction isolates an unexpected source failure from later selections. The
    caller retains ownership of the outer transaction and must commit or roll it back.
    """
    selected_ids = _validate_source_ids(source_ids)
    outcomes: list[SourceExecutionOutcome] = []

    for source_id in selected_ids:
        source, authorization_failure = _resolve_authorized_source(source_id, registry_path)
        if authorization_failure is not None:
            outcomes.append(authorization_failure)
            continue

        assert source is not None
        try:
            with session.begin_nested():
                summary = run_authorized_extraction_pipeline(
                    session,
                    source_id,
                    source.homepage_url,
                    ollama_client=ollama_client,
                    registry_path=registry_path,
                    fetch_transport=fetch_transport,
                )
        except Exception as exc:
            outcomes.append(
                SourceExecutionOutcome(
                    source_id,
                    "failed",
                    source.homepage_url,
                    reason=f"execution failed ({type(exc).__name__}): {exc}",
                )
            )
            continue

        if summary.failed:
            status: ExecutionStatus = "failed"
            reason = "persistence reported a candidate failure"
        elif summary.discovered:
            status = "succeeded"
            reason = None
        elif summary.existing:
            status = "existing"
            reason = None
        else:
            status = "failed"
            reason = "execution produced no persisted or existing candidate"
        outcomes.append(
            SourceExecutionOutcome(
                source_id,
                status,
                source.homepage_url,
                persistence=summary,
                reason=reason,
            )
        )

    return DiscoveryExecutionResult(tuple(outcomes))
