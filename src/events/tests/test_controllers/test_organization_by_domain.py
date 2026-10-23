"""Tests for tenant resolution over HTTP: by-domain and by-id custom-domain routes, org payloads, CORS (#1152)."""

import typing as t
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import resolve, reverse

from events.models import (
    AdditionalResource,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationDomain,
    ResourceVisibility,
)
from events.schema import MinimalOrganizationSchema
from events.service import custom_domain_service, tenant_service
from events.service.tenant_service import invalidate_tenant_cache

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
HOST = "events.example.com"


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts and CORS so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    settings.CORS_ALLOW_ALL_ORIGINS = False
    settings.CORS_ALLOWED_ORIGINS = ["https://app.letsrevel.io"]


@pytest.fixture
def tenant_org(organization: Organization) -> Organization:
    """An enabled organization with an ACTIVE domain (and private visibility: the default)."""
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    domain = OrganizationDomain.objects.create(organization=organization, hostname=HOST)
    custom_domain_service.activate_domain(domain)
    organization.refresh_from_db()
    return organization


def _by_domain(client: Client, hostname: str) -> t.Any:
    return client.get(reverse("api:get_organization_by_domain", kwargs={"hostname": hostname}))


# --- GET /organizations/by-domain/{hostname} ---


def test_by_domain_resolves_active_domain_for_any_visibility(client: Client, tenant_org: Organization) -> None:
    """The hostname is public DNS, so even a private organization resolves."""
    assert tenant_org.visibility == Organization.Visibility.PRIVATE

    response = _by_domain(client, HOST)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(tenant_org.id)
    assert body["slug"] == tenant_org.slug
    assert body["name"] == tenant_org.name
    assert body["custom_domain"] == HOST
    assert body["accept_membership_requests"] is True
    assert body["has_membership_plans"] is False
    assert body["has_resources"] is False
    assert body["branding"]["primary_color"] is None
    assert body["brand_theme"] is None
    for key in ("logo", "cover_art", "logo_thumbnail_url", "cover_art_thumbnail_url", "cover_art_social_url"):
        assert key in body


def test_by_domain_exposes_branding_and_derived_brand_theme(client: Client, tenant_org: Organization) -> None:
    """A tenant host gets the stored branding plus the derived light/dark theme to apply app-wide."""
    tenant_org.branding = {"primary_color": "#8C3CDD", "accent_color": "#E4251B"}
    tenant_org.save(update_fields=["branding"])

    body = _by_domain(client, HOST).json()

    assert body["branding"]["primary_color"] == "#8C3CDD"
    assert body["branding"]["accent_color"] == "#E4251B"
    assert set(body["brand_theme"]) == {"light", "dark"}
    assert body["brand_theme"]["light"] != body["brand_theme"]["dark"]


def test_by_domain_is_case_insensitive(client: Client, tenant_org: Organization) -> None:
    """Hostnames are matched lowercase."""
    assert _by_domain(client, "Events.EXAMPLE.com").status_code == 200


def test_by_domain_has_membership_plans_counts_active_plans_only(client: Client, tenant_org: Organization) -> None:
    """Archived plans do not make the membership nav appear."""
    tier = MembershipTier.objects.get(organization=tenant_org, name="General membership")
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name="Old", price=Decimal("10.00"), currency="EUR", period_unit="month", is_active=False
    )
    assert _by_domain(client, HOST).json()["has_membership_plans"] is False

    plan.is_active = True
    plan.save(update_fields=["is_active"])
    assert _by_domain(client, HOST).json()["has_membership_plans"] is True


def test_by_domain_has_resources_counts_public_displayed_resources_only(
    client: Client, tenant_org: Organization
) -> None:
    """Members-only, private or hidden-from-page resources do not count."""
    for visibility, display in [
        (ResourceVisibility.MEMBERS_ONLY, True),
        (ResourceVisibility.PRIVATE, True),
        (ResourceVisibility.PUBLIC, False),
    ]:
        AdditionalResource.objects.create(
            organization=tenant_org,
            name="r",
            resource_type="text",
            text="x",
            visibility=visibility,
            display_on_organization_page=display,
        )
    assert _by_domain(client, HOST).json()["has_resources"] is False

    AdditionalResource.objects.create(
        organization=tenant_org, name="p", resource_type="text", text="x", visibility=ResourceVisibility.PUBLIC
    )
    assert _by_domain(client, HOST).json()["has_resources"] is True


