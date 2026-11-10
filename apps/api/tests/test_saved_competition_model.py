from sqlalchemy import BigInteger, DateTime, Uuid

from app.auth.models import User
from app.models import Competition, SavedCompetition


def test_saved_competition_metadata_has_expected_keys_types_and_constraints():
    table = SavedCompetition.__table__

    assert table.primary_key.columns.keys() == ["user_id", "competition_id"]
    assert isinstance(table.c.user_id.type, Uuid)
    assert isinstance(table.c.competition_id.type, BigInteger)
    assert isinstance(table.c.created_at.type, DateTime)
    assert table.c.created_at.type.timezone is True
    assert table.c.created_at.server_default is not None
    assert [key.target_fullname for key in table.c.user_id.foreign_keys] == ["users.id"]
    assert [key.target_fullname for key in table.c.competition_id.foreign_keys] == [
        "competitions.id"
    ]

    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete
        for foreign_key in table.foreign_keys
    }
    assert foreign_keys == {"users.id": "CASCADE", "competitions.id": "CASCADE"}
    assert any(
        index.name == "ix_saved_competitions_user_created_at"
        and [column.name for column in index.columns] == ["user_id", "created_at"]
        for index in table.indexes
    )


def test_saved_competition_relationships_are_registered():
    assert "saved_competitions" in User.__mapper__.relationships
    assert "saved_by_users" in Competition.__mapper__.relationships
    assert "user" in SavedCompetition.__mapper__.relationships
    assert "competition" in SavedCompetition.__mapper__.relationships
