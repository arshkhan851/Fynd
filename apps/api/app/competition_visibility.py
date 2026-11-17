"""Shared SQL predicates for the existing public competition visibility rule."""

from sqlalchemy.sql.elements import ColumnElement

from app.models import Competition


def public_competition_visibility_predicate() -> ColumnElement[bool]:
    """Return the current behavior-preserving public visibility predicate."""
    return Competition.is_published.is_(True) & Competition.title.is_not(None)
