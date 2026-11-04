"""Email normalization and validation shared by auth API and services."""

import re

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


def normalize_email(email: str) -> str:
    """Trim and case-normalize an email address without provider-specific rewriting."""
    normalized = email.strip().casefold()
    if len(normalized) > 255 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("Invalid email address")
    return normalized
