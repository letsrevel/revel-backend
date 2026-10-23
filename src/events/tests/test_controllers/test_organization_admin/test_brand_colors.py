"""Brand color edit/read/preview endpoints (#1149)."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationStaff, PermissionMap, PermissionsSchema

pytestmark = pytest.mark.django_db

JSON = dict[str, t.Any]

EMPTY = {"primary_color": None, "accent_color": None}
HEX_MESSAGE = "Enter a color as a 6-digit hex value, for example #8C3CDD."


def _branding_url(organization: Organization) -> str:
    return reverse("api:update_organization_branding", kwargs={"slug": organization.slug})


def _put_branding(client: Client, organization: Organization, body: JSON) -> tuple[int, JSON]:
    response = client.put(_branding_url(organization), data=orjson.dumps(body), content_type="application/json")
    return response.status_code, response.json()


def test_set_branding_normalizes_and_returns_theme(
    organization_owner_client: Client, organization: Organization
) -> None:
    status, data = _put_branding(
        organization_owner_client, organization, {"primary_color": "#8c3cdd", "accent_color": "#f9b233"}
    )

    assert status == 200
    assert set(data) == {"branding", "brand_theme"}
    assert data["branding"] == {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}
    assert set(data["brand_theme"]) == {"light", "dark"}
    assert set(data["brand_theme"]["light"]) == {"primary", "primary_foreground", "accent", "accent_foreground"}
    organization.refresh_from_db()
    assert organization.branding == {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}


def test_put_replaces_the_whole_object(organization_owner_client: Client, organization: Organization) -> None:
    organization.branding = {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}
    organization.save()

    status, data = _put_branding(organization_owner_client, organization, {"primary_color": "#112233"})

    assert status == 200
    assert data["branding"] == {"primary_color": "#112233", "accent_color": None}
    organization.refresh_from_db()
    assert organization.branding == {"primary_color": "#112233", "accent_color": None}


@pytest.mark.parametrize("body", [{}, {"primary_color": None, "accent_color": None}])
def test_put_clears_branding(organization_owner_client: Client, organization: Organization, body: JSON) -> None:
    organization.branding = {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}
    organization.save()

    status, data = _put_branding(organization_owner_client, organization, body)

    assert status == 200
    assert data["branding"] == {"primary_color": None, "accent_color": None}
    assert data["brand_theme"] is None
    organization.refresh_from_db()
    assert organization.branding == {"primary_color": None, "accent_color": None}


def test_accent_only_has_no_theme(organization_owner_client: Client, organization: Organization) -> None:
    status, data = _put_branding(organization_owner_client, organization, {"accent_color": "#F9B233"})

    assert status == 200
    assert data["branding"]["accent_color"] == "#F9B233"
    assert data["brand_theme"] is None


def test_unknown_key_is_422(organization_owner_client: Client, organization: Organization) -> None:
    status, data = _put_branding(organization_owner_client, organization, {"primary_color": "#8C3CDD", "logo": "x"})

    assert status == 422
    assert any(err["type"] == "extra_forbidden" for err in data["detail"])
    organization.refresh_from_db()
    assert organization.branding == EMPTY


@pytest.mark.parametrize("bad", ["8C3CDD", "#12345", "#GGGGGG", "purple", "#8C3CDD00", ""])
def test_invalid_hex_is_422_with_translated_message(
    organization_owner_client: Client, organization: Organization, bad: str
) -> None:
    status, data = _put_branding(organization_owner_client, organization, {"primary_color": bad})

    assert status == 422
    [error] = data["detail"]
    assert error["type"] == "invalid_hex_color"
    assert error["msg"] == HEX_MESSAGE
    assert error["loc"][-1] == "primary_color"
    organization.refresh_from_db()
    assert organization.branding == EMPTY


def test_invalid_hex_message_is_translated(organization_owner_client: Client, organization: Organization) -> None:
    RevelUser.objects.filter(pk=organization.owner_id).update(language="de")

    response = organization_owner_client.put(
        _branding_url(organization), data=orjson.dumps({"primary_color": "nope"}), content_type="application/json"
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"][0]["msg"] == "Gib eine Farbe als sechsstelligen Hex-Wert ein, zum Beispiel #8C3CDD."
    )


def test_branding_denied_for_non_admin(nonmember_client: Client, organization: Organization) -> None:
    status, _data = _put_branding(nonmember_client, organization, {"primary_color": "#8C3CDD"})

    assert status in {403, 404}
    organization.refresh_from_db()
    assert organization.branding == EMPTY


def test_staff_with_edit_permission_can_set_branding(
    organization_staff_client: Client, organization: Organization
) -> None:
    status, data = _put_branding(organization_staff_client, organization, {"primary_color": "#8C3CDD"})

    assert status == 200
    assert data["branding"]["primary_color"] == "#8C3CDD"


def test_staff_without_edit_permission_is_denied(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    staff_member.permissions = PermissionsSchema(default=PermissionMap(edit_organization=False)).model_dump(mode="json")
    staff_member.save()

    status, _data = _put_branding(organization_staff_client, organization, {"primary_color": "#8C3CDD"})

    assert status == 403
    organization.refresh_from_db()
    assert organization.branding == EMPTY


def test_org_edit_put_no_longer_accepts_brand_fields(
    organization_owner_client: Client, organization: Organization
) -> None:
    url = reverse("api:edit_organization", kwargs={"slug": organization.slug})
    body = {"visibility": "public", "brand_primary_color": "#8C3CDD", "branding": {"primary_color": "#8C3CDD"}}

    response = organization_owner_client.put(url, data=orjson.dumps(body), content_type="application/json")

    assert response.status_code == 200
    organization.refresh_from_db()
    assert organization.branding == EMPTY
    assert "brand_primary_color" not in response.json()
    assert response.json()["branding"] == {"primary_color": None, "accent_color": None}


def test_retrieve_schema_exposes_branding_and_theme(
    organization_owner_client: Client, organization: Organization
) -> None:
    organization.branding = {"primary_color": "#8C3CDD"}
    organization.save()

    response = organization_owner_client.get(reverse("api:get_organization", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    data = response.json()
    assert data["branding"] == {"primary_color": "#8C3CDD", "accent_color": None}
    assert data["brand_theme"]["light"]["accent"] is None
    assert data["brand_theme"]["dark"]["primary"].startswith("#")
    assert "brand_primary_color" not in data


def test_admin_detail_exposes_branding_and_theme(organization_owner_client: Client, organization: Organization) -> None:
    organization.branding = {"primary_color": "#8C3CDD"}
    organization.save()

    response = organization_owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    data = response.json()
    assert data["branding"] == {"primary_color": "#8C3CDD", "accent_color": None}
    assert data["brand_theme"]["dark"]["primary"] == "#A667E5"


def test_nested_minimal_organization_in_event_detail(public_event: Event) -> None:
    org = public_event.organization
    org.branding = {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}
    org.save()

    response = Client().get(reverse("api:get_event", kwargs={"event_id": str(public_event.id)}))

    assert response.status_code == 200
    nested = response.json()["organization"]
    assert nested["branding"] == {"primary_color": "#8C3CDD", "accent_color": "#F9B233"}
    assert nested["brand_theme"]["light"]["accent_foreground"] in {"#000000", "#FFFFFF"}
    assert "brand_primary_color" not in nested


def test_unbranded_org_has_empty_branding_and_null_theme(public_event: Event) -> None:
    response = Client().get(reverse("api:get_event", kwargs={"event_id": str(public_event.id)}))

    nested = response.json()["organization"]
    assert nested["branding"] == {"primary_color": None, "accent_color": None}
    assert nested["brand_theme"] is None


@pytest.mark.parametrize("blob", [{"primary_color": "nope"}, {"unknown": 1}, ["x"]])
def test_invalid_stored_blob_reads_as_empty(
    organization_owner_client: Client, organization: Organization, blob: object
) -> None:
    Organization.objects.filter(pk=organization.pk).update(branding=blob)

    response = organization_owner_client.get(reverse("api:get_organization", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    data = response.json()
    assert data["branding"] == {"primary_color": None, "accent_color": None}
    assert data["brand_theme"] is None


def test_invalid_stored_blob_does_not_break_event_detail(public_event: Event) -> None:
    Organization.objects.filter(pk=public_event.organization_id).update(branding={"primary_color": "nope"})

    response = Client().get(reverse("api:get_event", kwargs={"event_id": str(public_event.id)}))

    assert response.status_code == 200
    assert response.json()["organization"]["brand_theme"] is None


def _preview_url(organization: Organization) -> str:
    return reverse("api:brand_theme_preview", kwargs={"slug": organization.slug})


def test_preview_valid(organization_owner_client: Client, organization: Organization) -> None:
    response = organization_owner_client.get(_preview_url(organization), {"primary": "#8C3CDD", "accent": "#F9B233"})

    assert response.status_code == 200
    data = response.json()
    assert data["light"] == {
        "primary": "#7F26D9",
        "primary_foreground": "#FFFFFF",
        "accent": "#A86D05",
        "accent_foreground": "#000000",
    }
    assert data["dark"]["primary"] == "#A667E5"
    organization.refresh_from_db()
    assert organization.branding == EMPTY  # preview never persists


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
    assert response.json() == {"detail": HEX_MESSAGE}


def test_preview_denied_for_non_admin(nonmember_client: Client, organization: Organization) -> None:
    response = nonmember_client.get(_preview_url(organization), {"primary": "#8C3CDD"})

    assert response.status_code in {403, 404}
