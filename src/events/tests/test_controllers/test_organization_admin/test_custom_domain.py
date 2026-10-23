"""Tests for the org-admin custom-domain endpoints (#1151). Cloudflare is mocked."""

import typing as t
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse

from events.exceptions import CloudflareAPIError
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
CNAME_TARGET = "customers.letsrevel.io"


def _url(name: str, org: Organization) -> str:
    return reverse(f"api:{name}", kwargs={"slug": org.slug})


def hosted(status: str = "pending", ssl_status: str = "pending_validation") -> cloudflare.CustomHostname:
    return cloudflare.CustomHostname(
        id="cf-1",
        hostname="events.example.com",
        status=status,
        ssl=cloudflare.CustomHostnameSsl(status=ssl_status),
        ownership_verification=cloudflare.OwnershipVerification(
            type="txt", name="_cf-custom-hostname.events.example.com", value="owner-token"
        ),
    )


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    settings.CUSTOM_DOMAIN_CNAME_TARGET = CNAME_TARGET
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"


@pytest.fixture
def enabled_org(organization: Organization) -> Organization:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return organization


# ---- GET ----


def test_get_returns_null_when_no_domain(organization_owner_client: Client, enabled_org: Organization) -> None:
    response = organization_owner_client.get(_url("get_custom_domain", enabled_org))

    assert response.status_code == 200
    assert response.json() is None


