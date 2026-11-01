from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.evidence_vocabulary import REVIEW_SUPPORTED_EVIDENCE_FIELDS
from app.models import (
    Category,
    Competition,
    CompetitionCategory,
    CompetitionSource,
    FieldEvidence,
    SourcePublisher,
)
from app.review import (
    ReviewError,
    UnsupportedEvidenceField,
    accept_evidence,
    create_category_proposal,
    get_competition_detail,
    list_pending_evidence,
    reject_evidence,
    unassign_category,
)
from app.review import service as review_service

pytestmark = pytest.mark.integration


def test_review_acceptance_paths_match_shared_evidence_vocabulary():
    scalar_fields = set(review_service._SCALAR_FIELD_MAPPINGS) | set(
        review_service._DATE_FIELD_COLUMNS
    )
    acceptance_fields = scalar_fields | {"category"}

    assert acceptance_fields == REVIEW_SUPPORTED_EVIDENCE_FIELDS


@pytest.fixture(scope="module")
def review_database_available():
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for evidence review tests")


@pytest.fixture
def db_session(review_database_available):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _add_source_and_evidence(
    session,
    competition,
    *,
    value,
    field_name="title",
    review_state="candidate",
    suffix="source",
    field_key="",
):
    publisher = SourcePublisher(name=f"Review test publisher {suffix}")
    session.add(publisher)
    session.flush()
    source = CompetitionSource(
        competition_id=competition.id,
        publisher_id=publisher.id,
        source_kind="official",
        source_url=f"https://review.example.test/{suffix}",
        normalized_url=f"https://review.example.test/{suffix}",
    )
    session.add(source)
    session.flush()
    evidence = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name=field_name,
        field_key=field_key,
        observed_value=value,
        evidence_excerpt=str(value),
        review_state=review_state,
    )
    session.add(evidence)
    session.flush()
    return source, evidence


def test_list_and_detail_include_source_provenance_and_evidence(db_session):
    competition = Competition()
    db_session.add(competition)
    db_session.flush()
    source, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Reviewable event"},
        suffix="list-detail",
    )

    pending = list_pending_evidence(db_session)
    listed = next(row for row in pending if row["evidence_id"] == evidence.id)
    detail = get_competition_detail(db_session, competition.id)

    assert listed["source_url"] == source.source_url
    assert listed["publisher"] == source.publisher.name
    assert listed["observed_value"] == {"title": "Reviewable event"}
    assert detail["competition"]["title"] is None
    assert detail["sources"][0]["source_url"] == source.source_url
    assert detail["sources"][0]["evidence"][0]["review_state"] == "candidate"


def test_rejecting_evidence_preserves_canonical_values_and_provenance(db_session):
    competition = Competition(title="Existing title")
    db_session.add(competition)
    db_session.flush()
    source, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Rejected title"},
        suffix="reject",
    )

    rejected = reject_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(source)
    assert rejected.review_state == "rejected"
    assert rejected.reviewed_at is not None
    assert competition.title == "Existing title"
    assert source.source_url == "https://review.example.test/reject"
    assert rejected.source_id == source.id


def test_accepting_evidence_updates_only_its_mapped_canonical_field(db_session):
    competition = Competition(title=None, description=None)
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"description": "A concise event description."},
        field_name="description",
        suffix="description",
    )

    accepted = accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    assert accepted.review_state == "selected"
    assert accepted.reviewed_at is not None
    assert competition.description == "A concise event description."
    assert competition.title is None


def test_accepting_replacement_supersedes_prior_selection_and_keeps_both(db_session):
    competition = Competition(title="Previous title")
    db_session.add(competition)
    db_session.flush()
    _, old = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Previous title"},
        review_state="selected",
        suffix="selected-old",
    )
    _, replacement = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Updated title"},
        suffix="selected-new",
    )

    accept_evidence(db_session, replacement.id)

    db_session.refresh(competition)
    db_session.refresh(old)
    db_session.refresh(replacement)
    assert competition.title == "Updated title"
    assert old.review_state == "superseded"
    assert replacement.review_state == "selected"
    assert db_session.scalar(
        select(func.count()).select_from(FieldEvidence).where(
            FieldEvidence.competition_id == competition.id,
            FieldEvidence.field_name == "title",
        )
    ) == 2


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("title", {"title": "  "}),
        ("format", {"format": "in-person"}),
        ("official_url", {"official_url": "javascript:alert(1)"}),
        ("event_starts", {"event_starts": {"at": "2027-01-01T12:00:00"}}),
    ],
)
def test_invalid_evidence_is_not_selected_or_applied(db_session, field_name, value):
    competition = Competition(title="Stable title")
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value=value,
        field_name=field_name,
        suffix=f"invalid-{field_name}",
    )

    with pytest.raises(ReviewError):
        accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == "Stable title"
    assert evidence.review_state == "candidate"
    assert evidence.reviewed_at is None


