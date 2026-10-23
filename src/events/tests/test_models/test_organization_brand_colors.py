"""Model validation for Organization brand colors (#1149)."""

import pytest
from django.core.exceptions import ValidationError

from events.models import Organization

pytestmark = pytest.mark.django_db


def test_defaults_to_none(organization: Organization) -> None:
    assert organization.brand_primary_color is None
    assert organization.brand_accent_color is None


def test_normalizes_to_uppercase_on_save(organization: Organization) -> None:
    organization.brand_primary_color = "#8c3cdd"
    organization.brand_accent_color = "#f9b233"
    organization.save()
    organization.refresh_from_db()
    assert organization.brand_primary_color == "#8C3CDD"
    assert organization.brand_accent_color == "#F9B233"


def test_empty_string_is_stored_as_none(organization: Organization) -> None:
    organization.brand_primary_color = ""
    organization.save()
    organization.refresh_from_db()
    assert organization.brand_primary_color is None


@pytest.mark.parametrize("bad", ["8C3CDD", "#8C3CD", "#8C3CDDD", "#GGGGGG", "red", " #8C3CDD", "#8C3CDD "])
def test_rejects_invalid_hex(organization: Organization, bad: str) -> None:
    organization.brand_accent_color = bad
    with pytest.raises(ValidationError) as exc_info:
        organization.save()
    assert "brand_accent_color" in exc_info.value.message_dict
