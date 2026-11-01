"""Shared vocabulary for evidence fields supported by human review."""

REVIEW_SUPPORTED_EVIDENCE_FIELDS = frozenset(
    {
        "title",
        "description",
        "eligibility_summary",
        "format",
        "registration_opens",
        "registration_deadline",
        "event_starts",
        "event_ends",
        "event_timezone",
        "official_url",
        "registration_url",
        "category",
    }
)
