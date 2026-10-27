"""Model validation for ``Organization.branding`` (#1149)."""

import pytest
from django.core.exceptions import ValidationError

from events.models import Organization
from events.utils.branding import OrganizationBranding, parse_branding

pytestmark = pytest.mark.django_db


def test_unbranded_organization_stores_empty_branding(organization: Organization) -> None:
    assert organization.branding == {"primary_color": None, "accent_color": None}
    assert Organization._meta.get_field("branding").get_default() == {}


def test_normalizes_to_uppercase_and_fills_defaults_on_save(organization: Organization) -> None:
    organization.branding = {"primary_color": "#8c3cdd"}
    organization.save()
    organization.refresh_from_db()
    assert organization.branding == {"primary_color": "#8C3CDD", "accent_color": None}


def test_none_branding_is_treated_as_empty(organization: Organization) -> None:
    organization.branding = None
    organization.clean()
    assert organization.branding == {"primary_color": None, "accent_color": None}


@pytest.mark.parametrize("bad", ["8C3CDD", "#8C3CD", "#8C3CDDD", "#GGGGGG", "red", " #8C3CDD", "#8C3CDD ", ""])
def test_rejects_invalid_hex(organization: Organization, bad: str) -> None:
    organization.branding = {"accent_color": bad}
    with pytest.raises(ValidationError) as exc_info:
        organization.save()
    assert "branding" in exc_info.value.message_dict


@pytest.mark.parametrize("bad", [{"unknown": 1}, ["#8C3CDD"], "nope", {"primary_color": 5}])
def test_rejects_unknown_keys_and_wrong_shapes(organization: Organization, bad: object) -> None:
    organization.branding = bad
    with pytest.raises(ValidationError) as exc_info:
        organization.save()
    assert "branding" in exc_info.value.message_dict


def test_parse_branding_valid() -> None:
    assert parse_branding({"primary_color": "#8c3cdd"}) == OrganizationBranding(primary_color="#8C3CDD")


@pytest.mark.parametrize("raw", [None, {}, {"primary_color": "nope"}, {"x": 1}, [], "str", 3])
def test_parse_branding_is_tolerant(raw: object) -> None:
    result = parse_branding(raw)
    if raw is None or raw == {}:
        assert result == OrganizationBranding()
    assert result.primary_color is None
    assert result.accent_color is None
