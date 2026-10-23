"""Brand color edit/read/preview endpoints (#1149)."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Organization

pytestmark = pytest.mark.django_db

JSON = dict[str, t.Any]


def _put(client: Client, organization: Organization, **fields: object) -> tuple[int, JSON]:
    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    response = client.put(url, data=orjson.dumps({"visibility": "public", **fields}), content_type="application/json")
    return response.status_code, response.json()


def test_set_colors_normalizes_and_returns_theme(organization_owner_client: Client, organization: Organization) -> None:
    status, data = _put(
        organization_owner_client, organization, brand_primary_color="#8c3cdd", brand_accent_color="#f9b233"
    )

    assert status == 200
    assert data["brand_primary_color"] == "#8C3CDD"
    assert data["brand_accent_color"] == "#F9B233"
    assert set(data["brand_theme"]) == {"light", "dark"}
    assert set(data["brand_theme"]["light"]) == {"primary", "primary_foreground", "accent", "accent_foreground"}
    organization.refresh_from_db()
    assert organization.brand_primary_color == "#8C3CDD"


def test_omitted_fields_are_left_untouched(organization_owner_client: Client, organization: Organization) -> None:
    organization.brand_primary_color = "#8C3CDD"
    organization.save()

    status, data = _put(organization_owner_client, organization, description="x")

    assert status == 200
    assert data["brand_primary_color"] == "#8C3CDD"


def test_explicit_null_clears_colors(organization_owner_client: Client, organization: Organization) -> None:
    organization.brand_primary_color = "#8C3CDD"
    organization.brand_accent_color = "#F9B233"
    organization.save()

    status, data = _put(organization_owner_client, organization, brand_primary_color=None, brand_accent_color=None)

    assert status == 200
    assert data["brand_primary_color"] is None
    assert data["brand_theme"] is None
    refreshed = Organization.objects.get(pk=organization.pk)
    assert refreshed.brand_primary_color is None
    assert refreshed.brand_accent_color is None


@pytest.mark.parametrize("bad", ["8C3CDD", "#12345", "#GGGGGG", "purple", "#8C3CDD00"])
def test_invalid_hex_is_a_clean_400(organization_owner_client: Client, organization: Organization, bad: str) -> None:
    status, data = _put(organization_owner_client, organization, brand_primary_color=bad)

    assert status == 400
    assert "brand_primary_color" in data["errors"]
    organization.refresh_from_db()
    assert organization.brand_primary_color is None


def test_accent_only_has_no_theme(organization_owner_client: Client, organization: Organization) -> None:
    status, data = _put(organization_owner_client, organization, brand_accent_color="#F9B233")

    assert status == 200
    assert data["brand_accent_color"] == "#F9B233"
    assert data["brand_theme"] is None


def test_retrieve_schema_exposes_theme(organization_owner_client: Client, organization: Organization) -> None:
    organization.brand_primary_color = "#8C3CDD"
    organization.save()

    response = organization_owner_client.get(reverse("api:get_organization", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    data = response.json()
    assert data["brand_primary_color"] == "#8C3CDD"
    assert data["brand_accent_color"] is None
    assert data["brand_theme"]["light"]["accent"] is None
    assert data["brand_theme"]["dark"]["primary"].startswith("#")


def test_admin_detail_exposes_theme(organization_owner_client: Client, organization: Organization) -> None:
    organization.brand_primary_color = "#8C3CDD"
    organization.save()

    response = organization_owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    assert response.json()["brand_theme"]["dark"]["primary"] == "#A667E5"


def test_nested_minimal_organization_in_event_detail(public_event: Event) -> None:
    org = public_event.organization
    org.brand_primary_color = "#8C3CDD"
    org.brand_accent_color = "#F9B233"
    org.save()

    response = Client().get(reverse("api:get_event", kwargs={"event_id": str(public_event.id)}))

    assert response.status_code == 200
    nested = response.json()["organization"]
    assert nested["brand_primary_color"] == "#8C3CDD"
    assert nested["brand_accent_color"] == "#F9B233"
    assert nested["brand_theme"]["light"]["accent_foreground"] in {"#000000", "#FFFFFF"}


def test_unbranded_org_has_null_theme(public_event: Event) -> None:
    response = Client().get(reverse("api:get_event", kwargs={"event_id": str(public_event.id)}))

    nested = response.json()["organization"]
    assert nested["brand_primary_color"] is None
    assert nested["brand_theme"] is None


# --- GET /organization-admin/{slug}/brand-theme-preview ---


def _preview_url(organization: Organization) -> str:
    return reverse("api:brand_theme_preview", kwargs={"slug": organization.slug})


def test_preview_valid(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_preview_url(organization), {"primary": "#8C3CDD", "accent": "#F9B233"})

    assert response.status_code == 200
    data = response.json()
    assert data["light"] == {
        "primary": "#8C3CDD",
        "primary_foreground": "#FFFFFF",
        "accent": "#C07D06",
        "accent_foreground": "#000000",
    }
    assert data["dark"]["primary"] == "#A667E5"
    organization.refresh_from_db()
    assert organization.brand_primary_color is None  # preview never persists


def test_preview_accent_only_is_null(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_preview_url(organization), {"accent": "#F9B233"})

    assert response.status_code == 200
    assert response.json() is None


def test_preview_no_params_is_null(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_preview_url(organization))

    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.parametrize("params", [{"primary": "8C3CDD"}, {"primary": "#8C3CDD", "accent": "nope"}])
def test_preview_invalid_hex_is_400(
    organization_owner_client: Client, organization: Organization, params: dict[str, str]
) -> None:
    response = organization_owner_client.get(_preview_url(organization), params)

    assert response.status_code == 400
    assert "detail" in response.json()


def test_preview_denied_for_non_admin(nonmember_client: Client, organization: Organization) -> None:
    response = nonmember_client.get(_preview_url(organization), {"primary": "#8C3CDD"})

    assert response.status_code in {403, 404}