def test_unsupported_relationship_evidence_cannot_update_canonical_data(db_session):
    competition = Competition(title="Stable title")
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"name": "Organizer A"},
        field_name="organizer",
        suffix="unsupported-organizer",
    )

    with pytest.raises(UnsupportedEvidenceField, match="not supported"):
        accept_evidence(db_session, evidence.id)

    db_session.refresh(competition)
    db_session.refresh(evidence)
    assert competition.title == "Stable title"
    assert evidence.review_state == "candidate"


def test_flush_failure_rolls_back_review_and_canonical_update(db_session, monkeypatch):
    competition = Competition()
    db_session.add(competition)
    db_session.flush()
    _, evidence = _add_source_and_evidence(
        db_session,
        competition,
        value={"title": "Must not partially apply"},
        suffix="flush-failure",
    )
    original_flush = db_session.flush
    flush_count = 0

    def fail_after_changes(objects=None):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            raise RuntimeError("simulated review flush failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_after_changes)
    with pytest.raises(RuntimeError, match="simulated review"):
        accept_evidence(db_session, evidence.id)
    monkeypatch.setattr(db_session, "flush", original_flush)
    db_session.expire_all()

    assert db_session.get(Competition, competition.id).title is None
    assert db_session.get(FieldEvidence, evidence.id).review_state == "candidate"


def test_migration_clears_only_matching_unselected_candidate_titles(db_session, monkeypatch):
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "6a1c4f2b9d30_clear_unreviewed_candidate_titles.py"
    )
    spec = spec_from_file_location("candidate_title_cleanup", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(execute=lambda statement: db_session.execute(text(statement))),
    )

    matching = Competition(title="Matching candidate")
    mismatch = Competition(title="Manually changed title")
    selected = Competition(title="Accepted title")
    db_session.add_all([matching, mismatch, selected])
    db_session.flush()
    _, matching_evidence = _add_source_and_evidence(
        db_session,
        matching,
        value={"title": "Matching candidate"},
        suffix="migration-match",
    )
    _, mismatch_evidence = _add_source_and_evidence(
        db_session,
        mismatch,
        value={"title": "Original candidate"},
        suffix="migration-mismatch",
    )
    _, selected_evidence = _add_source_and_evidence(
        db_session,
        selected,
        value={"title": "Accepted title"},
        suffix="migration-selected-candidate",
    )
    _add_source_and_evidence(
        db_session,
        selected,
        value={"title": "Accepted title"},
        review_state="selected",
        suffix="migration-selected",
    )

    migration.upgrade()
    db_session.expire_all()

    assert db_session.get(Competition, matching.id).title is None
    assert db_session.get(Competition, mismatch.id).title == "Manually changed title"
    assert db_session.get(Competition, selected.id).title == "Accepted title"
    assert db_session.get(FieldEvidence, matching_evidence.id) is not None
    assert db_session.get(FieldEvidence, mismatch_evidence.id) is not None
    assert db_session.get(FieldEvidence, selected_evidence.id) is not None