@pytest.mark.parametrize("terminal", [Status.FAILED, Status.REMOVED])
def test_by_domain_404_after_domain_leaves_active(
    client: Client,
    tenant_org: Organization,
    terminal: OrganizationDomain.Status,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Failed and removed domains stop resolving as soon as the cache is invalidated."""
    domain = tenant_org.domains.get()
    assert _by_domain(client, HOST).status_code == 200

    with django_capture_on_commit_callbacks(execute=True):
        if terminal == Status.FAILED:
            custom_domain_service.mark_domain_failed(domain)
        else:
            custom_domain_service.remove_domain(domain)

    assert _by_domain(client, HOST).status_code == 404


def test_by_domain_404_for_pending_domain(client: Client, organization: Organization) -> None:
    """A pending domain is not resolvable; custom_domain is active-only."""
    OrganizationDomain.objects.create(organization=organization, hostname="pending.example.com")

    assert _by_domain(client, "pending.example.com").status_code == 404


@pytest.mark.parametrize("hostname", ["unknown.example.com", "nodots", "a.com:8080", "x" * 300])
def test_by_domain_404_for_unknown_hosts(client: Client, hostname: str) -> None:
    """Unknown or nonsensical hostnames are plain 404s."""
    assert _by_domain(client, hostname).status_code == 404


def test_by_domain_does_not_collide_with_slug_routes(client: Client, tenant_org: Organization) -> None:
    """``/organizations/by-domain/...`` is its own route; slug routes keep working."""
    tenant_org.visibility = Organization.Visibility.PUBLIC
    tenant_org.save(update_fields=["visibility"])

    by_domain = client.get(f"/api/organizations/by-domain/{HOST}")
    by_slug = client.get(f"/api/organizations/{tenant_org.slug}")
    plans_by_slug = client.get(f"/api/organizations/{tenant_org.slug}/membership-plans")
    # "membership-plans" as a hostname must not be swallowed by /{slug}/membership-plans.
    shadow = client.get("/api/organizations/by-domain/membership-plans")

    assert by_domain.status_code == 200
    assert by_domain.json()["slug"] == tenant_org.slug
    assert by_slug.status_code == 200
    assert by_slug.json()["slug"] == tenant_org.slug
    assert plans_by_slug.status_code == 200
    assert shadow.status_code == 404
    assert shadow.json() == {"detail": "Organization not found."}


# --- custom_domain on the organization payloads ---


def test_custom_domain_on_retrieve_and_list_schemas(client: Client, tenant_org: Organization) -> None:
    """Active hostnames show up on the retrieve and list payloads."""
    tenant_org.visibility = Organization.Visibility.PUBLIC
    tenant_org.save(update_fields=["visibility"])

    retrieved = client.get(reverse("api:get_organization", kwargs={"slug": tenant_org.slug})).json()
    listed = client.get(reverse("api:list_organizations")).json()["results"]

    assert retrieved["custom_domain"] == HOST
    assert [o["custom_domain"] for o in listed if o["id"] == str(tenant_org.id)] == [HOST]


def test_custom_domain_is_none_without_an_active_domain(client: Client, organization: Organization) -> None:
    """Pending requests are never exposed."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    OrganizationDomain.objects.create(organization=organization, hostname="pending.example.com")

    retrieved = client.get(reverse("api:get_organization", kwargs={"slug": organization.slug})).json()

    assert retrieved["custom_domain"] is None


def test_custom_domain_on_minimal_schema(tenant_org: Organization, organization: Organization) -> None:
    """MinimalOrganizationSchema reads the plain column."""
    assert MinimalOrganizationSchema.from_orm(tenant_org).custom_domain == HOST
    tenant_org.custom_domain = None
    assert MinimalOrganizationSchema.from_orm(tenant_org).custom_domain is None


def test_custom_domain_hidden_on_minimal_schema_when_gate_off(tenant_org: Organization) -> None:
    """The kill switch hides the hostname even while the column still holds it."""
    tenant_org.custom_domain_enabled = False

    assert tenant_org.custom_domain == HOST
    assert MinimalOrganizationSchema.from_orm(tenant_org).custom_domain is None


# --- Platform kill switch: custom_domain_enabled off hides the domain everywhere ---


def _set_gate(organization: Organization, enabled: bool) -> None:
    """Flip the platform gate and drop the cached resolution, as the admin's save_model does on commit."""
    organization.custom_domain_enabled = enabled
    organization.save(update_fields=["custom_domain_enabled"])
    invalidate_tenant_cache(HOST)


def test_kill_switch_off_stops_resolution_advertising_and_cors(client: Client, tenant_org: Organization) -> None:
    """Disabling the gate stops the active domain resolving, being advertised and being an allowed origin."""
    tenant_org.visibility = Organization.Visibility.PUBLIC
    tenant_org.save(update_fields=["visibility"])
    assert _by_domain(client, HOST).status_code == 200  # warms the cache with a positive answer
    assert "Access-Control-Allow-Origin" in _preflight(client, f"https://{HOST}")

    _set_gate(tenant_org, False)

    assert _by_domain(client, HOST).status_code == 404
    retrieved = client.get(reverse("api:get_organization", kwargs={"slug": tenant_org.slug})).json()
    assert retrieved["custom_domain"] is None
    listed = client.get(reverse("api:list_organizations")).json()["results"]
    assert [o["custom_domain"] for o in listed if o["id"] == str(tenant_org.id)] == [None]
    assert "Access-Control-Allow-Origin" not in _preflight(client, f"https://{HOST}")


def test_kill_switch_back_on_restores_resolution_advertising_and_cors(client: Client, tenant_org: Organization) -> None:
    """Re-enabling the gate brings the existing active domain back with no other action."""
    tenant_org.visibility = Organization.Visibility.PUBLIC
    tenant_org.save(update_fields=["visibility"])
    _set_gate(tenant_org, False)
    assert _by_domain(client, HOST).status_code == 404

    _set_gate(tenant_org, True)

    by_domain = _by_domain(client, HOST)
    assert by_domain.status_code == 200
    assert by_domain.json()["custom_domain"] == HOST
    retrieved = client.get(reverse("api:get_organization", kwargs={"slug": tenant_org.slug})).json()
    assert retrieved["custom_domain"] == HOST
    listed = client.get(reverse("api:list_organizations")).json()["results"]
    assert [o["custom_domain"] for o in listed if o["id"] == str(tenant_org.id)] == [HOST]
    assert "Access-Control-Allow-Origin" in _preflight(client, f"https://{HOST}")


# --- Dynamic CORS ---


def _preflight(client: Client, origin: str) -> t.Any:
    return client.options(
        "/api/organizations/",
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
    )


def test_cors_preflight_allowed_for_active_tenant_origin(client: Client, tenant_org: Organization) -> None:
    """An active tenant origin gets the normal CORS headers, credentials and allowed headers included."""
    response = _preflight(client, f"https://{HOST}")

    assert response["Access-Control-Allow-Origin"] == f"https://{HOST}"
    assert response["Access-Control-Allow-Credentials"] == "true"
    assert "authorization" in response["Access-Control-Allow-Headers"]
    assert "x-org-token" in response["Access-Control-Allow-Headers"]


def test_cors_allowed_on_actual_request_and_ignores_port(client: Client, tenant_org: Organization) -> None:
    """The check applies to normal requests too, and the origin port is ignored."""
    response = client.get("/api/organizations/", HTTP_ORIGIN=f"https://{HOST}:8443")

    assert response["Access-Control-Allow-Origin"] == f"https://{HOST}:8443"
    assert response["Access-Control-Allow-Credentials"] == "true"


def test_cors_denied_for_unknown_origin(client: Client, tenant_org: Organization) -> None:
    """Origins that are not tenant domains stay blocked."""
    assert "Access-Control-Allow-Origin" not in _preflight(client, "https://evil.example.org")
    assert "Access-Control-Allow-Origin" not in _preflight(client, "null")


def test_cors_denied_for_pending_tenant_origin(client: Client, organization: Organization) -> None:
    """A pending domain is not an allowed origin."""
    OrganizationDomain.objects.create(organization=organization, hostname="pending.example.com")

    assert "Access-Control-Allow-Origin" not in _preflight(client, "https://pending.example.com")


def test_cors_denied_after_domain_removed(
    client: Client, tenant_org: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Removal takes effect once the cache entry is invalidated."""
    assert "Access-Control-Allow-Origin" in _preflight(client, f"https://{HOST}")

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.remove_domain(tenant_org.domains.get())

    assert "Access-Control-Allow-Origin" not in _preflight(client, f"https://{HOST}")


def test_cors_http_origin_denied_unless_debug(client: Client, tenant_org: Organization, settings: t.Any) -> None:
    """Tenant origins must be https; plain http only passes in DEBUG (dev/E2E)."""
    settings.DEBUG = False
    assert "Access-Control-Allow-Origin" not in _preflight(client, f"http://{HOST}")

    settings.DEBUG = True
    assert _preflight(client, f"http://{HOST}")["Access-Control-Allow-Origin"] == f"http://{HOST}"


def test_cors_still_allows_configured_origins(client: Client, tenant_org: Organization) -> None:
    """The static allow-list keeps working next to tenant origins."""
    assert _preflight(client, "https://app.letsrevel.io")["Access-Control-Allow-Origin"] == "https://app.letsrevel.io"


# --- Cache fails open ---


@pytest.fixture
def broken_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every tenant cache operation raise, as an unreachable Redis would (other cache users unaffected)."""
    boom = ConnectionError("cache down")
    monkeypatch.setattr(
        tenant_service,
        "cache",
        Mock(get=Mock(side_effect=boom), set=Mock(side_effect=boom), delete=Mock(side_effect=boom)),
    )


@pytest.mark.usefixtures("broken_cache")
def test_by_domain_resolves_from_the_db_when_the_cache_is_down(client: Client, tenant_org: Organization) -> None:
    """A failed cache read counts as a miss and a failed write is skipped."""
    response = _by_domain(client, HOST)

    assert response.status_code == 200
    assert response.json()["id"] == str(tenant_org.pk)
    assert _by_domain(client, "unknown.example.com").status_code == 404


@pytest.mark.usefixtures("broken_cache")
def test_cors_answers_correctly_when_the_cache_is_down(client: Client, tenant_org: Organization) -> None:
    """The CORS receiver falls back to the DB: tenants allowed, strangers still blocked."""
    assert _preflight(client, f"https://{HOST}")["Access-Control-Allow-Origin"] == f"https://{HOST}"
    assert "Access-Control-Allow-Origin" not in _preflight(client, "https://evil.example.org")


@pytest.mark.usefixtures("broken_cache")
def test_invalidation_does_not_raise_when_the_cache_is_down(
    tenant_org: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A failed ``cache.delete`` is skipped, so the on_commit hook of a domain transition cannot blow up."""
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.remove_domain(tenant_org.domains.get())

    tenant_org.refresh_from_db()
    assert tenant_org.custom_domain is None
    assert tenant_service.get_tenant_organization(HOST) is None


# --- GET /organizations/{uuid}/custom-domain (anonymous, any visibility: the Stripe return flow) ---


def _custom_domain_by_id(client: Client, organization_id: t.Any) -> t.Any:
    return client.get(reverse("api:get_organization_custom_domain", kwargs={"organization_id": organization_id}))


@pytest.mark.parametrize(
    "visibility",
    [Organization.Visibility.PUBLIC, Organization.Visibility.PRIVATE, Organization.Visibility.MEMBERS_ONLY],
)
def test_custom_domain_by_id_resolves_anonymously_for_any_visibility(
    client: Client, tenant_org: Organization, visibility: Organization.Visibility
) -> None:
    """The client has no session on the platform host, and the hostname is public DNS: any visibility resolves."""
    tenant_org.visibility = visibility
    tenant_org.save(update_fields=["visibility"])

    response = _custom_domain_by_id(client, tenant_org.id)

    assert response.status_code == 200
    assert response.json() == {"custom_domain": HOST}


def test_custom_domain_by_id_404_without_an_active_domain(client: Client, organization: Organization) -> None:
    """Enabled but no domain, and a pending domain, both 404."""
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    assert _custom_domain_by_id(client, organization.id).status_code == 404

    OrganizationDomain.objects.create(organization=organization, hostname="pending.example.com")
    response = _custom_domain_by_id(client, organization.id)

    assert response.status_code == 404
    assert response.json() == {"detail": "Organization not found."}


def test_custom_domain_by_id_404_when_kill_switch_off(client: Client, tenant_org: Organization) -> None:
    """The platform kill switch hides the hostname even though the active domain is still stored."""
    tenant_org.custom_domain_enabled = False
    tenant_org.save(update_fields=["custom_domain_enabled"])

    assert _custom_domain_by_id(client, tenant_org.id).status_code == 404


def test_custom_domain_by_id_404_for_unknown_uuid(client: Client) -> None:
    """An unknown organization id is a plain 404."""
    response = _custom_domain_by_id(client, uuid4())

    assert response.status_code == 404
    assert response.json() == {"detail": "Organization not found."}


def test_custom_domain_by_id_route_is_not_shadowed(client: Client, tenant_org: Organization) -> None:
    """``/{uuid}/custom-domain`` resolves to its own route, and the neighbouring uuid routes keep working."""
    path = f"/api/organizations/{tenant_org.id}/custom-domain"

    assert resolve(path).url_name == "get_organization_custom_domain"
    assert resolve(f"/api/organizations/{tenant_org.id}/logo").url_name == "organization_logo"
    assert resolve(f"/api/organizations/{tenant_org.id}").url_name == "get_organization_by_id"
    assert client.get(path).json() == {"custom_domain": HOST}
    assert client.get(f"/api/organizations/{tenant_org.id}/logo").status_code == 200
