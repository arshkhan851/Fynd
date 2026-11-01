"""Fail-closed parser and static authorization gate for discovery sources."""

from __future__ import annotations

import ast
import importlib.util
import math
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS

SOURCE_KINDS = frozenset({"official", "organizer", "aggregator", "other"})
SOURCE_STATUSES = frozenset(
    {"proposed", "auditing", "approved", "restricted", "prohibited", "blocked", "retired"}
)
ACCESS_METHODS = frozenset(
    {
        "official_api",
        "official_client",
        "feed",
        "sitemap",
        "structured_html",
        "html",
        "manual_submission",
    }
)
ROBOTS_RESULTS = frozenset({"allowed", "disallowed", "no_file", "not_applicable", "unknown"})
TERMS_DECISIONS = frozenset({"allowed", "permission_required", "prohibited", "unclear", "unknown"})
GRANT_STATUSES = frozenset({"none", "requested", "granted", "denied", "expired", "revoked"})
TRIGGERS = frozenset({"manual", "scheduled"})
FREQUENCIES = ("monthly", "weekly", "daily", "hourly")
WEB_METHODS = frozenset({"feed", "sitemap", "structured_html", "html"})
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SECRET_KEY_RE = re.compile(r"(?:token|secret|password|api[_-]?key)", re.IGNORECASE)
_TOKEN_QUERY_RE = re.compile(r"(?:token|secret|password|api[_-]?key|authorization)", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+\S+|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|\b[A-Za-z0-9_-]{40,}\b)",
    re.IGNORECASE,
)
_GRANT_KEYS = frozenset({"field", "trigger", "frequency"})
_RETRYABLE_STATUSES = frozenset({429, 503})
_CLIENT_MAX_RETRIES = 2
_CLIENT_MAX_RETRY_AFTER_S = 30
_RE_AUDIT_PERIOD = timedelta(days=90)
_AUDIT_NOTE_NAME_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<revision>[2-9]|[1-9]\d+))?\.md$"
)
_AUDIT_HEADINGS = {
    1: "fields, triggers and frequency needed ([source.use])",
    2: "official channels found",
    3: "robots.txt (date, result, relevant rules, crawl delay)",
    4: "terms reviewed (url, date, decision, paraphrase)",
    5: (
        "permission grant (status, scope as key:value entries, expiry, reference; "
        "correspondence stored off-repo? yes/no)"
    ),
    6: (
        "change of authorization or terms since last audit "
        "(previous decision, new decision, document or clarification, date)"
    ),
    7: "technical notes",
    8: (
        "rate-limit and error behavior (rate-limit statuses, retry-after seen, real 403 examples, "
        "auth_failure_403_markers)"
    ),
    9: "sample of 10 items (differences found)",
    10: "fit for fynd (relevant share)",
    11: "attribution required",
    12: "contact url check (url, date, result)",
    13: "limits chosen",
    14: "open questions / permission requests sent",
    15: "decision and next audit due",
}