def test_get_returns_domain_with_dns_instructions(organization_owner_client: Client, enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(
        organization=enabled_org,
        hostname="events.example.com",
        verification_records=[{"type": "CNAME", "name": "events.example.com", "value": CNAME_TARGET}],
    )

    response = organization_owner_client.get(_url("get_custom_domain", enabled_org))

    assert response.status_code == 200
    assert response.json() == {
        "hostname": "events.example.com",
        "status": "pending",
        "verification_records": [{"type": "CNAME", "name": "events.example.com", "value": CNAME_TARGET}],
        "cname_target": CNAME_TARGET,
        "verified_at": None,
        "errors": [],
    }


def test_get_exposes_the_problems_cloudflare_reported(
    organization_owner_client: Client, enabled_org: Organization
) -> None:
    OrganizationDomain.objects.create(
        organization=enabled_org, hostname="events.example.com", last_errors=["CNAME does not point to the origin"]
    )

    response = organization_owner_client.get(_url("get_custom_domain", enabled_org))

    assert response.status_code == 200
    assert response.json()["errors"] == ["CNAME does not point to the origin"]


def test_get_hides_removed_domains(organization_owner_client: Client, enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(organization=enabled_org, hostname="gone.example.com", status=Status.REMOVED)

    response = organization_owner_client.get(_url("get_custom_domain", enabled_org))

    assert response.json() is None


# ---- POST ----


def test_request_creates_pending_domain(organization_owner_client: Client, enabled_org: Organization) -> None:
    with patch.object(cloudflare, "create_custom_hostname", return_value=hosted()) as create:
        response = organization_owner_client.post(
            _url("request_custom_domain", enabled_org),
            data={"hostname": "Events.Example.com"},
            content_type="application/json",
        )

    assert response.status_code == 201, response.content
    create.assert_called_once_with("events.example.com")
    body = response.json()
    assert body["hostname"] == "events.example.com"
    assert body["status"] == "pending"
    assert body["cname_target"] == CNAME_TARGET
    assert {"type": "CNAME", "name": "events.example.com", "value": CNAME_TARGET} in body["verification_records"]
    assert {
        "type": "TXT",
        "name": "_cf-custom-hostname.events.example.com",
        "value": "owner-token",
    } in body["verification_records"]
    assert OrganizationDomain.objects.get(hostname="events.example.com").cloudflare_hostname_id == "cf-1"


def test_request_invalid_hostname_is_400(organization_owner_client: Client, enabled_org: Organization) -> None:
    response = organization_owner_client.post(
        _url("request_custom_domain", enabled_org),
        data={"hostname": "letsrevel.io"},
        content_type="application/json",
    )

    assert response.status_code == 400


def test_request_duplicate_is_409(organization_owner_client: Client, enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(organization=enabled_org, hostname="old.example.com")

    response = organization_owner_client.post(
        _url("request_custom_domain", enabled_org),
        data={"hostname": "events.example.com"},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "This organization already has a pending or active domain."


def test_request_cloudflare_failure_is_502_without_provider_detail(
    organization_owner_client: Client, enabled_org: Organization
) -> None:
    with patch.object(cloudflare, "create_custom_hostname", side_effect=CloudflareAPIError("secret detail")):
        response = organization_owner_client.post(
            _url("request_custom_domain", enabled_org),
            data={"hostname": "events.example.com"},
            content_type="application/json",
        )

    assert response.status_code == 502
    assert "secret detail" not in response.content.decode()
    assert not OrganizationDomain.objects.exists()


# ---- refresh ----


def test_refresh_syncs_and_activates(organization_owner_client: Client, enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(
        organization=enabled_org, hostname="events.example.com", cloudflare_hostname_id="cf-1"
    )

    with patch.object(cloudflare, "get_custom_hostname", return_value=hosted("active", "active")):
        response = organization_owner_client.post(_url("refresh_custom_domain", enabled_org))

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "active"
    enabled_org.refresh_from_db()
    assert enabled_org.custom_domain == "events.example.com"


def test_refresh_without_domain_is_404(organization_owner_client: Client, enabled_org: Organization) -> None:
    response = organization_owner_client.post(_url("refresh_custom_domain", enabled_org))

    assert response.status_code == 404


# ---- DELETE ----


def test_delete_removes_domain(organization_owner_client: Client, enabled_org: Organization) -> None:
    domain = OrganizationDomain.objects.create(
        organization=enabled_org, hostname="events.example.com", cloudflare_hostname_id="cf-1"
    )

    with patch.object(cloudflare, "delete_custom_hostname") as delete:
        response = organization_owner_client.delete(_url("delete_custom_domain", enabled_org))

    assert response.status_code == 204
    delete.assert_called_once_with("cf-1")
    domain.refresh_from_db()
    assert domain.status == Status.REMOVED


def test_delete_without_domain_is_404(organization_owner_client: Client, enabled_org: Organization) -> None:
    response = organization_owner_client.delete(_url("delete_custom_domain", enabled_org))

    assert response.status_code == 404


# ---- permissions and kill switch ----

ENDPOINTS = [
    ("get", "get_custom_domain"),
    ("post", "request_custom_domain"),
    ("post", "refresh_custom_domain"),
    ("delete", "delete_custom_domain"),
]


@pytest.mark.parametrize(("method", "name"), ENDPOINTS)
@pytest.mark.parametrize("client_fixture", ["organization_staff_client", "member_client", "nonmember_client"])
def test_only_the_owner_may_use_the_endpoints(
    request: pytest.FixtureRequest, enabled_org: Organization, method: str, name: str, client_fixture: str
) -> None:
    client: Client = request.getfixturevalue(client_fixture)
    kwargs: dict[str, t.Any] = {}
    if name == "request_custom_domain":
        kwargs = {"data": {"hostname": "events.example.com"}, "content_type": "application/json"}

    response = getattr(client, method)(_url(name, enabled_org), **kwargs)

    assert response.status_code in (403, 404)
    assert not OrganizationDomain.objects.exists()


@pytest.mark.parametrize(("method", "name"), ENDPOINTS)
def test_kill_switch_off_is_403(
    organization_owner_client: Client, organization: Organization, method: str, name: str
) -> None:
    kwargs: dict[str, t.Any] = {}
    if name == "request_custom_domain":
        kwargs = {"data": {"hostname": "events.example.com"}, "content_type": "application/json"}

    response = getattr(organization_owner_client, method)(_url(name, organization), **kwargs)

    assert response.status_code == 403
    assert response.json()["detail"] == "Custom domains are not enabled for this organization."


# ---- admin detail schema ----


@pytest.mark.parametrize("enabled", [True, False])
def test_admin_detail_exposes_custom_domain_enabled(
    organization_owner_client: Client, organization: Organization, enabled: bool
) -> None:
    organization.custom_domain_enabled = enabled
    organization.save(update_fields=["custom_domain_enabled"])

    response = organization_owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    assert response.json()["custom_domain_enabled"] is enabled


# ---- Admin detail payload advertises the active domain ("view public site") ----


def test_admin_detail_exposes_custom_domain_only_while_the_gate_is_on(
    organization_owner_client: Client, enabled_org: Organization
) -> None:
    domain = OrganizationDomain.objects.create(organization=enabled_org, hostname="events.example.com")
    custom_domain_service.activate_domain(domain)
    url = reverse("api:get_organization_admin", kwargs={"slug": enabled_org.slug})

    assert organization_owner_client.get(url).json()["custom_domain"] == "events.example.com"

    enabled_org.custom_domain_enabled = False
    enabled_org.save(update_fields=["custom_domain_enabled"])
    assert organization_owner_client.get(url).json()["custom_domain"] is None


def test_admin_detail_custom_domain_is_null_without_an_active_domain(
    organization_owner_client: Client, enabled_org: Organization
) -> None:
    url = reverse("api:get_organization_admin", kwargs={"slug": enabled_org.slug})

    assert organization_owner_client.get(url).json()["custom_domain"] is None
