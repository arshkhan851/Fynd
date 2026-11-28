"""End-to-end authorized discovery extraction entry point."""

from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.discovery.extraction_persistence import persist_extraction_candidate
from app.discovery.ollama import OllamaExtractionClient
from app.discovery.orchestration import AuthorizedSourceExtraction
from app.discovery.persistence import DiscoverySummary


def run_authorized_extraction_pipeline(
    session: Session,
    registry_source_id: str,
    source_url: str,
    *,
    ollama_client: OllamaExtractionClient,
    registry_path: str | Path | None = None,
    fetch_transport: httpx.BaseTransport | None = None,
) -> DiscoverySummary:
    """Fetch, validate, and persist one source as unpublished candidate evidence.

    Fetch authorization is checked by ``AuthorizedSourceExtraction``. Persistence then
    independently rechecks store and internal-review permissions through Task 6D.
    The caller owns the supplied Ollama client's lifecycle and the SQLAlchemy transaction.
    """
    extraction = AuthorizedSourceExtraction(
        ollama_client,
        registry_path=registry_path,
        transport=fetch_transport,
    ).extract(registry_source_id, source_url)
    return persist_extraction_candidate(session, extraction)
