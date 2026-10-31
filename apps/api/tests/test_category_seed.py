from collections.abc import Generator
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.category_seed import CATEGORIES, seed_categories
from app.database import engine
from app.models import Category

pytestmark = pytest.mark.integration


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    try:
        connection = engine.connect()
    except SQLAlchemyError:
        pytest.skip("PostgreSQL is unavailable for category seed tests")
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_seed_categories_is_idempotent_and_does_not_duplicate(db_session):
    first = seed_categories(db_session)
    second = seed_categories(db_session)

    assert not first["conflicts"]
    assert {item["slug"] for item in first["created"] + first["already_existing"]} == {
        slug for _, slug in CATEGORIES
    }
    assert second["created"] == []
    assert {item["slug"] for item in second["already_existing"]} == {
        slug for _, slug in CATEGORIES
    }
    assert db_session.scalar(
        select(func.count()).select_from(Category).where(
            Category.slug.in_([slug for _, slug in CATEGORIES])
        )
    ) == len(CATEGORIES)


def test_seed_categories_reports_conflicts_without_overwriting(db_session, monkeypatch):
    slug = f"seed-conflict-{uuid4().hex}"
    monkeypatch.setattr("app.category_seed.CATEGORIES", (("Expected name", slug),))
    existing = Category(name="Locally named category", slug=slug)
    db_session.add(existing)
    db_session.flush()

    result = seed_categories(db_session)

    assert result["conflicts"] == [
        {
            "name": "Expected name",
            "slug": slug,
            "existing_name": "Locally named category",
            "existing_slug": slug,
        }
    ]
    db_session.refresh(existing)
    assert existing.name == "Locally named category"
