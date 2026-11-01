from __future__ import annotations

import ast
import tomllib
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.discovery.registry import (
    RegistryError,
    SourceRegistry,
    _is_placeholder_host,
    _require_audit_sections,
    _validate_contact_section,
    _validate_grant_section,
    _validate_robots_section,
    _validate_terms_section,
    authorization_decision,
    load_registry,
)
from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS

TODAY = date(2026, 11, 1)


@pytest.mark.parametrize(
    ("validator", "section", "kwargs"),
    [
        (
            _validate_terms_section,
            "https://terms.example/terms; 2026-10-01; allowed; "
            "Reviewed official terms permit collection; collection is unlawful under these terms",
            {
                "expected_url": None,
                "expected_date": None,
                "expected_decision": "allowed",
                "expected_notes": "Reviewed official terms permit collection",
            },
        ),
        (
            _validate_robots_section,
            "not applicable because robots.txt disallows the relevant path",
            {"expected_result": "not_applicable", "expected_date": None},
        ),
        (
            _validate_grant_section,
            "granted; the grant was withdrawn",
            {"expected_status": "granted"},
        ),
        (
            _validate_contact_section,
            "https://fynd.org/contact; 2026-10-01; confirmed; the contact page is unavailable",
            {"expected_url": None, "expected_date": None},
        ),
    ],
)
def test_audit_sections_reject_contradictory_full_prose(validator, section, kwargs):
    with pytest.raises(RegistryError):
        validator(section, **kwargs)


def _registry_text(
    *,
    status: str = "auditing",
    contact: str = "https://fynd.example/contact",
    checked: str = "",
    source_overrides: str = "",
) -> str:
    return f'''[registry]
version = 1
contact_url = "{contact}"
contact_url_checked_on = "{checked}"
user_agent = "FyndDiscovery/0.1 (+{contact})"

[[source]]
id = "sample"
name = "Sample"
publisher_name = "Publisher"
homepage_url = "https://sample.test"
source_kind = "aggregator"
status = "{status}"
status_reason = ""
adapter = "app.discovery.kaggle"

[source.access]
method = "official_client"
allowed_hosts = ["sample.test"]
auth_env_var = ""
auth_failure_403_markers = []

[source.use]
fields = ["title"]
triggers = ["manual"]
frequency = "weekly"

[source.permission]
robots_checked_on = ""
robots_result = "not_applicable"
terms_url = "https://sample.test/terms"
terms_reviewed_on = "2026-10-31"
terms_decision = "allowed"
terms_notes = "Reviewed official client terms permit this use"
previous_terms_decision = ""
terms_change_reference = ""

[source.permission.grant]
status = "none"
requested_on = ""
granted_on = ""
expires_on = ""
granted_by_role = ""
scope = []
conditions = ""
reference = ""
evidence_stored_offrepo = false

[source.limits]
min_request_interval_s = 1.0
max_items_per_run = 50
max_response_bytes = 1000000
max_retries = 2
max_retry_after_s = 30
rate_limit_statuses = [429]

[source.attribution]
display_name = "Publisher"
link_required = true
required_text = ""

[source.audit]
last_audit_on = "2026-10-31"
next_audit_due = "2027-01-29"
auditor = "reviewer"
latest_note = "docs/sources/audits/sample/2026-10-31.md"
approved_on = "2026-10-31"
approved_by = "reviewer"
{source_overrides}'''


def _note_text(data: dict) -> str:
    registry = data["registry"]
    source = data["source"][0]
    use = source["use"]
    permission = source["permission"]
    grant = permission["grant"]
    audit = source["audit"]
    limits = source["limits"]
    note_date = audit["last_audit_on"]
    robots_summary = (
        "Not applicable; official API terms govern."
        if permission["robots_result"] == "not_applicable"
        else f"{permission['robots_checked_on']}; {permission['robots_result']}"
    )
    lines = [
        f"# Source audit: {source['id']} ({note_date})",
        f"Auditor: {audit['auditor']}",
        f"Status after audit: {source['status']}",
        f"Source kind: {source['source_kind']}",
        "1. Fields, triggers and frequency needed ([source.use]): "
        f"{', '.join(use['fields'])}; {', '.join(use['triggers'])}; {use['frequency']}",
        f"2. Official channels found: {source['publisher_name']} official client",
        f"3. robots.txt (date, result, relevant rules, crawl delay): {robots_summary}",
        "4. Terms reviewed (URL, date, decision, paraphrase): "
        f"{permission['terms_url']}; {permission['terms_reviewed_on']}; "
        f"{permission['terms_decision']}; {permission['terms_notes']}",
        "5. Permission grant (status, scope as key:value entries, expiry, reference; "
        "correspondence stored off-repo? yes/no): "
        f"{grant['status']}; {', '.join(grant['scope'])}; {grant['expires_on']}; "
        f"{grant['reference']}; off-repository={grant['evidence_stored_offrepo']}",
        "6. Change of authorization or terms since last audit "
        "(previous decision, new decision, document or clarification, date): "
        "No change since last audit.",
        "7. Technical notes: Official client; bounded result limit.",
        "8. Rate-limit and error behavior (rate-limit statuses, Retry-After seen, real 403 "
        "examples, auth_failure_403_markers): "
        f"statuses={', '.join(map(str, limits['rate_limit_statuses']))}; Retry-After capped.",
        "9. Sample of 10 items (differences found): "
        "Compared ten samples; no unresolved mismatches.",
        "10. Fit for Fynd (relevant share): Relevant technical opportunities reviewed.",
        "11. Attribution required: "
        f"{source['attribution']['display_name']}; "
        f"link required={source['attribution']['link_required']}",
        "12. Contact URL check (URL, date, result): "
        f"{registry['contact_url']}; {registry['contact_url_checked_on']}; "
        "confirmed, page identifies the project and contact.",
        "13. Limits chosen: "
        f"min_request_interval_s={limits['min_request_interval_s']}; "
        f"max_items_per_run={limits['max_items_per_run']}; "
        f"max_response_bytes={limits['max_response_bytes']}; "
        f"max_retries={limits['max_retries']}; "
        f"max_retry_after_s={limits['max_retry_after_s']}",
        "14. Open questions / permission requests sent: No outstanding questions.",
        "15. Decision and next audit due: "
        f"{source['status']}; auditor={audit['auditor']}; "
        f"next due={audit['next_audit_due']}",
    ]
    return "\n".join(lines) + "\n"


