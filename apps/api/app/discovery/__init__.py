"""Reusable discovery primitives; source adapters can be added independently."""

from app.discovery.candidates import DiscoveryCandidate
from app.discovery.persistence import DiscoverySummary, persist_candidates

__all__ = ["DiscoveryCandidate", "DiscoverySummary", "persist_candidates"]
