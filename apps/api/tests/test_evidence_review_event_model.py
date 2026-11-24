from sqlalchemy import DateTime, String, Uuid

from app.models import CompetitionSource, EvidenceReviewEvent


def test_competition_source_registry_identity_is_nullable_and_indexed():
    table = CompetitionSource.__table__

    assert table.c.registry_source_id.nullable is True
    assert isinstance(table.c.registry_source_id.type, String)
    assert any(
        index.name == "ix_competition_sources_registry_source_id"
        and [column.name for column in index.columns] == ["registry_source_id"]
        for index in table.indexes
    )


def test_evidence_review_event_metadata_has_expected_fields_constraints_and_indexes():
    table = EvidenceReviewEvent.__table__

    assert isinstance(table.c.id.type, Uuid)
    assert table.primary_key.columns.keys() == ["id"]
    assert table.c.evidence_id.nullable is False
    assert table.c.reviewer_user_id.nullable is False
    assert isinstance(table.c.action.type, String)
    assert table.c.action.type.length == 16
    assert table.c.previous_state.nullable is False
    assert table.c.new_state.nullable is False
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.server_default is not None

    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete for foreign_key in table.foreign_keys
    }
    assert foreign_keys == {
        "field_evidence.id": "RESTRICT",
        "users.id": "RESTRICT",
    }

    transition = next(
        constraint
        for constraint in table.constraints
        if constraint.name == "ck_evidence_review_events_transition"
    )
    assert "candidate" in str(transition.sqltext)
    assert "selected" in str(transition.sqltext)
    assert "rejected" in str(transition.sqltext)
    assert "superseded" in str(transition.sqltext)

    indexes = {index.name: [column.name for column in index.columns] for index in table.indexes}
    assert indexes == {
        "ix_evidence_review_events_evidence_created": ["evidence_id", "created_at"],
        "ix_evidence_review_events_reviewer_created": ["reviewer_user_id", "created_at"],
    }
