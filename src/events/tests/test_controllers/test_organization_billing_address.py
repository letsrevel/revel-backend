"""Tests for the structured billing address on organizations (billing-info endpoints)."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from common.postal_address import PostalAddress
from events.models import Organization

pytestmark = pytest.mark.django_db


def test_patch_billing_info_sets_structured_address(
    organization_owner_client: Client, organization: Organization
) -> None:
    """PATCH stores the structured address, upper-cases the country and GET returns it."""
    url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})
    payload = {
        "billing_address_line1": "Musterstraße 42",
        "billing_postal_code": "1010",
        "billing_city": "Wien",
        "billing_country_code": "at",
    }

    response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    got = organization_owner_client.get(reverse("api:get_billing_info", kwargs={"slug": organization.slug}))
    assert got.status_code == 200
    assert got.json()["billing_country_code"] == "AT"
    assert got.json()["billing_address_line1"] == "Musterstraße 42"
    organization.refresh_from_db()
    assert organization.billing_postal_address().is_complete


def test_patch_rejects_invalid_country(organization_owner_client: Client, organization: Organization) -> None:
    """An unknown ISO country code is a 422."""
    url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"billing_country_code": "XX"}), content_type="application/json"
    )

    assert response.status_code == 422


def test_patch_without_address_fields_leaves_them_untouched(
    organization_owner_client: Client, organization: Organization
) -> None:
    """A PATCH that omits the address fields does not clear them."""
    organization.billing_city = "Wien"
    organization.billing_country_code = "AT"
    organization.save()
    url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"billing_name": "Acme"}), content_type="application/json"
    )

    assert response.status_code == 200
    organization.refresh_from_db()
    assert organization.billing_city == "Wien"
    assert organization.billing_country_code == "AT"


def test_whitespace_only_address_field_becomes_empty(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Whitespace is stripped, so whitespace-only input clears the field."""
    organization.billing_city = "Wien"
    organization.save()
    url = reverse("api:update_billing_info", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"billing_city": "   "}), content_type="application/json"
    )

    assert response.status_code == 200
    organization.refresh_from_db()
    assert organization.billing_city == ""


def test_billing_postal_address_maps_each_field(organization: Organization) -> None:
    """Each of the six model fields maps to the matching PostalAddress field."""
    organization.billing_address_line1 = "L1"
    organization.billing_address_line2 = "L2"
    organization.billing_postal_code = "PC"
    organization.billing_city = "CITY"
    organization.billing_subdivision = "SUB"
    organization.billing_country_code = "AT"

    assert organization.billing_postal_address() == PostalAddress(
        line1="L1", line2="L2", postal_code="PC", city="CITY", subdivision="SUB", country_code="AT"
    )