def _write_registry(tmp_path: Path, content: str, *, audit_note: str | None = None) -> Path:
    path = tmp_path / "docs" / "sources" / "registry.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        data = tomllib.loads(content)
        source = data.get("source", [])[0]
        note_rel = source["audit"]["latest_note"]
        if note_rel:
            note = tmp_path / Path(note_rel)
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                audit_note if audit_note is not None else _note_text(data), encoding="utf-8"
            )
    except (tomllib.TOMLDecodeError, IndexError, KeyError, TypeError):
        pass
    return path


def _load_valid(tmp_path: Path) -> SourceRegistry:
    path = _write_registry(
        tmp_path,
        _registry_text(status="auditing", contact="https://fynd.org/contact", checked="2026-10-31"),
    )
    registry = load_registry(path, today=TODAY)
    source = replace(registry.sources[0], status="approved")
    note_path = tmp_path / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    note = note.replace("Status after audit: auditing", "Status after audit: approved")
    note = note.replace(
        "15. Decision and next audit due: auditing;",
        "15. Decision and next audit due: approved;",
    )
    note_path.write_text(note, encoding="utf-8")
    return replace(registry, sources=(source,))


def _sync_audit_note(registry: SourceRegistry, source) -> None:
    data = tomllib.loads(registry.path.read_text(encoding="utf-8"))
    raw = data["source"][0]
    raw["status"] = source.status
    raw["use"] = source.use
    raw["permission"] = source.permission
    raw["limits"] = source.limits
    raw["audit"] = source.audit
    note_path = registry.path.parent.parent.parent / Path(source.audit["latest_note"])
    note_path.write_text(_note_text(data), encoding="utf-8")


def test_empty_registry_loads_without_approving_sources(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        """[registry]
version = 1
contact_url = "https://fynd.example/contact"
contact_url_checked_on = ""
user_agent = "FyndDiscovery/0.1 (+https://fynd.example/contact)"
""",
    )
    assert load_registry(path, today=TODAY).sources == ()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("version = 1", "version = 2"),
        ('source_kind = "aggregator"', 'source_kind = "bogus"'),
        ('status = "auditing"', 'status = "wat"'),
        ('method = "official_client"', 'method = "wat"'),
        ('robots_result = "not_applicable"', 'robots_result = "wat"'),
        ('terms_decision = "allowed"', 'terms_decision = "wat"'),
        ('frequency = "weekly"', 'frequency = "on_demand"'),
        ('allowed_hosts = ["sample.test"]', 'allowed_hosts = ["*.sample.test"]'),
        ('fields = ["title"]', 'fields = ["organizer"]'),
        ('triggers = ["manual"]', 'triggers = ["manual", "manual"]'),
        ("max_retries = 2", "max_retries = 3"),
        ("rate_limit_statuses = [429]", "rate_limit_statuses = [403]"),
    ],
)
def test_schema_and_enum_errors_fail_closed(tmp_path: Path, old: str, new: str) -> None:
    path = _write_registry(tmp_path, _registry_text().replace(old, new))
    with pytest.raises(RegistryError):
        load_registry(path, today=TODAY)


def test_adapter_kind_and_status_consistency_are_validated(tmp_path: Path) -> None:
    content = _registry_text().replace('source_kind = "aggregator"', 'source_kind = "official"')
    with pytest.raises(RegistryError, match="does not match adapter"):
        load_registry(_write_registry(tmp_path, content), today=TODAY)

    content = _registry_text(
        status="approved", contact="https://fynd.org/contact", checked="2026-10-31"
    ).replace('robots_result = "not_applicable"', 'robots_result = "disallowed"')
    with pytest.raises(RegistryError, match="disallowed robots"):
        load_registry(_write_registry(tmp_path, content), today=TODAY)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('homepage_url = "https://sample.test"', 'homepage_url = "http://sample.test"'),
        ('allowed_hosts = ["sample.test"]', 'allowed_hosts = ["https://sample.test/path"]'),
        ('auth_env_var = ""', 'auth_env_var = "invalid-name"'),
        ("auth_failure_403_markers = []", 'auth_failure_403_markers = ["Login"]'),
        ("min_request_interval_s = 1.0", "min_request_interval_s = 0"),
        ("max_retry_after_s = 30", "max_retry_after_s = 31"),
    ],
)
def test_access_url_and_limit_rules(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(RegistryError):
        load_registry(_write_registry(tmp_path, _registry_text().replace(old, new)), today=TODAY)


@pytest.mark.parametrize(
    "secret",
    [
        'token = "abc"',
        'notes = "Bearer abcdefghijklmnopqrstuvwxyz"',
        'required_text = "ghp_abcdefghijklmnopqrstuvwxyz123456"',
    ],
)
def test_secret_like_values_and_secret_keys_are_rejected(tmp_path: Path, secret: str) -> None:
    content = _registry_text().replace('required_text = ""', f'required_text = ""\n{secret}')
    with pytest.raises(RegistryError):
        load_registry(_write_registry(tmp_path, content), today=TODAY)


def test_missing_malformed_registry_and_unknown_keys_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "missing.toml", today=TODAY)
    path = _write_registry(tmp_path, "not toml = [")
    with pytest.raises(RegistryError):
        load_registry(path, today=TODAY)
    path = _write_registry(
        tmp_path, _registry_text().replace("version = 1", "version = 1\nextra = true")
    )
    with pytest.raises(RegistryError, match="unknown keys"):
        load_registry(path, today=TODAY)


