"""Reusable discovery primitives; source adapters can be added independently."""

from app.discovery.candidates import DiscoveryCandidate
from app.discovery.extraction_persistence import persist_extraction_candidate
from app.discovery.persistence import DiscoverySummary, persist_candidates
from app.discovery.pipeline import run_authorized_extraction_pipeline

__all__ = [
    "DiscoveryCandidate",
    "DiscoverySummary",
    "persist_candidates",
    "persist_extraction_candidate",
    "run_authorized_extraction_pipeline",
]