class RegistryError(ValueError):
    """The registry is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    name: str
    publisher_name: str
    homepage_url: str
    source_kind: str
    status: str
    status_reason: str
    adapter: str
    access: dict[str, Any]
    use: dict[str, Any]
    permission: dict[str, Any]
    limits: dict[str, Any]
    attribution: dict[str, Any]
    audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    version: int
    contact_url: str
    contact_url_checked_on: str
    user_agent: str
    sources: tuple[SourceRecord, ...]
    path: Path

    def get_source(self, source_id: str) -> SourceRecord | None:
        return next((source for source in self.sources if source.id == source_id), None)


def _require_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be a table")
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing keys: {', '.join(sorted(missing))}")
        if extra:
            detail.append(f"unknown keys: {', '.join(sorted(extra))}")
        raise RegistryError(f"{label}: {'; '.join(detail)}")
    return value


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise RegistryError(f"{label} must be {'a string' if empty else 'a non-empty string'}")
    return value


def _date(value: Any, label: str, *, empty: bool = True) -> date | None:
    raw = _string(value, label, empty=empty)
    if not raw and empty:
        return None
    if not _DATE_RE.fullmatch(raw):
        raise RegistryError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise RegistryError(f"{label} must be a valid ISO date") from exc
    return parsed


def _https_url(value: Any, label: str, *, empty: bool = False) -> str:
    url = _string(value, label, empty=empty)
    if not url and empty:
        return url
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RegistryError(f"{label} must be an absolute HTTPS URL")
    hostname = parsed.hostname
    normalized_hostname = hostname.rstrip(".").lower()
    if hostname != hostname.strip() or not _HOST_RE.fullmatch(normalized_hostname):
        raise RegistryError(f"{label} must contain a valid hostname")
    if parsed.username is not None or parsed.password is not None:
        raise RegistryError(f"{label} must not contain URL credentials")
    if any(
        _TOKEN_QUERY_RE.search(key) or _TOKEN_QUERY_RE.search(val)
        for key, val in parse_qsl(parsed.query)
    ):
        raise RegistryError(f"{label} must not contain token-like query parameters")
    return url


def _validate_host(host: Any, label: str) -> str:
    if (
        not isinstance(host, str)
        or not host
        or host != host.lower()
        or not _HOST_RE.fullmatch(host)
    ):
        raise RegistryError(
            f"{label} must be an exact lowercase hostname without wildcard or URL parts"
        )
    return host


def _validate_url_value(value: Any, label: str) -> None:
    """Apply the same offline URL checks used by registry loading."""
    parsed = urlsplit(_https_url(value, label))
    host = parsed.hostname or ""
    if _is_placeholder_host(host):
        raise RegistryError(f"{label} must not use a reserved placeholder host")


def _scan_secrets(value: Any, path: str = "registry") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY_RE.search(str(key)) and str(key) not in {"auth_env_var"}:
                raise RegistryError(f"{path} contains a prohibited secret-named key")
            _scan_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise RegistryError(f"{path} contains a secret-like value")


def _scope(scope: Any, source_id: str) -> tuple[frozenset[str], frozenset[str], str | None]:
    if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
        raise RegistryError(f"source {source_id} grant scope must be an array of strings")
    if not scope:
        return frozenset(), frozenset(), None
    fields: set[str] = set()
    triggers: set[str] = set()
    frequencies: list[str] = []
    seen: set[str] = set()
    for entry in scope:
        if entry in seen:
            raise RegistryError(f"source {source_id} grant scope contains duplicate entries")
        seen.add(entry)
        if entry != entry.lower() or " " in entry or "*" in entry:
            raise RegistryError(
                f"source {source_id} grant scope entries must be lowercase exact key:value values"
            )
        if entry.count(":") != 1:
            raise RegistryError(f"source {source_id} has malformed grant scope entry")
        key, value = entry.split(":", 1)
        if key not in _GRANT_KEYS:
            raise RegistryError(f"source {source_id} grant scope has unknown key '{key}'")
        if key == "field":
            if value not in REVIEW_SUPPORTED_EVIDENCE_FIELDS:
                raise RegistryError(f"source {source_id} grant scope has unknown field '{value}'")
            fields.add(value)
        elif key == "trigger":
            if value not in TRIGGERS or value in triggers:
                raise RegistryError(
                    f"source {source_id} grant scope has invalid or duplicate trigger"
                )
            triggers.add(value)
        else:
            if value not in FREQUENCIES or frequencies:
                raise RegistryError(
                    f"source {source_id} grant scope needs exactly one valid frequency"
                )
            frequencies.append(value)
    if not fields or not triggers or len(frequencies) != 1:
        raise RegistryError(
            f"source {source_id} grant scope needs fields, triggers and exactly one frequency"
        )
    return frozenset(fields), frozenset(triggers), frequencies[0]


def _validate_grant_coverage(
    source_id: str,
    use_fields: list[str],
    use_triggers: list[str],
    use_frequency: str,
    grant_fields: frozenset[str],
    grant_triggers: frozenset[str],
    grant_frequency: str | None,
) -> None:
    if not set(use_fields).issubset(grant_fields):
        raise RegistryError(f"source {source_id} grant does not cover all declared fields")
    if not set(use_triggers).issubset(grant_triggers):
        raise RegistryError(f"source {source_id} grant does not cover all declared triggers")
    if grant_frequency is None:
        raise RegistryError(f"source {source_id} grant has no complete scope")
    if FREQUENCIES.index(use_frequency) > FREQUENCIES.index(grant_frequency):
        raise RegistryError(f"source {source_id} declared frequency is faster than grant")


def _validate_source(raw: Any, root: Path, today: date) -> SourceRecord:
    base = _require_keys(
        raw,
        {
            "id",
            "name",
            "publisher_name",
            "homepage_url",
            "source_kind",
            "status",
            "status_reason",
            "adapter",
            "access",
            "use",
            "permission",
            "limits",
            "attribution",
            "audit",
        },
        "source",
    )
    source_id = _string(base["id"], "source.id")
    if not _ID_RE.fullmatch(source_id):
        raise RegistryError(
            f"source id '{source_id}' must contain lowercase letters, digits and hyphens"
        )
    for key in ("name", "publisher_name"):
        _string(base[key], f"source.{key}")
    _https_url(base["homepage_url"], f"source {source_id} homepage_url")
    kind = _string(base["source_kind"], f"source {source_id} source_kind")
    if kind not in SOURCE_KINDS:
        raise RegistryError(f"source {source_id} has unknown source_kind '{kind}'")
    status = _string(base["status"], f"source {source_id} status")
    if status not in SOURCE_STATUSES:
        raise RegistryError(f"source {source_id} has unknown status '{status}'")
    status_reason = _string(base["status_reason"], f"source {source_id} status_reason", empty=True)
    if status in {"restricted", "prohibited", "blocked", "retired"} and not status_reason.strip():
        raise RegistryError(f"source {source_id} status_reason is required for status {status}")
    adapter = _string(base["adapter"], f"source {source_id} adapter")
    access = _require_keys(
        base["access"],
        {"method", "allowed_hosts", "auth_env_var", "auth_failure_403_markers"},
        f"source {source_id} access",
    )
    method = _string(access["method"], f"source {source_id} method")
    if method not in ACCESS_METHODS:
        raise RegistryError(f"source {source_id} has unknown access method '{method}'")
    hosts = access["allowed_hosts"]
    if not isinstance(hosts, list):
        raise RegistryError(f"source {source_id} allowed_hosts must be an array")
    normalized_hosts = [_validate_host(host, f"source {source_id} allowed_hosts") for host in hosts]
    if len(set(normalized_hosts)) != len(normalized_hosts):
        raise RegistryError(f"source {source_id} allowed_hosts contains duplicates")
    auth_name = _string(access["auth_env_var"], f"source {source_id} auth_env_var", empty=True)
    if auth_name and not _ENV_RE.fullmatch(auth_name):
        raise RegistryError(f"source {source_id} auth_env_var must be an environment variable name")
    markers = access["auth_failure_403_markers"]
    if not isinstance(markers, list) or any(
        not isinstance(item, str) or not item or item != item.lower() for item in markers
    ):
        raise RegistryError(
            f"source {source_id} auth_failure_403_markers must contain lowercase non-empty strings"
        )
    if markers and not auth_name:
        raise RegistryError(f"source {source_id} 403 markers require auth_env_var")

    use = _require_keys(base["use"], {"fields", "triggers", "frequency"}, f"source {source_id} use")
    fields = use["fields"]
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(item, str) for item in fields)
    ):
        raise RegistryError(f"source {source_id} use.fields must be a non-empty array of strings")
    if len(set(fields)) != len(fields):
        raise RegistryError(f"source {source_id} use.fields contains duplicates")
    unknown_fields = set(fields) - REVIEW_SUPPORTED_EVIDENCE_FIELDS
    if unknown_fields:
        raise RegistryError(
            "source "
            f"{source_id} use.fields contains unsupported fields: "
            f"{', '.join(sorted(unknown_fields))}"
        )
    triggers = use["triggers"]
    if (
        not isinstance(triggers, list)
        or not triggers
        or any(item not in TRIGGERS for item in triggers)
    ):
        raise RegistryError(f"source {source_id} use.triggers must contain manual and/or scheduled")
    if len(set(triggers)) != len(triggers):
        raise RegistryError(f"source {source_id} use.triggers contains duplicates")
    frequency = use["frequency"]
    if frequency not in FREQUENCIES:
        raise RegistryError(f"source {source_id} has unknown frequency '{frequency}'")

    permission = _require_keys(
        base["permission"],
        {
            "robots_checked_on",
            "robots_result",
            "terms_url",
            "terms_reviewed_on",
            "terms_decision",
            "terms_notes",
            "previous_terms_decision",
            "terms_change_reference",
            "grant",
        },
        f"source {source_id} permission",
    )
    robots_result = _string(permission["robots_result"], f"source {source_id} robots_result")
    terms_decision = _string(permission["terms_decision"], f"source {source_id} terms_decision")
    if robots_result not in ROBOTS_RESULTS:
        raise RegistryError(f"source {source_id} has unknown robots_result '{robots_result}'")
    if terms_decision not in TERMS_DECISIONS:
        raise RegistryError(f"source {source_id} has unknown terms_decision '{terms_decision}'")
    _date(permission["robots_checked_on"], f"source {source_id} robots_checked_on")
    _https_url(permission["terms_url"], f"source {source_id} terms_url", empty=True)
    _date(permission["terms_reviewed_on"], f"source {source_id} terms_reviewed_on")
    _string(permission["terms_notes"], f"source {source_id} terms_notes", empty=True)
    previous_terms = permission["previous_terms_decision"]
    if previous_terms not in {"", "prohibited"}:
        raise RegistryError(
            f"source {source_id} previous_terms_decision may only record prohibited"
        )
    change_ref = _string(
        permission["terms_change_reference"],
        f"source {source_id} terms_change_reference",
        empty=True,
    )
    if change_ref and previous_terms != "prohibited":
        raise RegistryError(
            f"source {source_id} terms_change_reference requires previous_terms_decision prohibited"
        )
    grant = _require_keys(
        permission["grant"],
        {
            "status",
            "requested_on",
            "granted_on",
            "expires_on",
            "granted_by_role",
            "scope",
            "conditions",
            "reference",
            "evidence_stored_offrepo",
        },
        f"source {source_id} grant",
    )
    grant_status = _string(grant["status"], f"source {source_id} grant.status")
    if grant_status not in GRANT_STATUSES:
        raise RegistryError(f"source {source_id} has unknown grant status '{grant_status}'")
    grant_dates = {
        key: _date(grant[key], f"source {source_id} grant.{key}")
        for key in ("requested_on", "granted_on", "expires_on")
    }
    for key in ("granted_by_role", "conditions", "reference"):
        _string(grant[key], f"source {source_id} grant.{key}", empty=True)
    if not isinstance(grant["evidence_stored_offrepo"], bool):
        raise RegistryError(f"source {source_id} evidence_stored_offrepo must be boolean")
    grant_fields, grant_triggers, grant_frequency = _scope(grant["scope"], source_id)
    if grant_status == "granted":
        _validate_grant_coverage(
            source_id,
            fields,
            triggers,
            frequency,
            grant_fields,
            grant_triggers,
            grant_frequency,
        )

    if terms_decision == "prohibited" and status not in {"prohibited", "retired"}:
        raise RegistryError(
            f"source {source_id}: prohibited terms require prohibited or retired status"
        )
    if status == "prohibited" and terms_decision != "prohibited":
        raise RegistryError(f"source {source_id}: prohibited status requires prohibited terms")
    if robots_result == "disallowed" and status not in {"blocked", "retired"}:
        raise RegistryError(
            f"source {source_id}: disallowed robots requires blocked or retired status"
        )
    if terms_decision == "prohibited" and grant_status == "granted":
        raise RegistryError(f"source {source_id}: a grant cannot override prohibited terms")
    if previous_terms == "prohibited" and terms_decision not in {
        "allowed",
        "permission_required",
    }:
        raise RegistryError(
            f"source {source_id}: prohibited terms can only be lifted to allowed or "
            "permission_required"
        )

    limits = _require_keys(
        base["limits"],
        {
            "min_request_interval_s",
            "max_items_per_run",
            "max_response_bytes",
            "max_retries",
            "max_retry_after_s",
            "rate_limit_statuses",
        },
        f"source {source_id} limits",
    )
    for key in (
        "min_request_interval_s",
        "max_items_per_run",
        "max_response_bytes",
        "max_retries",
        "max_retry_after_s",
    ):
        value = limits[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise RegistryError(f"source {source_id} limit {key} must be positive")
    for key in ("max_items_per_run", "max_response_bytes", "max_retries", "max_retry_after_s"):
        if not isinstance(limits[key], int) or isinstance(limits[key], bool):
            raise RegistryError(f"source {source_id} limit {key} must be an integer")
    if not isinstance(limits["max_retries"], int) or limits["max_retries"] > _CLIENT_MAX_RETRIES:
        raise RegistryError(f"source {source_id} max_retries exceeds the client maximum")
    if limits["max_retry_after_s"] > _CLIENT_MAX_RETRY_AFTER_S:
        raise RegistryError(f"source {source_id} max_retry_after_s exceeds the client maximum")
    statuses = limits["rate_limit_statuses"]
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(type(item) is not int or item not in _RETRYABLE_STATUSES for item in statuses)
    ):
        raise RegistryError(f"source {source_id} rate_limit_statuses may contain only 429 and 503")
    if len(set(statuses)) != len(statuses):
        raise RegistryError(f"source {source_id} rate_limit_statuses contains duplicates")

    attribution = _require_keys(
        base["attribution"],
        {"display_name", "link_required", "required_text"},
        f"source {source_id} attribution",
    )
    _string(attribution["display_name"], f"source {source_id} attribution.display_name")
    if not isinstance(attribution["link_required"], bool):
        raise RegistryError(f"source {source_id} attribution.link_required must be boolean")
    _string(
        attribution["required_text"], f"source {source_id} attribution.required_text", empty=True
    )
    audit = _require_keys(
        base["audit"],
        {"last_audit_on", "next_audit_due", "auditor", "latest_note", "approved_on", "approved_by"},
        f"source {source_id} audit",
    )
    for key in ("auditor", "latest_note", "approved_by"):
        audit[key] = _string(
            audit[key], f"source {source_id} audit.{key}", empty=True
        ).strip()
    audit_date = _date(audit["last_audit_on"], f"source {source_id} audit.last_audit_on")
    due_date = _date(audit["next_audit_due"], f"source {source_id} audit.next_audit_due")
    approved_date = _date(audit["approved_on"], f"source {source_id} audit.approved_on")
    for label, parsed in (
        ("robots_checked_on", _date(permission["robots_checked_on"], "robots_checked_on")),
        ("terms_reviewed_on", _date(permission["terms_reviewed_on"], "terms_reviewed_on")),
        ("last_audit_on", audit_date),
        ("approved_on", approved_date),
        *[(f"grant.{key}", val) for key, val in grant_dates.items()],
    ):
        if parsed and parsed > today and label not in {"grant.expires_on"}:
            raise RegistryError(f"source {source_id} {label} cannot be in the future")
    note_path: Path | None = None
    if audit_date is not None or audit["latest_note"]:
        if audit_date is None or not audit["latest_note"]:
            raise RegistryError(
                f"source {source_id} latest_note and last_audit_on must be set together"
            )
        note_path = _validate_latest_note(root, source_id, audit_date, audit["latest_note"])
    if (audit_date is None) != (due_date is None):
        raise RegistryError(
            f"source {source_id} last_audit_on and next_audit_due must be set together"
        )
    if audit_date is not None and due_date is not None:
        if due_date > audit_date + _RE_AUDIT_PERIOD:
            raise RegistryError(
                f"source {source_id} next_audit_due exceeds the 90-day re-audit period"
            )
    if previous_terms == "prohibited":
        if status != "auditing":
            raise RegistryError(
                f"source {source_id}: prohibited terms must return to auditing before approval"
            )
        if not change_ref:
            raise RegistryError(
                f"source {source_id}: leaving prohibited terms requires terms_change_reference"
            )
        if audit_date is None or note_path is None:
            raise RegistryError(
                f"source {source_id}: prohibited-terms change requires a newer dated audit note"
            )
    if status == "approved" and (terms_decision in {"unclear", "unknown"}):
        raise RegistryError(f"source {source_id}: unclear or unknown terms cannot be approved")
    emitted_kind = _adapter_source_kind(adapter)
    if emitted_kind is not None and emitted_kind != kind:
        raise RegistryError(
            f"source {source_id}: source_kind does not match adapter candidate kind"
        )
    if grant_status == "granted":
        if grant_dates["expires_on"] is None:
            raise RegistryError(
                f"source {source_id}: granted permission requires an explicit expiry date"
            )
        if grant_dates["expires_on"] < today:
            raise RegistryError(f"source {source_id}: grant is expired")
    if grant_status == "granted" and terms_decision == "permission_required":
        if (
            not grant["evidence_stored_offrepo"]
            or not grant["reference"]
            or not grant["granted_by_role"]
            or grant_dates["granted_on"] is None
        ):
            raise RegistryError(
                f"source {source_id}: granted permission requires dated off-repository "
                "evidence and grant reference"
            )
    if grant_status in {"expired", "revoked"} and status == "approved":
        raise RegistryError(f"source {source_id}: expired or revoked grant cannot be approved")
    return SourceRecord(
        source_id,
        base["name"],
        base["publisher_name"],
        base["homepage_url"],
        kind,
        status,
        status_reason,
        adapter,
        access,
        use,
        permission,
        limits,
        attribution,
        audit,
    )


def _audit_note_path(root: Path, source_id: str, value: Any) -> Path:
    relative = _string(value, f"source {source_id} audit.latest_note")
    expected_prefix = Path("docs") / "sources" / "audits" / source_id
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.parent != expected_prefix:
        raise RegistryError(f"source {source_id} latest_note must be under {expected_prefix}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-\d+)?\.md", path.name):
        raise RegistryError(
            f"source {source_id} latest_note filename must be a dated Markdown note"
        )
    return root / path


def _audit_note_date(path: Path) -> tuple[date, int]:
    match = _AUDIT_NOTE_NAME_RE.fullmatch(path.name)
    if match is None:
        raise RegistryError(f"audit note {path} does not have a dated Markdown filename")
    try:
        note_date = date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise RegistryError(f"audit note {path} has an invalid filename date") from exc
    return note_date, int(match.group("revision") or 1)


def _source_audit_notes(root: Path, source_id: str) -> list[Path]:
    directory = root / "docs" / "sources" / "audits" / source_id
    if not directory.is_dir():
        return []
    notes = [
        path
        for path in directory.iterdir()
        if path.is_file() and _AUDIT_NOTE_NAME_RE.fullmatch(path.name)
    ]
    return sorted(notes, key=_audit_note_date)


def _validate_latest_note(root: Path, source_id: str, audit_date: date, latest_note: str) -> Path:
    note_path = _audit_note_path(root, source_id, latest_note)
    if not note_path.is_file():
        raise RegistryError(f"source {source_id} audit note does not exist: {latest_note}")
    note_date, _ = _audit_note_date(note_path)
    if note_date != audit_date:
        raise RegistryError(f"source {source_id} audit note filename date must match last_audit_on")
    notes = _source_audit_notes(root, source_id)
    if not notes or note_path != notes[-1]:
        raise RegistryError(
            f"source {source_id} latest_note must point to the newest dated audit note"
        )
    return note_path


def _validate_prohibited_lift(
    root: Path,
    source: SourceRecord,
    registry: SourceRegistry,
) -> None:
    source_id = source.id
    audit_date = _date(source.audit["last_audit_on"], f"source {source_id} audit.last_audit_on")
    latest_note = _validate_latest_note(
        root, source_id, audit_date, source.audit["latest_note"]
    ) if audit_date is not None else None
    new_decision = source.permission["terms_decision"]
    change_reference = source.permission["terms_change_reference"]
    if audit_date is None or latest_note is None:
        raise RegistryError(f"source {source_id} prohibited lift requires a dated latest note")
    if new_decision not in {"allowed", "permission_required"}:
        raise RegistryError(f"source {source_id} prohibited lift has an invalid new decision")
    notes = _source_audit_notes(root, source_id)
    prior_notes = [
        path
        for path in notes
        if path != latest_note and _audit_note_date(path)[0] < audit_date
    ]
    if not prior_notes:
        raise RegistryError(
            f"source {source_id} prohibited lift requires a prior prohibited audit note"
        )
    prior_note = prior_notes[-1]
    prior_date, _ = _audit_note_date(prior_note)
    prior_text = prior_note.read_text(encoding="utf-8")
    status_match = _unique_metadata_value(prior_text, "status after audit")
    prior_sections, _, _ = _validate_audit_note_structure(
        source_id=source_id,
        source_kind=source.source_kind,
        audit_date=prior_date,
        note_path=prior_note,
        expected_status="prohibited",
    )
    decision_body = prior_sections.get(15, ("", ""))[1]
    prior_decision = _decision_section_values(decision_body)
    prior_terms = _section_items(prior_sections[4][1])
    if (
        status_match is None
        or status_match.lower() != "prohibited"
        or prior_decision.get("decision") != "prohibited"
        or prior_decision.get("auditor") is None
        or prior_decision.get("next due") is None
        or len(prior_terms) < 3
        or prior_terms[2] != "prohibited"
    ):
        raise RegistryError(
            f"source {source_id} prior audit note must identify this source, match its date, "
            "and record an actual prohibited decision"
        )

    # Validate the complete current audit note while status is still auditing. This
    # checks structure and audit evidence without invoking the approval gate.
    _require_audit_sections(source, registry, root)
    current_sections = _audit_sections(latest_note.read_text(encoding="utf-8").lower())
    heading, body = current_sections.get(6, ("", ""))
    if heading != _AUDIT_HEADINGS[6]:
        raise RegistryError(f"source {source_id} audit note lacks the authorization change section")
    change_values = _labelled_section_values(body)
    new_decision_value = change_values.get("new decision")
    evidence_value = change_values.get("formal change evidence", "")
    valid_formal_evidence = {
        "formal clarification was issued",
        "formal clarification was received",
        "formal agreement was signed",
        "formal agreement was received",
        "formal authorization was issued",
        "formal authorization was received",
        "new terms were published",
        "updated terms were published",
        "official terms were updated",
        "official policy was updated",
    }
    if (
        change_values.get("previous decision") != "prohibited"
        or new_decision_value != new_decision
        or change_values.get("change reference") != change_reference.lower()
        or change_values.get("change date") != audit_date.isoformat()
        or evidence_value not in valid_formal_evidence
    ):
        raise RegistryError(
            f"source {source_id} audit note must document prior and new decisions, date, "
            "reference, and affirmative formal change evidence"
        )


def _labelled_section_values(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in body.split(";"):
        if ":" not in item:
            continue
        label, value = item.split(":", 1)
        normalized_label = label.strip().lower()
        if normalized_label in values:
            raise RegistryError(
                f"audit note contains duplicate evidence label '{normalized_label}'"
            )
        values[normalized_label] = value.strip().lower()
    return values


def _unique_metadata_value(note: str, label: str, *, header: bool = False) -> str | None:
    """Read one metadata line, rejecting duplicates and malformed variants."""
    if header:
        pattern = re.compile(r"(?im)^\s*#\s*source\s+audit\b\s*:?(.*)$")
        exact_pattern = re.compile(r"(?i)^\s*#\s*source\s+audit:\s*(.+?)\s*$")
    else:
        escaped = re.escape(label).replace(r"\ ", r"\s+")
        pattern = re.compile(rf"(?im)^\s*{escaped}\b\s*:?(.*)$")
        exact_pattern = re.compile(rf"(?i)^\s*{escaped}\s*:\s*(.+?)\s*$")
    candidates = [line for line in note.splitlines() if pattern.fullmatch(line)]
    if len(candidates) > 1:
        raise RegistryError(f"audit note contains duplicate '{label}' metadata")
    if not candidates:
        return None
    match = exact_pattern.fullmatch(candidates[0])
    if match is None:
        raise RegistryError(f"audit note contains malformed '{label}' metadata")
    return match.group(1).strip()


def _decision_section_values(body: str) -> dict[str, str]:
    """Parse the complete section 15 decision line and reject extra/conflicting data."""
    values: dict[str, str] = {}
    for index, item in enumerate(body.split(";")):
        item = item.strip().lower()
        if index == 0:
            if not item:
                raise RegistryError("audit note decision section is empty")
            values["decision"] = item
            continue
        if "=" not in item:
            raise RegistryError("audit note decision section contains malformed evidence")
        label, value = item.split("=", 1)
        label = label.strip()
        if label in values:
            raise RegistryError(f"audit note contains duplicate decision label '{label}'")
        if label not in {"auditor", "next due"} or not value.strip():
            raise RegistryError("audit note decision section contains unknown or empty evidence")
        values[label] = value.strip()
    return values


def _reject_conflicting_labelled_evidence(
    section: str, expected: dict[str, str], *, section_name: str
) -> None:
    """Reject repeated or contradictory explicit labels in required audit sections."""
    values = _labelled_section_values(section)
    for label, expected_value in expected.items():
        if label in values and values[label] != expected_value.lower():
            raise RegistryError(
                f"audit note {section_name} section has contradictory '{label}' evidence"
            )


def _section_items(section: str) -> list[str]:
    return [item.strip() for item in section.split(";")]


def _validate_terms_section(
    section: str,
    *,
    expected_url: str | None,
    expected_date: str | None,
    expected_decision: str,
    expected_notes: str | None,
    audit_date: date | None = None,
) -> None:
    items = _section_items(section)
    if len(items) < 4:
        raise RegistryError(
            "audit note terms section must record URL, date, decision and paraphrase"
        )
    if expected_url is not None and items[0] != expected_url.lower():
        raise RegistryError("audit note terms section URL is inconsistent")
    if expected_url is None:
        _https_url(items[0], "audit note terms URL")
    parsed_date = _date(items[1], "audit note terms review date", empty=False)
    if expected_date is not None and items[1] != expected_date.lower():
        raise RegistryError("audit note terms section date is inconsistent")
    if parsed_date is None:
        raise RegistryError("audit note terms section date is missing")
    if audit_date is not None and parsed_date > audit_date:
        raise RegistryError("audit note terms review date is later than the audit")
    if items[2] != expected_decision.lower():
        raise RegistryError("audit note terms section has contradictory decision evidence")
    paraphrase = "; ".join(items[3:])
    if not paraphrase or (expected_notes and expected_notes.lower() not in paraphrase):
        raise RegistryError("audit note terms section lacks its configured paraphrase")
    decision_conflicts = {
        "allowed": (
            r"\b(?:prohibit(?:ed|s|ing)?|forbid(?:den|s)?|disallow(?:ed|s)?|unclear|unknown|"
            r"permission required|not\s+(?:allowed|permitted|authorize)|"
            r"(?:do|does|did)\s+not\s+(?:allow|permit|authorize)|"
            r"cannot\s+(?:allow|permit|authorize)|unlawful|illegal|not\s+authorized)\b"
        ),
        "permission_required": (
            r"\b(?:prohibit(?:ed|s|ing)?|forbid(?:den|s)?|disallow(?:ed|s)?|unclear|unknown|"
            r"permit(?:ted)? without permission|allowed without permission|"
            r"permission is not required|no permission required)\b"
        ),
        "prohibited": r"\b(?:allowed|permits?|permitted|unclear|unknown|not\s+prohibited)\b",
        "unclear": r"\b(?:allowed|prohibited|permission required|unknown)\b",
        "unknown": r"\b(?:allowed|prohibited|permission required|unclear)\b",
    }
    if re.search(decision_conflicts[expected_decision], paraphrase, re.IGNORECASE):
        raise RegistryError("audit note terms section contains contradictory prose")


def _validate_robots_section(
    section: str,
    *,
    expected_result: str,
    expected_date: str | None,
    audit_date: date | None = None,
) -> None:
    items = _section_items(section)
    if expected_result == "not_applicable":
        if not items or not any(
            marker in items[0] for marker in ("not applicable", "n/a", "api terms govern")
        ):
            raise RegistryError("audit note robots section lacks a not-applicable decision")
    else:
        if len(items) < 2 or items[1] != expected_result.lower():
            raise RegistryError("audit note robots section has contradictory result evidence")
        parsed_date = _date(items[0], "audit note robots check date", empty=False)
        if expected_date is not None and items[0] != expected_date.lower():
            raise RegistryError("audit note robots section date is inconsistent")
        if parsed_date is None:
            raise RegistryError("audit note robots section date is missing")
        if audit_date is not None and parsed_date > audit_date:
            raise RegistryError("audit note robots check date is later than the audit")
    _reject_conflicting_labelled_evidence(
        section, {"robots result": expected_result}, section_name="robots review"
    )
    conflicts = {
        "allowed": (
            r"\b(?:robots(?:\.txt)?\s+(?:disallow\w*|deny|block\w*)|"
            r"(?:our|the)\s+(?:requested|relevant)\s+path\s+(?:is\s+)?"
            r"(?:disallowed|blocked)|not\s+allowed)\b"
        ),
        "no_file": (
            r"\b(?:robots(?:\.txt)?\s+(?:disallow\w*|deny|block\w*)|"
            r"(?:our|the)\s+(?:requested|relevant)\s+path\s+(?:is\s+)?"
            r"(?:disallowed|blocked))\b"
        ),
        "not_applicable": (
            r"\b(?:robots(?:\.txt)?\s+(?:allowed|disallowed|disallow\w*|denied|blocked)|"
            r"(?:our|the)\s+(?:requested|relevant)\s+path\s+(?:is\s+)?"
            r"(?:allowed|disallowed|blocked))\b"
        ),
    }
    full_text = "; ".join(items)
    if re.search(conflicts.get(expected_result, r"(?!)"), full_text, re.IGNORECASE):
        raise RegistryError("audit note robots section contains contradictory prose")
    if expected_result == "not_applicable" and re.search(
        r"\b(?:disallow\w*|den(?:y|ied)|block\w*|not\s+allowed)\b", full_text, re.IGNORECASE
    ):
        raise RegistryError("audit note robots section contains contradictory prose")


def _validate_grant_section(section: str, *, expected_status: str | None) -> str:
    items = _section_items(section)
    if not items or items[0] not in GRANT_STATUSES:
        raise RegistryError("audit note grant section must start with one grant status")
    status = items[0]
    if expected_status is not None and status != expected_status.lower():
        raise RegistryError("audit note grant section has contradictory status evidence")
    remainder = "; ".join(items[1:])
    _reject_conflicting_labelled_evidence(
        section,
        {"grant status": status},
        section_name="permission grant",
    )
    if re.search(
        r"\b(?:withdrawn|revoked|expired|denied|rejected|not\s+granted|not\s+approved)\b",
        remainder,
        re.IGNORECASE,
    ):
        raise RegistryError("audit note grant section contains contradictory status prose")
    for other_status in GRANT_STATUSES - {status}:
        if re.search(rf"\b{re.escape(other_status)}\b", remainder, re.IGNORECASE):
            raise RegistryError("audit note grant section contains contradictory status prose")
    return status


def _validate_contact_section(
    section: str,
    *,
    expected_url: str | None,
    expected_date: str | None,
    audit_date: date | None = None,
) -> None:
    items = _section_items(section)
    if len(items) < 3:
        raise RegistryError("audit note contact section must record URL, date and result")
    if expected_url is not None and items[0] != expected_url.lower():
        raise RegistryError("audit note contact section URL is inconsistent")
    if expected_url is None:
        _https_url(items[0], "audit note contact URL")
    contact_date = _date(items[1], "audit note contact check date", empty=False)
    if expected_date is not None and items[1] != expected_date.lower():
        raise RegistryError("audit note contact section date is inconsistent")
    if contact_date is None:
        raise RegistryError("audit note contact section date is missing")
    if audit_date is not None and contact_date > audit_date:
        raise RegistryError("audit note contact check date is later than the audit")
    result = "; ".join(items[2:])
    if not any(
        marker in result
        for marker in ("confirmed", "reachable", "loads over https", "identifies the project")
    ):
        raise RegistryError("audit note contact section lacks an affirmative result")
    if re.search(
        r"\b(?:unreachable|not reachable|could not reach|failed to load|not confirmed|"
        r"unavailable|refused|404|does not load|cannot reach)\b",
        result,
    ):
        raise RegistryError("audit note contact section contains contradictory result prose")


def _validate_audit_note_structure(
    *,
    source_id: str,
    source_kind: str,
    audit_date: date,
    note_path: Path,
    expected_status: str,
    expected_auditor: str | None = None,
    expected_due: str | None = None,
) -> tuple[dict[int, tuple[str, str]], str, str]:
    """Validate required note structure and unique metadata independently of approval."""
    if not note_path.is_file():
        raise RegistryError(f"source {source_id} audit note does not exist: {note_path}")
    if _audit_note_date(note_path)[0] != audit_date:
        raise RegistryError(
            f"source {source_id} audit note filename date must match its audit date"
        )
    note = note_path.read_text(encoding="utf-8")
    sections = _audit_sections(note)
    missing = [
        f"{number}. {heading}"
        for number, heading in _AUDIT_HEADINGS.items()
        if number not in sections
        or sections[number][0] != heading
        or not sections[number][1]
    ]
    if missing:
        raise RegistryError(
            f"source {source_id} audit note is missing required sections: {', '.join(missing)}"
        )
    if _unique_metadata_value(note, "source audit", header=True) != (
        f"{source_id} ({audit_date.isoformat()})"
    ):
        raise RegistryError(f"source {source_id} audit note header must identify source and date")
    status = _unique_metadata_value(note, "status after audit")
    decision = _unique_metadata_value(note, "decision")
    auditor = _unique_metadata_value(note, "auditor")
    kind = _unique_metadata_value(note, "source kind")
    if status != expected_status or (decision is not None and decision != expected_status):
        raise RegistryError(
            f"source {source_id} audit note status/decision metadata is contradictory"
        )
    if not auditor or (expected_auditor is not None and auditor != expected_auditor):
        raise RegistryError(
            f"source {source_id} audit note auditor metadata is missing or contradictory"
        )
    if kind != source_kind:
        raise RegistryError(
            f"source {source_id} audit note source-kind metadata is missing or contradictory"
        )
    decision_values = _decision_section_values(sections[15][1])
    due = decision_values.get("next due", "")
    due_date = _date(due, f"source {source_id} audit-note next due", empty=False)
    if (
        decision_values.get("decision") != expected_status
        or decision_values.get("auditor") != auditor.lower()
        or due_date is None
        or due_date <= audit_date
        or due_date > audit_date + _RE_AUDIT_PERIOD
        or (expected_due is not None and due != expected_due.lower())
    ):
        raise RegistryError(f"source {source_id} audit note decision evidence is contradictory")
    terms_items = _section_items(sections[4][1])
    if len(terms_items) < 3 or terms_items[2] not in TERMS_DECISIONS:
        raise RegistryError("audit note terms section does not establish one decision")
    _validate_terms_section(
        sections[4][1],
        expected_url=None,
        expected_date=None,
        expected_decision=terms_items[2],
        expected_notes=None,
        audit_date=audit_date,
    )
    robots_items = _section_items(sections[3][1])
    if robots_items and any(
        marker in robots_items[0] for marker in ("not applicable", "n/a", "api terms govern")
    ):
        robots_result = "not_applicable"
    elif len(robots_items) > 1 and robots_items[1] in ROBOTS_RESULTS:
        robots_result = robots_items[1]
    else:
        raise RegistryError("audit note robots section does not establish one result")
    _validate_robots_section(
        sections[3][1], expected_result=robots_result, expected_date=None, audit_date=audit_date
    )
    _validate_grant_section(sections[5][1], expected_status=None)
    _validate_contact_section(
        sections[12][1], expected_url=None, expected_date=None, audit_date=audit_date
    )
    return sections, auditor, due


def _audit_sections(note: str) -> dict[int, tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^\s*(\d+)\.\s+([^\r\n]*)", note))
    sections: dict[int, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        if number in sections:
            raise RegistryError(f"audit note contains duplicate section number {number}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(note)
        heading_line = match.group(2).strip()
        expected_heading = _AUDIT_HEADINGS.get(number)
        expected_prefix = f"{expected_heading}:" if expected_heading else None
        if expected_prefix and heading_line.casefold().startswith(expected_prefix.casefold()):
            heading = expected_heading
            inline = heading_line[len(expected_prefix) :].strip()
        elif ": " in heading_line:
            heading, inline = heading_line.split(": ", 1)
        else:
            heading, inline = heading_line, ""
        body = "\n".join(part for part in (inline, note[match.end() : end]) if part).strip()
        sections[number] = (heading.strip().lower(), body.strip().lower())
    return sections


def _require_audit_sections(source: SourceRecord, registry: SourceRegistry, root: Path) -> None:
    audit_date = _date(source.audit["last_audit_on"], f"source {source.id} audit.last_audit_on")
    if audit_date is None:
        raise RegistryError(f"source {source.id} requires a dated audit note")
    note_path = _validate_latest_note(root, source.id, audit_date, source.audit["latest_note"])
    sections, _, _ = _validate_audit_note_structure(
        source_id=source.id,
        source_kind=source.source_kind,
        audit_date=audit_date,
        note_path=note_path,
        expected_status=source.status,
        expected_auditor=source.audit["auditor"],
        expected_due=source.audit["next_audit_due"],
    )
    used = sections[1][1]
    if any(field not in used for field in source.use["fields"]):
        raise RegistryError(f"source {source.id} audit note does not list all declared fields")
    if any(trigger not in used for trigger in source.use["triggers"]):
        raise RegistryError(f"source {source.id} audit note does not list all declared triggers")
    if source.use["frequency"] not in used:
        raise RegistryError(f"source {source.id} audit note does not list declared frequency")

    permission = source.permission
    terms_review = sections[4][1]
    _validate_terms_section(
        terms_review,
        expected_url=permission["terms_url"],
        expected_date=permission["terms_reviewed_on"],
        expected_decision=permission["terms_decision"],
        expected_notes=permission["terms_notes"],
        audit_date=audit_date,
    )
    _reject_conflicting_labelled_evidence(
        terms_review,
        {"terms decision": permission["terms_decision"]},
        section_name="terms review",
    )
    terms_date = permission["terms_reviewed_on"]
    parsed_terms_date = _date(terms_date, f"source {source.id} terms_reviewed_on")
    if (
        not permission["terms_url"]
        or not terms_date
        or parsed_terms_date is None
        or parsed_terms_date > audit_date
        or not permission["terms_notes"].strip()
        or permission["terms_url"].lower() not in terms_review
        or terms_date not in terms_review
        or permission["terms_decision"] not in terms_review
        or permission["terms_notes"].lower() not in terms_review
    ):
        raise RegistryError(
            f"source {source.id} audit note must record the terms URL, review date, "
            "decision and paraphrase"
        )

    robots_review = sections[3][1]
    _validate_robots_section(
        robots_review,
        expected_result=permission["robots_result"],
        expected_date=permission["robots_checked_on"] or None,
        audit_date=audit_date,
    )
    _reject_conflicting_labelled_evidence(
        robots_review,
        {"robots result": permission["robots_result"]},
        section_name="robots review",
    )
    if source.access["method"] in WEB_METHODS:
        parsed_robots_date = _date(
            permission["robots_checked_on"], f"source {source.id} robots_checked_on"
        )
        if (
            not permission["robots_checked_on"]
            or parsed_robots_date is None
            or parsed_robots_date > audit_date
            or permission["robots_result"] not in {"allowed", "no_file"}
            or permission["robots_checked_on"] not in robots_review
            or permission["robots_result"] not in robots_review
        ):
            raise RegistryError(
                f"source {source.id} audit note must record the permitted robots result "
                "and check date"
            )
    elif permission["robots_result"] == "not_applicable":
        if not any(
            marker in robots_review
            for marker in ("not applicable", "n/a", "api terms govern")
        ):
            raise RegistryError(
                f"source {source.id} audit note must explain why robots review is not applicable"
            )
    elif permission["robots_result"] in {"allowed", "no_file"}:
        parsed_robots_date = _date(
            permission["robots_checked_on"], f"source {source.id} robots_checked_on"
        )
        if (
            parsed_robots_date is None
            or parsed_robots_date > audit_date
            or permission["robots_checked_on"] not in robots_review
            or permission["robots_result"] not in robots_review
        ):
            raise RegistryError(
                f"source {source.id} audit note must record the robots result and check date"
            )

    grant_review = sections[5][1]
    grant = permission["grant"]
    _validate_grant_section(grant_review, expected_status=grant["status"])
    _reject_conflicting_labelled_evidence(
        grant_review,
        {"grant status": grant["status"]},
        section_name="permission grant",
    )
    if (
        grant["status"] not in grant_review
        or any(scope_entry.lower() not in grant_review for scope_entry in grant["scope"])
        or (grant["expires_on"] and grant["expires_on"] not in grant_review)
        or (grant["reference"] and grant["reference"].lower() not in grant_review)
    ):
        raise RegistryError(
            f"source {source.id} audit note must record the grant status, scope, expiry "
            "and reference"
        )

    if (
        source.id and source.id.lower() not in sections[2][1]
        and source.publisher_name.lower() not in sections[2][1]
    ):
        raise RegistryError(
            f"source {source.id} audit note must identify the official channel reviewed"
        )
    contact_check = sections[12][1]
    _validate_contact_section(
        contact_check,
        expected_url=registry.contact_url,
        expected_date=registry.contact_url_checked_on,
        audit_date=audit_date,
    )
    contact_checked = _date(
        registry.contact_url_checked_on, "registry.contact_url_checked_on"
    )
    if (
        contact_checked is None
        or contact_checked > audit_date
        or registry.contact_url_checked_on not in contact_check
        or registry.contact_url.lower() not in contact_check
        or not any(
            marker in contact_check
            for marker in ("confirmed", "reachable", "loads over https", "identifies the project")
        )
    ):
        raise RegistryError(
            f"source {source.id} audit note must record the contact URL and dated check result"
        )
    numeric_values = (
        str(source.limits["min_request_interval_s"]),
        str(source.limits["max_items_per_run"]),
        str(source.limits["max_response_bytes"]),
        str(source.limits["max_retries"]),
        str(source.limits["max_retry_after_s"]),
    )
    if any(value not in sections[13][1] for value in numeric_values):
        raise RegistryError(
            f"source {source.id} audit note must record all configured numeric limits"
        )
    decision = sections[15][1]
    if source.status not in decision or source.audit["next_audit_due"] not in decision:
        raise RegistryError(
            f"source {source.id} audit note must record the current decision and next audit date"
        )
    if source.audit["auditor"].lower() not in decision:
        raise RegistryError(f"source {source.id} audit note must identify the auditor")
    if source.attribution["display_name"].lower() not in sections[11][1]:
        raise RegistryError(f"source {source.id} audit note must record attribution requirements")


def _permission_establishes_numeric_limits(source: SourceRecord) -> bool:
    """Always false for v1: frozen scopes authorize only fields, triggers, and frequency.

    Numeric permission caps must be represented by a future schema field. Parsing `conditions`
    would turn human prose into authorization, which the frozen specification forbids.
    """
    return False


def _adapter_source_kind(adapter: str) -> str | None:
    try:
        spec = importlib.util.find_spec(adapter)
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is None or spec.origin is None:
        raise RegistryError(f"adapter module '{adapter}' does not exist")
    try:
        source = Path(spec.origin).read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"adapter module '{adapter}' cannot be inspected") from exc
    try:
        tree = ast.parse(source, filename=spec.origin)
    except SyntaxError as exc:
        raise RegistryError(f"adapter module '{adapter}' cannot be parsed") from exc
    emitted_kinds: set[str] = set()
    found_candidate = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if function_name != "DiscoveryCandidate":
            continue
        found_candidate = True
        keyword = next((item for item in node.keywords if item.arg == "source_kind"), None)
        if keyword is None or not isinstance(keyword.value, ast.Constant):
            raise RegistryError(
                f"adapter module '{adapter}' candidate source_kind cannot be verified"
            )
        if not isinstance(keyword.value.value, str):
            raise RegistryError(
                f"adapter module '{adapter}' candidate source_kind must be a string"
            )
        emitted_kinds.add(keyword.value.value)
    if not found_candidate or len(emitted_kinds) != 1:
        raise RegistryError(f"adapter module '{adapter}' must emit one consistent source_kind")
    return next(iter(emitted_kinds))


def authorization_decision(
    registry: SourceRegistry,
    source: SourceRecord,
    *,
    today: date | None = None,
    environ: Any = None,
) -> AuthorizationDecision:
    """Return a fail-closed decision and actionable refusal reasons."""
    today = today or date.today()
    environ = os.environ if environ is None else environ
    reasons: list[str] = []
    # Direct callers must receive the same structural checks as records loaded from TOML.
    try:
        if not source.access["allowed_hosts"] or any(
            _validate_host(host, f"source {source.id} allowed host") is None
            for host in source.access["allowed_hosts"]
        ):
            reasons.append("at least one valid allowed host is required")
        _validate_url_value(source.homepage_url, "source homepage URL")
        _validate_url_value(permission_url := source.permission["terms_url"], "source terms URL")
        if not permission_url:
            reasons.append("source terms URL is required")
    except (KeyError, TypeError, RegistryError) as exc:
        reasons.append(str(exc) or "source schema validation failed")
    audit = source.audit
    permission = source.permission
    grant = permission["grant"]
    if source.status != "approved":
        reasons.append(f"source status is {source.status}, not approved")
    approved_by = audit.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        reasons.append("approval date and approver are required")
    try:
        approved_on = _date(audit.get("approved_on"), "audit.approved_on", empty=False)
        if approved_on is not None and approved_on > today:
            reasons.append("approval date cannot be in the future")
    except RegistryError:
        reasons.append("a valid approval date is required")
    audit_date = _date(audit["last_audit_on"], "audit.last_audit_on")
    if audit_date is None or not audit["latest_note"]:
        reasons.append("a dated audit note is required")
    due = _date(audit["next_audit_due"], "audit.next_audit_due")
    if due is None or due <= today:
        reasons.append("next audit due date must be in the future")
    terms = permission["terms_decision"]
    if terms == "permission_required":
        if grant["status"] != "granted":
            reasons.append("permission grant is not granted")
        else:
            expiry = _date(grant["expires_on"], "grant.expires_on")
            if expiry is None:
                reasons.append("permission grant expiry date is required")
            elif expiry < today:
                reasons.append("permission grant has expired")
            try:
                grant_fields, grant_triggers, grant_frequency = _scope(grant["scope"], source.id)
                _validate_grant_coverage(
                    source.id,
                    source.use["fields"],
                    source.use["triggers"],
                    source.use["frequency"],
                    grant_fields,
                    grant_triggers,
                    grant_frequency,
                )
            except RegistryError as exc:
                reasons.append(str(exc))
    elif terms != "allowed":
        reasons.append(f"terms decision '{terms}' does not authorize use")
    method = source.access["method"]
    robots = permission["robots_result"]
    if method in WEB_METHODS and robots not in {"allowed", "no_file"}:
        reasons.append(f"robots result '{robots}' does not permit this web access method")
    elif method in {"official_api", "official_client"} and robots not in {
        "not_applicable",
        "allowed",
        "no_file",
    }:
        reasons.append(f"robots result '{robots}' is not acceptable for this API/client method")
    if not source.access["allowed_hosts"]:
        reasons.append("at least one allowed host is required")
    if any(_is_placeholder_host(host) for host in source.access["allowed_hosts"]):
        reasons.append("allowed hosts must not use reserved placeholder domains")
    for label, value in (
        ("homepage URL", source.homepage_url),
        ("terms URL", permission["terms_url"]),
    ):
        host = urlsplit(value).hostname if value else None
        if host and _is_placeholder_host(host):
            reasons.append(f"{label} must not use a reserved placeholder host")
    if (
        not source.use["fields"]
        or not source.use["triggers"]
        or source.use["frequency"] not in FREQUENCIES
    ):
        reasons.append("declared fields, triggers and frequency are required")
    if registry.contact_url.startswith("https://") is False or _is_placeholder_host(
        urlsplit(registry.contact_url).hostname or ""
    ):
        reasons.append("contact URL must be HTTPS and use a real, non-placeholder host")
    elif registry.contact_url not in registry.user_agent:
        reasons.append("User-Agent must include the contact URL")
    checked = _date(registry.contact_url_checked_on, "registry.contact_url_checked_on")
    if checked is None or checked > today or checked < today - timedelta(days=90):
        reasons.append("contact URL check must be dated within the last 90 days")
    auth_name = source.access["auth_env_var"]
    if auth_name and auth_name not in environ:
        reasons.append(f"required environment variable {auth_name} is not set")
    if not _permission_establishes_numeric_limits(source):
        reasons.append(
            "approval refused: v1 schema has no verifiable numeric permission limits; "
            "source terms or grant must establish these before approval"
        )
    try:
        _require_audit_sections(source, registry, registry.path.parent.parent.parent)
    except (OSError, RegistryError) as exc:
        reasons.append(str(exc))
    try:
        _adapter_source_kind(source.adapter)
    except RegistryError as exc:
        reasons.append(str(exc))
    return AuthorizationDecision(not reasons, tuple(reasons))


def authorize_source_execution(
    source_id: str,
    registry_path: str | Path | None = None,
    *,
    today: date | None = None,
    environ: Any = None,
) -> SourceRecord:
    """Load and authorize a source before an adapter authenticates or connects.

    This is deliberately fail-closed. Runtime trigger, frequency, and reservation checks are
    not implemented in Step 2, so no live execution is enabled by this helper.
    """
    if registry_path is None:
        registry_path = Path(__file__).resolve().parents[4] / "docs" / "sources" / "registry.toml"
    registry = load_registry(registry_path, today=today)
    source = registry.get_source(source_id)
    if source is None:
        raise RegistryError(f"source {source_id} is not registered")
    decision = authorization_decision(registry, source, today=today, environ=environ)
    if not decision.allowed:
        raise RegistryError(
            f"source {source_id} execution refused: {'; '.join(decision.reasons)}"
        )
    return source


def _is_placeholder_host(host: str) -> bool:
    lowered = host.rstrip(".").lower()
    placeholder_hosts = {
        "example.com",
        "example.org",
        "example.net",
        "localhost",
    }
    reserved_roots = ("example.com", "example.org", "example.net")
    return (
        lowered in placeholder_hosts
        or lowered.endswith((".example", ".test", ".invalid", ".localhost"))
        or lowered in {"example", "test", "invalid"}
        or any(lowered.endswith(f".{domain}") for domain in reserved_roots)
    )


def _load_registry(path: str | Path, *, today: date | None = None) -> SourceRegistry:
    path = Path(path).resolve()
    root = path.parent.parent.parent
    today = today or date.today()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError("cannot load registry file") from exc
    _scan_secrets(data)
    if not isinstance(data, dict) or data.keys() - {"registry", "source"} or "registry" not in data:
        raise RegistryError("registry file must contain only registry metadata and source entries")
    meta = _require_keys(
        data["registry"],
        {"version", "contact_url", "contact_url_checked_on", "user_agent"},
        "registry",
    )
    if type(meta["version"]) is not int or meta["version"] != 1:
        raise RegistryError("unsupported registry version")
    contact_url = _https_url(meta["contact_url"], "registry.contact_url")
    checked_on = _string(
        meta["contact_url_checked_on"], "registry.contact_url_checked_on", empty=True
    )
    checked_date = _date(checked_on, "registry.contact_url_checked_on")
    if checked_date and checked_date > today:
        raise RegistryError("registry.contact_url_checked_on cannot be in the future")
    user_agent = _string(meta["user_agent"], "registry.user_agent")
    raw_sources = data.get("source", [])
    if not isinstance(raw_sources, list):
        raise RegistryError("source must be an array of tables")
    records = tuple(_validate_source(item, root, today) for item in raw_sources)
    ids = [record.id for record in records]
    if len(set(ids)) != len(ids):
        raise RegistryError("source ids must be unique")
    registry = SourceRegistry(1, contact_url, checked_on, user_agent, records, path)
    for source in records:
        if source.permission["previous_terms_decision"] == "prohibited":
            _validate_prohibited_lift(root, source, registry)
        if source.status == "approved":
            decision = authorization_decision(registry, source, today=today)
            if not decision.allowed:
                raise RegistryError(
                    f"approved source {source.id} fails authorization gate: "
                    f"{'; '.join(decision.reasons)}"
                )
    return registry


def load_registry(path: str | Path, *, today: date | None = None) -> SourceRegistry:
    """Load and validate TOML registry. Approved entries fail loading unless runnable."""
    try:
        return _load_registry(path, today=today)
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError("registry validation failed closed due to malformed data") from exc