def test_audit_note_must_exist_and_match_date(tmp_path: Path) -> None:
    content = _registry_text().replace('status = "auditing"', 'status = "auditing"')
    path = _write_registry(tmp_path, content)
    path.with_name("registry.toml").parent.joinpath("audits", "sample", "2026-10-31.md").unlink()
    with pytest.raises(RegistryError, match="does not exist"):
        load_registry(path, today=TODAY)


def test_audit_freshness_and_newest_note_are_enforced(tmp_path: Path) -> None:
    stale = _registry_text().replace("2026-10-31", "2026-05-01").replace("2027-01-29", "2026-12-01")
    with pytest.raises(RegistryError, match="90-day re-audit period"):
        load_registry(_write_registry(tmp_path, stale), today=TODAY)

    path = _write_registry(tmp_path, _registry_text())
    newer = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-11-01.md"
    newer.write_text("Later dated audit note", encoding="utf-8")
    with pytest.raises(RegistryError, match="newest dated audit note"):
        load_registry(path, today=TODAY)

    content = _registry_text().replace("2026-10-31.md", "2026-10-30.md")
    path = _write_registry(tmp_path, content)
    dated_note = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-10-30.md"
    dated_note.write_text("Audit note", encoding="utf-8")
    with pytest.raises(RegistryError, match="filename date"):
        load_registry(path, today=TODAY)


def test_audit_note_suffix_ten_is_newer_than_suffix_nine(tmp_path: Path) -> None:
    content = _registry_text().replace("2026-10-31.md", "2026-10-31-9.md")
    path = _write_registry(tmp_path, content)
    note_ten = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-10-31-10.md"
    note_ten.write_text("Later same-day correction", encoding="utf-8")

    with pytest.raises(RegistryError, match="newest dated audit note"):
        load_registry(path, today=TODAY)

    updated_content = path.read_text(encoding="utf-8").replace(
        "2026-10-31-9.md", "2026-10-31-10.md"
    )
    path.write_text(updated_content, encoding="utf-8")
    registry = load_registry(path, today=TODAY)
    assert registry.sources[0].audit["latest_note"].endswith("2026-10-31-10.md")


def test_approved_source_requires_complete_audit_note(tmp_path: Path) -> None:
    content = _registry_text(
        status="approved", contact="https://fynd.org/contact", checked="2026-10-31"
    )
    with pytest.raises(RegistryError, match="missing required sections"):
        load_registry(
            _write_registry(tmp_path, content, audit_note="# Source audit: sample (2026-10-31)"),
            today=TODAY,
        )


