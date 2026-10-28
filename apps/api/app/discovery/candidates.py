from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """Minimal listing-level information; it is not verified competition data."""

    title: str
    source_url: str
    publisher_name: str
    source_kind: str = "aggregator"

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise ValueError("candidate title must be a string")
        if not isinstance(self.source_url, str):
            raise ValueError("candidate source URL must be a string")
        if not isinstance(self.publisher_name, str):
            raise ValueError("publisher name must be a string")
        if not isinstance(self.source_kind, str):
            raise ValueError("source kind must be a string")
        if not self.title.strip():
            raise ValueError("candidate title must not be empty")
        if not self.publisher_name.strip():
            raise ValueError("publisher name must not be empty")
        if self.source_kind not in {"official", "organizer", "aggregator", "other"}:
            raise ValueError("unsupported discovery source kind")