def test_candidate_title_cleanup_migration_downgrade_fails_explicitly():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "6a1c4f2b9d30_clear_unreviewed_candidate_titles.py"
    )
    spec = spec_from_file_location("candidate_title_cleanup_downgrade", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    with pytest.raises(RuntimeError, match="irreversible.*cannot be restored"):
        migration.downgrade()


def _add_category_and_source(session, slug="review-category"):
    competition = Competition(title="Category review competition")
    category = Category(name="Review Category", slug=slug)
    session.add_all([competition, category])
    session.flush()
    source, _ = _add_source_and_evidence(
        session,
        competition,
        value={"title": "Category review competition"},
        review_state="selected",
        suffix=f"category-source-{slug}",
    )
    return competition, category, source


def test_category_proposal_creation_uses_existing_slug_and_is_reviewable(db_session):
    competition, category, source = _add_category_and_source(db_session)

    proposal = create_category_proposal(
        db_session, competition.id, category.slug, source.id
    )
    pending = list_pending_evidence(db_session, field_name="category")

    assert proposal.field_name == "category"
    assert proposal.field_key == category.slug
    assert proposal.observed_value["slug"] == category.slug
    listed = next(item for item in pending if item["evidence_id"] == proposal.id)
    assert listed["category_name"] == category.name
    assert listed["category_slug"] == category.slug


def test_unknown_category_slug_cannot_be_proposed_or_accepted(db_session):
    competition = Competition(title="No category competition")
    db_session.add(competition)
    db_session.flush()
    source, _ = _add_source_and_evidence(
        db_session, competition, value={"title": competition.title}, suffix="unknown-cat"
    )

    with pytest.raises(ReviewError, match="does not exist"):
        create_category_proposal(db_session, competition.id, "unknown-slug", source.id)
    assert db_session.scalar(
        select(func.count())
        .select_from(FieldEvidence)
        .where(
            FieldEvidence.competition_id == competition.id,
            FieldEvidence.field_name == "category",
        )
    ) == 0

    proposal = FieldEvidence(
        competition_id=competition.id,
        source_id=source.id,
        field_name="category",
        field_key="unknown-slug",
        observed_value={"slug": "unknown-slug"},
    )
    db_session.add(proposal)
    db_session.flush()
    with pytest.raises(ReviewError, match="does not exist"):
        accept_evidence(db_session, proposal.id)
    db_session.refresh(proposal)
    assert proposal.review_state == "candidate"


def test_accepting_category_proposal_assigns_and_selects_evidence(db_session):
    competition, category, source = _add_category_and_source(db_session)
    proposal = create_category_proposal(db_session, competition.id, category.slug, source.id)

    accepted = accept_evidence(db_session, proposal.id)

    link = db_session.get(CompetitionCategory, (competition.id, category.id))
    assert accepted.review_state == "selected"
    assert link is not None
    assert link.is_primary is False


def test_repeated_category_approval_supersedes_evidence_without_duplicate_link(db_session):
    competition, category, source = _add_category_and_source(db_session)
    first = create_category_proposal(db_session, competition.id, category.slug, source.id)
    second = create_category_proposal(db_session, competition.id, category.slug, source.id)

    accept_evidence(db_session, first.id)
    with pytest.raises(ReviewError, match="already selected"):
        accept_evidence(db_session, first.id)
    accept_evidence(db_session, second.id)

    db_session.refresh(first)
    assert first.review_state == "superseded"
    assert second.review_state == "selected"
    assert db_session.scalar(
        select(func.count())
        .select_from(CompetitionCategory)
        .where(
            CompetitionCategory.competition_id == competition.id,
            CompetitionCategory.category_id == category.id,
        )
    ) == 1


def test_rejected_category_proposal_does_not_assign_category(db_session):
    competition, category, source = _add_category_and_source(db_session)
    proposal = create_category_proposal(db_session, competition.id, category.slug, source.id)

    rejected = reject_evidence(db_session, proposal.id)

    assert rejected.review_state == "rejected"
    assert db_session.get(CompetitionCategory, (competition.id, category.id)) is None


def test_failed_category_assignment_rolls_back_relationship_and_evidence(db_session, monkeypatch):
    competition, category, source = _add_category_and_source(db_session)
    proposal = create_category_proposal(db_session, competition.id, category.slug, source.id)
    original_flush = db_session.flush
    flush_count = 0

    def fail_assignment_flush(objects=None):
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            raise RuntimeError("simulated assignment flush failure")
        return original_flush(objects=objects)

    monkeypatch.setattr(db_session, "flush", fail_assignment_flush)
    with pytest.raises(RuntimeError, match="assignment flush"):
        accept_evidence(db_session, proposal.id)
    monkeypatch.setattr(db_session, "flush", original_flush)
    db_session.expire_all()

    assert db_session.get(CompetitionCategory, (competition.id, category.id)) is None
    assert db_session.get(FieldEvidence, proposal.id).review_state == "candidate"


def test_competition_can_have_multiple_category_proposals_and_assignments(db_session):
    competition, first_category, source = _add_category_and_source(db_session, "first-category")
    second_category = Category(name="Second Category", slug="second-category")
    db_session.add(second_category)
    db_session.flush()
    first = create_category_proposal(
        db_session, competition.id, first_category.slug, source.id
    )
    second = create_category_proposal(
        db_session, competition.id, second_category.slug, source.id
    )

    accept_evidence(db_session, first.id)
    accept_evidence(db_session, second.id)

    links = db_session.scalars(
        select(CompetitionCategory).where(CompetitionCategory.competition_id == competition.id)
    ).all()
    assert {link.category_id for link in links} == {first_category.id, second_category.id}
    assert first.field_key != second.field_key


def test_unassigning_category_removes_link_and_supersedes_approval(db_session):
    competition, category, source = _add_category_and_source(db_session)
    proposal = create_category_proposal(db_session, competition.id, category.slug, source.id)
    accept_evidence(db_session, proposal.id)

    unassign_category(db_session, competition.id, category.slug)

    db_session.refresh(proposal)
    assert db_session.get(CompetitionCategory, (competition.id, category.id)) is None
    assert proposal.review_state == "superseded"


def test_category_primary_uniqueness_is_respected(db_session):
    competition, first_category, source = _add_category_and_source(db_session, "primary-one")
    second_category = Category(name="Second Primary", slug="primary-two")
    db_session.add(second_category)
    db_session.flush()
    first = create_category_proposal(
        db_session, competition.id, first_category.slug, source.id, is_primary=True
    )
    second = create_category_proposal(
        db_session, competition.id, second_category.slug, source.id, is_primary=True
    )

    accept_evidence(db_session, first.id)
    with pytest.raises(ReviewError, match="already has a primary category"):
        accept_evidence(db_session, second.id)

    db_session.refresh(second)
    assert second.review_state == "candidate"
    assert db_session.get(CompetitionCategory, (competition.id, first_category.id)).is_primary
    assert db_session.get(CompetitionCategory, (competition.id, second_category.id)) is None
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                CompetitionCategory(
                    competition_id=competition.id,
                    category_id=second_category.id,
                    is_primary=True,
                )
            )
            db_session.flush()