def test_complete_audit_note_passes_validation(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent

    _require_audit_sections(source, registry, root)


@pytest.mark.parametrize(
    "duplicate_line",
    [
        "# Source audit: sample (2026-10-31)",
        "Status after audit: approved",
        "Decision: approved\nDecision: approved",
        "Auditor: reviewer",
        "Source kind: aggregator",
    ],
)
def test_duplicate_audit_metadata_headers_are_rejected(
    tmp_path: Path, duplicate_line: str
) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note_path.write_text(
        note_path.read_text(encoding="utf-8") + f"\n{duplicate_line}\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="duplicate"):
        _require_audit_sections(source, registry, root)


def test_contradictory_status_and_required_section_evidence_are_rejected(
    tmp_path: Path,
) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    note = note.replace(
        "Status after audit: approved",
        "Status after audit: approved\nStatus after audit: prohibited",
    )
    note = note.replace(
        "Reviewed official client terms permit this use",
        "Reviewed official client terms permit this use; terms decision: prohibited",
    )
    note_path.write_text(note, encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate 'status after audit'"):
        _require_audit_sections(source, registry, root)

    note_path.write_text(
        note.replace("\nStatus after audit: prohibited", ""), encoding="utf-8"
    )
    with pytest.raises(RegistryError, match="contradictory prose"):
        _require_audit_sections(source, registry, root)


@pytest.mark.parametrize(
    ("needle", "replacement", "error"),
    [
        (
            "Reviewed official client terms permit this use",
            "Reviewed official client terms permit this use; the terms prohibit collection",
            "contradictory prose",
        ),
        (
            "Not applicable; official API terms govern.",
            "Not applicable; official API terms govern; robots disallow our requested path",
            "contradictory prose",
        ),
        (
            "none; ; ; ; off-repository=False",
            "none; ; ; ; off-repository=False; permission was granted",
            "contradictory status prose",
        ),
        (
            "confirmed, page identifies the project and contact.",
            "confirmed, page identifies the project and contact; the page was unreachable",
            "contradictory result prose",
        ),
    ],
)
def test_unlabelled_contradictions_in_required_sections_fail_closed(
    tmp_path: Path, needle: str, replacement: str, error: str
) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    if needle.startswith("none;"):
        line = next(line for line in note.splitlines() if line.startswith("5. "))
        note = note.replace(line, line.replace(needle, replacement))
    else:
        note = note.replace(needle, replacement)
    note_path.write_text(note, encoding="utf-8")

    with pytest.raises(RegistryError, match=error):
        _require_audit_sections(source, registry, root)


def test_section_15_checks_complete_decision_content(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8").replace(
        "next due=2027-01-29", "next due=2027-01-29; decision=prohibited"
    )
    note_path.write_text(note, encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate decision label"):
        _require_audit_sections(source, registry, root)


def test_duplicate_section_5_fails_even_when_second_section_is_valid(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    valid_section = next(line for line in note.splitlines() if line.startswith("5. "))
    note = note.replace(valid_section, "5. Malformed grant heading: invalid", 1)
    note = f"{note}\n{valid_section}\n"
    note_path.write_text(note, encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate section number 5"):
        _require_audit_sections(source, registry, root)


def test_duplicate_section_15_fails_validation(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    note = f"{note}\n15. Decision and next audit due: approved; auditor=reviewer\n"
    note_path.write_text(note, encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate section number 15"):
        _require_audit_sections(source, registry, root)


def test_duplicate_identical_section_content_fails_validation(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    root = registry.path.parent.parent.parent
    note_path = root / Path(source.audit["latest_note"])
    note = note_path.read_text(encoding="utf-8")
    duplicate = next(line for line in note.splitlines() if line.startswith("2. "))
    note_path.write_text(f"{note}\n{duplicate}\n", encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate section number 2"):
        _require_audit_sections(source, registry, root)


def _prohibited_lift_fixture(
    tmp_path: Path,
    *,
    prior_note: str,
    status: str = "auditing",
    change_evidence: str = (
        "Previous decision: prohibited; New decision: allowed; "
        "Change reference: terms-version-2; Change date: 2026-10-31; "
        "Formal change evidence: formal clarification was issued"
    ),
) -> Path:
    content = (
        _registry_text(
            status=status,
            contact="https://fynd.org/contact",
            checked="2026-10-30",
        )
        .replace('previous_terms_decision = ""', 'previous_terms_decision = "prohibited"')
        .replace('terms_change_reference = ""', 'terms_change_reference = "terms-version-2"')
    )
    path = _write_registry(tmp_path, content)
    prior = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-09-30.md"
    prior.write_text(prior_note, encoding="utf-8")
    latest = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-10-31.md"
    latest.write_text(
        latest.read_text(encoding="utf-8").replace(
            "No change since last audit.", change_evidence
        ),
        encoding="utf-8",
    )
    return path


def _prior_prohibited_note(
    *, source_id: str = "sample", audit_date: str = "2026-09-30", decision: str = "prohibited"
) -> str:
    audit_day = date.fromisoformat(audit_date)
    contact_day = audit_day - timedelta(days=1)
    data = tomllib.loads(
        _registry_text(
            status="prohibited",
            contact="https://fynd.org/contact",
            checked=contact_day.isoformat(),
        )
    )
    source = data["source"][0]
    source["id"] = source_id
    source["status_reason"] = "Terms prohibit automated collection"
    source["permission"]["terms_decision"] = "prohibited"
    source["permission"]["terms_reviewed_on"] = audit_date
    source["permission"]["terms_notes"] = "Official terms prohibit collection"
    source["audit"]["last_audit_on"] = audit_date
    source["audit"]["next_audit_due"] = (audit_day + timedelta(days=90)).isoformat()
    source["audit"]["latest_note"] = f"docs/sources/audits/{source_id}/{audit_date}.md"
    note = _note_text(data)
    if decision != "prohibited":
        note = note.replace("Status after audit: prohibited", f"Status after audit: {decision}")
        note = note.replace(
            "15. Decision and next audit due: prohibited;",
            f"15. Decision and next audit due: {decision};",
        )
    return note


def test_prohibited_lift_requires_new_audit_and_substantive_evidence(tmp_path: Path) -> None:
    content = _registry_text().replace(
        'previous_terms_decision = ""', 'previous_terms_decision = "prohibited"'
    ).replace('terms_change_reference = ""', 'terms_change_reference = "terms-version-2"')
    with pytest.raises(RegistryError, match="prior prohibited audit note"):
        load_registry(_write_registry(tmp_path, content), today=TODAY)

    same_date_content = content.replace(
        "2026-10-31.md", "2026-10-31-2.md"
    )
    same_date_root = tmp_path / "same-date"
    same_date_path = _write_registry(same_date_root, same_date_content)
    same_date_prior = (
        same_date_root / "docs" / "sources" / "audits" / "sample" / "2026-10-31.md"
    )
    same_date_prior.write_text(_prior_prohibited_note(audit_date="2026-10-31"), encoding="utf-8")
    with pytest.raises(RegistryError, match="prior prohibited audit note"):
        load_registry(same_date_path, today=TODAY)

    path = _prohibited_lift_fixture(
        tmp_path / "incomplete", prior_note=_prior_prohibited_note(), change_evidence="No change"
    )
    with pytest.raises(RegistryError, match="affirmative formal change evidence"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_rejects_malformed_prior_note(tmp_path: Path) -> None:
    path = _prohibited_lift_fixture(tmp_path, prior_note="15. Decision: prohibited")
    with pytest.raises(RegistryError, match="audit note"):
        load_registry(path, today=TODAY)


@pytest.mark.parametrize(
    "prior_note",
    [
        _prior_prohibited_note(source_id="another-source"),
        _prior_prohibited_note(audit_date="2026-09-29"),
    ],
)
def test_prohibited_lift_rejects_wrong_prior_source_or_date(
    tmp_path: Path, prior_note: str
) -> None:
    path = _prohibited_lift_fixture(tmp_path, prior_note=prior_note)
    with pytest.raises(RegistryError, match="audit note"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_rejects_negated_prior_decision(tmp_path: Path) -> None:
    prior = _prior_prohibited_note(decision="not prohibited")
    path = _prohibited_lift_fixture(tmp_path, prior_note=prior)
    with pytest.raises(RegistryError, match="status/decision metadata"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_rejects_repeated_contradictory_status_lines(
    tmp_path: Path,
) -> None:
    prior = _prior_prohibited_note() + "Status after audit: allowed\n"
    path = _prohibited_lift_fixture(tmp_path, prior_note=prior)

    with pytest.raises(RegistryError, match="duplicate 'status after audit'"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_rejects_contradictory_decision_metadata(tmp_path: Path) -> None:
    prior = _prior_prohibited_note() + "Decision: allowed\n"
    path = _prohibited_lift_fixture(tmp_path, prior_note=prior)

    with pytest.raises(RegistryError, match="status/decision metadata"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_rejects_negated_formal_evidence(tmp_path: Path) -> None:
    path = _prohibited_lift_fixture(
        tmp_path,
        prior_note=_prior_prohibited_note(),
        change_evidence=(
            "Previous decision: prohibited; New decision: allowed; "
            "Change reference: terms-version-2; Change date: 2026-10-31; "
            "Formal change evidence: no formal clarification was issued"
        ),
    )
    with pytest.raises(RegistryError, match="affirmative formal change evidence"):
        load_registry(path, today=TODAY)


@pytest.mark.parametrize(
    ("change_evidence", "label"),
    [
        (
            "Previous decision: not prohibited; Previous decision: prohibited; "
            "New decision: allowed; Change reference: terms-version-2; "
            "Change date: 2026-10-31; "
            "Formal change evidence: formal clarification was issued",
            "previous decision",
        ),
        (
            "Previous decision: prohibited; Previous decision: prohibited; "
            "New decision: allowed; Change reference: terms-version-2; "
            "Change date: 2026-10-31; "
            "Formal change evidence: formal clarification was issued",
            "previous decision",
        ),
        (
            "Previous decision: prohibited; New decision: allowed; "
            "Change reference: terms-version-2; Change date: 2026-10-31; "
            "Formal change evidence: no formal clarification was issued; "
            "Formal change evidence: formal clarification was issued",
            "formal change evidence",
        ),
        (
            "Previous decision: prohibited; New decision: allowed; "
            "Change reference: terms-version-2; Change date: 2026-10-31; "
            "Formal change evidence: formal clarification was issued; "
            "Formal change evidence: formal clarification was issued",
            "formal change evidence",
        ),
        (
            "Previous decision: prohibited; New decision: allowed; "
            "Change reference: terms-version-2; Change reference: terms-version-2; "
            "Change date: 2026-10-31; "
            "Formal change evidence: formal clarification was issued",
            "change reference",
        ),
        (
            "Previous decision: prohibited; New decision: allowed; "
            "Change reference: terms-version-2; Change date: 2026-10-31; "
            "Change date: 2026-10-31; "
            "Formal change evidence: formal clarification was issued",
            "change date",
        ),
    ],
)
def test_prohibited_lift_rejects_duplicate_evidence_labels(
    tmp_path: Path, change_evidence: str, label: str
) -> None:
    path = _prohibited_lift_fixture(
        tmp_path,
        prior_note=_prior_prohibited_note(),
        change_evidence=change_evidence,
    )

    with pytest.raises(RegistryError, match=f"duplicate evidence label '{label}'"):
        load_registry(path, today=TODAY)


@pytest.mark.parametrize(
    "change_evidence",
    [
        "Previous decision: prohibited; New decision: allowed; Change reference: other-ref; "
        "Change date: 2026-10-31; Formal change evidence: formal clarification was issued",
        "Previous decision: prohibited; New decision: allowed; Change reference: terms-version-2; "
        "Change date: 2026-10-30; Formal change evidence: formal clarification was issued",
    ],
)
def test_prohibited_lift_requires_matching_reference_and_date(
    tmp_path: Path, change_evidence: str
) -> None:
    path = _prohibited_lift_fixture(
        tmp_path, prior_note=_prior_prohibited_note(), change_evidence=change_evidence
    )
    with pytest.raises(RegistryError, match="affirmative formal change evidence"):
        load_registry(path, today=TODAY)


def test_prohibited_lift_accepts_valid_formal_evidence(tmp_path: Path) -> None:
    path = _prohibited_lift_fixture(tmp_path, prior_note=_prior_prohibited_note())
    registry = load_registry(path, today=TODAY)
    assert registry.sources[0].status == "auditing"


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        (
            "# Source audit: sample (2026-10-31)",
            "# Source audit: unrelated (2026-10-31)",
            "header must identify source and date",
        ),
        (
            "# Source audit: sample (2026-10-31)",
            "# Source audit: sample (2026-10-30)",
            "header must identify source and date",
        ),
        (
            "Status after audit: auditing",
            "Status after audit: auditing\nStatus after audit: approved",
            "duplicate 'status after audit'",
        ),
        (
            "7. Technical notes: Official client; bounded result limit.",
            "7. Technical notes:",
            "missing required sections",
        ),
    ],
)
def test_prohibited_lift_validates_complete_new_audit_note(
    tmp_path: Path, old: str, new: str, error: str
) -> None:
    path = _prohibited_lift_fixture(
        tmp_path, prior_note=_prior_prohibited_note()
    )
    note_path = tmp_path / "docs" / "sources" / "audits" / "sample" / "2026-10-31.md"
    note_path.write_text(
        note_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )

    with pytest.raises(RegistryError, match=error):
        load_registry(path, today=TODAY)


def test_prohibited_lift_requires_intermediate_auditing_status(tmp_path: Path) -> None:
    path = _prohibited_lift_fixture(
        tmp_path,
        prior_note=_prior_prohibited_note(),
        status="approved",
    )

    with pytest.raises(RegistryError, match="must return to auditing"):
        load_registry(path, today=TODAY)


@pytest.mark.parametrize(
    "host",
    [
        "fynd.example",
        "fynd.test",
        "fynd.invalid",
        "localhost",
        "api.localhost",
        "example.com",
        "example.net",
        "example.org",
        "api.example.com.",
        "example.org.",
    ],
)
def test_reserved_placeholder_hosts_are_rejected(host: str) -> None:
    assert _is_placeholder_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "fynd.example",
        "fynd.test",
        "fynd.invalid",
        "localhost",
        "api.localhost",
        "example.com",
        "example.net",
        "example.org",
    ],
)
def test_placeholder_hosts_prevent_authorization(tmp_path: Path, host: str) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    bad_registry = replace(
        registry,
        contact_url=f"https://{host}/contact",
        user_agent=f"FyndDiscovery/0.1 (+https://{host}/contact)",
    )
    decision = authorization_decision(bad_registry, source, today=TODAY)
    assert not decision.allowed
    assert any("placeholder" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "host",
    [
        "api.example.com",
        "cdn.example.net",
        "events.example.org",
        "service.example",
        "events.test",
        "feed.invalid",
    ],
)
def test_reserved_placeholder_subdomains_are_rejected(host: str) -> None:
    assert _is_placeholder_host(host)


@pytest.mark.parametrize(
    "host",
    ["fynd.org", "competitions.example.edu", "api.kaggle.com", "example.edu."],
)
def test_legitimate_hosts_are_not_misclassified_as_placeholders(host: str) -> None:
    assert not _is_placeholder_host(host)


@pytest.mark.parametrize("url_field", ["contact", "homepage", "terms"])
def test_trailing_dot_placeholder_urls_are_rejected(
    tmp_path: Path, url_field: str
) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    if url_field == "contact":
        bad_registry = replace(
            registry,
            contact_url="https://api.example.com./contact",
            user_agent="FyndDiscovery/0.1 (+https://api.example.com./contact)",
        )
    elif url_field == "homepage":
        source = replace(source, homepage_url="https://api.example.com./")
        bad_registry = registry
    else:
        source = replace(
            source,
            permission={
                **source.permission,
                "terms_url": "https://api.example.com./terms",
            },
        )
        bad_registry = registry

    decision = authorization_decision(bad_registry, source, today=TODAY)
    assert not decision.allowed
    assert any("placeholder" in reason for reason in decision.reasons)


def test_every_existing_adapter_has_registry_entry_and_kaggle_stays_auditing() -> None:
    repo_root = Path(__file__).parents[3]
    discovery_dir = repo_root / "apps" / "api" / "app" / "discovery"
    adapter_modules: set[str] = set()
    for path in discovery_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "DiscoveryCandidate"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "DiscoveryCandidate"
            )
            and any(
                keyword.arg == "source_kind" and isinstance(keyword.value, ast.Constant)
                for keyword in node.keywords
            )
            for node in ast.walk(tree)
        ):
            adapter_modules.add(f"app.discovery.{path.stem}")
    registry = load_registry(repo_root / "docs" / "sources" / "registry.toml", today=TODAY)
    registered = {source.adapter: source for source in registry.sources}
    assert adapter_modules <= registered.keys()
    kaggle = registered["app.discovery.kaggle"]
    assert kaggle.status == "auditing"
    assert kaggle.source_kind == "aggregator"


def test_approved_source_must_pass_all_gate_conditions(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    decision = authorization_decision(registry, source, today=TODAY)
    assert not decision.allowed
    assert any("no verifiable numeric permission limits" in reason for reason in decision.reasons)

    failures = [
        replace(source, status="auditing"),
        replace(source, audit={**source.audit, "approved_on": "", "approved_by": ""}),
        replace(source, audit={**source.audit, "last_audit_on": "", "latest_note": ""}),
        replace(source, audit={**source.audit, "next_audit_due": "2026-11-01"}),
        replace(source, permission={**source.permission, "terms_decision": "unknown"}),
        replace(source, permission={**source.permission, "robots_result": "unknown"}),
        replace(source, access={**source.access, "allowed_hosts": []}),
        replace(source, use={**source.use, "fields": []}),
    ]
    for altered in failures:
        assert not authorization_decision(registry, altered, today=TODAY).allowed

    bad_registry = replace(registry, contact_url="https://fynd.example/contact")
    assert not authorization_decision(bad_registry, source, today=TODAY).allowed
    bad_registry = replace(registry, user_agent="FyndDiscovery/0.1")
    assert not authorization_decision(bad_registry, source, today=TODAY).allowed
    bad_registry = replace(registry, contact_url_checked_on="2026-07-01")
    assert not authorization_decision(bad_registry, source, today=TODAY).allowed
    altered = replace(source, access={**source.access, "auth_env_var": "MISSING_TOKEN"})
    assert not authorization_decision(registry, altered, today=TODAY, environ={}).allowed
    present = replace(source, access={**source.access, "auth_env_var": "TOKEN_FOR_TEST"})

    class PresenceOnly(dict):
        def __getitem__(self, key: str) -> str:
            raise AssertionError("the credential value must never be read")

    present_decision = authorization_decision(
        registry, present, today=TODAY, environ=PresenceOnly(TOKEN_FOR_TEST="secret")
    )
    assert not present_decision.allowed
    assert not any("TOKEN_FOR_TEST" in reason for reason in present_decision.reasons)


def test_whitespace_only_approver_is_trimmed_and_rejected(tmp_path: Path) -> None:
    content = _registry_text().replace('approved_by = "reviewer"', 'approved_by = "   "')
    registry = load_registry(_write_registry(tmp_path, content), today=TODAY)

    assert registry.sources[0].audit["approved_by"] == ""
    decision = authorization_decision(
        registry,
        replace(registry.sources[0], status="approved"),
        today=TODAY,
    )
    assert not decision.allowed
    assert "approval date and approver are required" in decision.reasons


@pytest.mark.parametrize("approved_on", ["tomorrow", "   ", "2026-02-30"])
def test_direct_authorization_rejects_malformed_approval_dates(
    tmp_path: Path, approved_on: str
) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    altered = replace(
        source,
        audit={**source.audit, "approved_on": approved_on, "approved_by": "reviewer"},
    )

    decision = authorization_decision(registry, altered, today=TODAY)

    assert not decision.allowed
    assert "a valid approval date is required" in decision.reasons


def test_direct_authorization_accepts_a_valid_approval_date_field(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    altered = replace(
        source,
        audit={**source.audit, "approved_on": "2026-10-31", "approved_by": "reviewer"},
    )

    decision = authorization_decision(registry, altered, today=TODAY)

    assert not decision.allowed
    assert "approval date and approver are required" not in decision.reasons
    assert "a valid approval date is required" not in decision.reasons
    assert any("no verifiable numeric permission limits" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("limit_name", "valid_value"),
    [
        ("min_request_interval_s", "1.0"),
        ("max_items_per_run", "50"),
        ("max_response_bytes", "1000000"),
        ("max_retries", "2"),
        ("max_retry_after_s", "30"),
    ],
)
@pytest.mark.parametrize("non_finite", ["nan", "inf", "-inf"])
def test_non_finite_numeric_limits_are_rejected(
    tmp_path: Path, limit_name: str, valid_value: str, non_finite: str
) -> None:
    content = _registry_text().replace(
        f"{limit_name} = {valid_value}", f"{limit_name} = {non_finite}"
    )

    with pytest.raises(RegistryError, match="must be positive"):
        load_registry(_write_registry(tmp_path, content), today=TODAY)


def test_permission_grant_scope_and_frequency_coverage(tmp_path: Path) -> None:
    registry = _load_valid(tmp_path)
    source = registry.sources[0]
    grant = {
        "status": "granted",
        "requested_on": "2026-10-01",
        "granted_on": "2026-10-02",
        "expires_on": "2026-12-01",
        "granted_by_role": "administrator",
        "scope": ["field:title", "trigger:manual", "frequency:daily"],
        "conditions": "",
        "reference": "ticket-1",
        "evidence_stored_offrepo": True,
    }
    permitted = replace(
        source,
        permission={**source.permission, "terms_decision": "permission_required", "grant": grant},
    )
    _sync_audit_note(registry, permitted)
    valid_decision = authorization_decision(registry, permitted, today=TODAY)
    assert not valid_decision.allowed
    assert not any("expiry date is required" in reason for reason in valid_decision.reasons)
    assert any(
        "no verifiable numeric permission limits" in reason for reason in valid_decision.reasons
    )
    slower_grant = {**grant, "scope": ["field:title", "trigger:manual", "frequency:weekly"]}
    slower_decision = authorization_decision(
        registry,
        replace(permitted, permission={**permitted.permission, "grant": slower_grant}),
        today=TODAY,
    )
    assert not slower_decision.allowed
    assert any(
        "no verifiable numeric permission limits" in reason
        for reason in slower_decision.reasons
    )
    faster_use = replace(
        permitted,
        permission={**permitted.permission, "grant": slower_grant},
        use={**permitted.use, "frequency": "daily"},
    )
    assert not authorization_decision(registry, faster_use, today=TODAY).allowed
    assert any(
        "frequency is faster" in reason
        for reason in authorization_decision(registry, faster_use, today=TODAY).reasons
    )
    missing_trigger = {**grant, "scope": ["field:title", "trigger:scheduled", "frequency:daily"]}
    assert not authorization_decision(
        registry,
        replace(permitted, permission={**permitted.permission, "grant": missing_trigger}),
        today=TODAY,
    ).allowed
    expired_grant = {**grant, "expires_on": "2026-10-31"}
    assert not authorization_decision(
        registry,
        replace(permitted, permission={**permitted.permission, "grant": expired_grant}),
        today=TODAY,
    ).allowed
    assert any(
        "permission grant has expired" in reason
        for reason in authorization_decision(
            registry,
            replace(permitted, permission={**permitted.permission, "grant": expired_grant}),
            today=TODAY,
        ).reasons
    )


def test_granted_permission_requires_explicit_valid_expiry(tmp_path: Path) -> None:
    granted_template = (
        _registry_text()
        .replace('terms_decision = "allowed"', 'terms_decision = "permission_required"')
        .replace('status = "none"', 'status = "granted"')
        .replace("scope = []", 'scope = ["field:title", "trigger:manual", "frequency:weekly"]')
        .replace('granted_on = ""', 'granted_on = "2026-10-01"')
        .replace('granted_by_role = ""', 'granted_by_role = "administrator"')
        .replace('\nreference = ""', '\nreference = "ticket-1"')
        .replace("evidence_stored_offrepo = false", "evidence_stored_offrepo = true")
    )
    missing_expiry = granted_template
    with pytest.raises(RegistryError, match="explicit expiry date"):
        load_registry(_write_registry(tmp_path / "missing", missing_expiry), today=TODAY)

    future_expiry = granted_template.replace('expires_on = ""', 'expires_on = "2026-12-01"')
    registry = load_registry(
        _write_registry(tmp_path / "future", future_expiry), today=TODAY
    )
    assert registry.sources[0].permission["grant"]["expires_on"] == "2026-12-01"

    expired = granted_template.replace('expires_on = ""', 'expires_on = "2026-10-31"')
    with pytest.raises(RegistryError, match="grant is expired"):
        load_registry(_write_registry(tmp_path / "expired", expired), today=TODAY)


def test_granted_permission_with_allowed_terms_still_requires_expiry(tmp_path: Path) -> None:
    template = (
        _registry_text()
        .replace('status = "none"', 'status = "granted"')
        .replace("scope = []", 'scope = ["field:title", "trigger:manual", "frequency:weekly"]')
    )
    with pytest.raises(RegistryError, match="explicit expiry date"):
        load_registry(_write_registry(tmp_path / "missing", template), today=TODAY)

    future = template.replace('expires_on = ""', 'expires_on = "2026-12-01"')
    registry = load_registry(_write_registry(tmp_path / "future", future), today=TODAY)
    assert registry.sources[0].permission["terms_decision"] == "allowed"
    assert registry.sources[0].permission["grant"]["expires_on"] == "2026-12-01"

    expired = template.replace('expires_on = ""', 'expires_on = "2026-10-31"')
    with pytest.raises(RegistryError, match="grant is expired"):
        load_registry(_write_registry(tmp_path / "expired", expired), today=TODAY)


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        (
            '["field:description", "trigger:manual", "frequency:weekly"]',
            "cover all declared fields",
        ),
        (
            '["field:title", "trigger:scheduled", "frequency:weekly"]',
            "cover all declared triggers",
        ),
        ('["field:title", "trigger:manual", "frequency:monthly"]', "faster than grant"),
    ],
)
def test_grant_coverage_is_validated_during_loading(
    tmp_path: Path, scope: str, message: str
) -> None:
    content = (
        _registry_text()
        .replace('terms_decision = "allowed"', 'terms_decision = "permission_required"')
        .replace('status = "none"', 'status = "granted"')
        .replace("scope = []", f"scope = {scope}")
        .replace('granted_on = ""', 'granted_on = "2026-10-01"')
        .replace('granted_by_role = ""', 'granted_by_role = "administrator"')
        .replace('\nreference = ""', '\nreference = "ticket-1"')
        .replace("evidence_stored_offrepo = false", "evidence_stored_offrepo = true")
    )
    with pytest.raises(RegistryError, match=message):
        load_registry(_write_registry(tmp_path, content), today=TODAY)


def test_grant_scope_rejects_duplicates_wildcards_and_unknown_fields(tmp_path: Path) -> None:
    for scope in (
        '["field:title", "field:title", "trigger:manual", "frequency:daily"]',
        '["field:*", "trigger:manual", "frequency:daily"]',
        '["field:organizer", "trigger:manual", "frequency:daily"]',
        '["field:title", "trigger:manual", "frequency:daily", "frequency:weekly"]',
    ):
        content = _registry_text().replace("scope = []", f"scope = {scope}")
        with pytest.raises(RegistryError):
            load_registry(_write_registry(tmp_path, content), today=TODAY)


def test_prohibited_terms_cannot_be_overridden_and_requires_formal_lift(tmp_path: Path) -> None:
    prohibited = (
        _registry_text()
        .replace('status = "auditing"', 'status = "approved"')
        .replace('terms_decision = "allowed"', 'terms_decision = "prohibited"')
    )
    with pytest.raises(RegistryError, match="prohibited"):
        load_registry(_write_registry(tmp_path, prohibited), today=TODAY)
    granted = (
        prohibited.replace('status = "approved"', 'status = "prohibited"')
        .replace('status_reason = ""', 'status_reason = "Terms prohibit collection"')
        .replace('status = "none"', 'status = "granted"')
        .replace('scope = []', 'scope = ["field:title", "trigger:manual", "frequency:weekly"]')
        .replace('granted_on = ""', 'granted_on = "2026-10-01"')
        .replace('granted_by_role = ""', 'granted_by_role = "administrator"')
        .replace('\nreference = ""', '\nreference = "ticket-1"')
        .replace('evidence_stored_offrepo = false', 'evidence_stored_offrepo = true')
    )
    with pytest.raises(RegistryError, match="cannot override"):
        load_registry(_write_registry(tmp_path, granted), today=TODAY)
    lift = _registry_text().replace(
        'previous_terms_decision = ""', 'previous_terms_decision = "prohibited"'
    )
    with pytest.raises(RegistryError, match="terms_change_reference"):
        load_registry(_write_registry(tmp_path, lift), today=TODAY)


def test_registry_uses_shared_evidence_vocabulary() -> None:
    registry = Path(__file__).parents[1] / "app" / "evidence_vocabulary.py"
    assert "REVIEW_SUPPORTED_EVIDENCE_FIELDS" in registry.read_text(encoding="utf-8")
    assert len(REVIEW_SUPPORTED_EVIDENCE_FIELDS) == 12
