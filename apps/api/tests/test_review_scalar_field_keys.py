import pytest

from app.review.service import (
    _DATE_FIELD_COLUMNS,
    _SCALAR_FIELD_MAPPINGS,
    ReviewError,
    UnsupportedEvidenceField,
    _scalar_mapping,
    _validate_scalar_field_key,
)

SCALAR_FIELDS = set(_SCALAR_FIELD_MAPPINGS) | set(_DATE_FIELD_COLUMNS)


@pytest.mark.parametrize("field_name", sorted(SCALAR_FIELDS))
def test_supported_scalar_fields_accept_only_the_empty_key(field_name):
    _validate_scalar_field_key(field_name, "")

    with pytest.raises(ReviewError, match="must have an empty field_key"):
        _validate_scalar_field_key(field_name, "alternate")


def test_category_key_is_not_constrained_by_scalar_key_rule():
    _validate_scalar_field_key("category", "ai-machine-learning")


@pytest.mark.parametrize("field_name", ["organizer", "topic", "prize", "registration_status"])
def test_unsupported_fields_keep_existing_unsupported_acceptance_behavior(field_name):
    _validate_scalar_field_key(field_name, "existing-key")
    with pytest.raises(UnsupportedEvidenceField):
        _scalar_mapping(field_name)
