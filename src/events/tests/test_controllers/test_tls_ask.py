"""Tests for Caddy's on-demand TLS ask endpoint, ``GET /api/tls/ask?domain=`` (#1178)."""

import typing as t

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.service import custom_domain_service
from events.service.tenant_service import invalidate_tenant_cache

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
HOST = "events.example.com"


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts and the throttle so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DISABLE_THROTTLING = False


@pytest.fixture
def tenant_org(organization: Organization) -> Organization:
    """An enabled organization with an ACTIVE domain."""
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    domain = OrganizationDomain.objects.create(organization=organization, hostname=HOST)
    custom_domain_service.activate_domain(domain)
    organization.refresh_from_db()
    return organization


def _data_queries(queries: list[dict[str, str]]) -> list[str]:
    """The captured SQL minus the SAVEPOINT bookkeeping that ATOMIC_REQUESTS adds inside the test transaction."""
    return [q["sql"] for q in queries if "SAVEPOINT" not in q["sql"]]


def _ask(client: Client, domain: str | None = HOST) -> t.Any:
    return client.get(reverse("api:tls_ask"), {} if domain is None else {"domain": domain})


def test_ask_200_for_active_enabled_domain(client: Client, tenant_org: Organization) -> None:
    """An active domain of an enabled organization may get a certificate; the body is empty."""
    response = _ask(client)

    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.parametrize("variant", ["Events.EXAMPLE.com", "events.example.com.", "EVENTS.example.COM."])
def test_ask_normalizes_case_and_trailing_dot(client: Client, tenant_org: Organization, variant: str) -> None:
    """Caddy passes the SNI name as received; case and a trailing dot must not matter."""
    assert _ask(client, variant).status_code == 200


def test_ask_path_is_the_one_caddy_points_at() -> None:
    """The ask URL documented for Caddy and configured in the infra repo."""
    assert reverse("api:tls_ask") == "/api/tls/ask"


def test_ask_404_for_unknown_domain(client: Client) -> None:
    """Nobody can make Caddy burn certificate quota on an arbitrary name."""
    assert _ask(client, "unknown.example.com").status_code == 404


def test_ask_404_for_pending_domain(client: Client, organization: Organization) -> None:
    """A pending domain is not served yet, so no certificate."""
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    OrganizationDomain.objects.create(organization=organization, hostname="pending.example.com")

    assert _ask(client, "pending.example.com").status_code == 404


@pytest.mark.parametrize("terminal", [Status.FAILED, Status.REMOVED])
def test_ask_404_after_domain_leaves_active(
    client: Client,
    tenant_org: Organization,
    terminal: OrganizationDomainStatus,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Failed and removed domains stop getting certificates once the cache is invalidated."""
    domain = tenant_org.domains.get()
    assert _ask(client).status_code == 200

    with django_capture_on_commit_callbacks(execute=True):
        if terminal == Status.FAILED:
            custom_domain_service.mark_domain_failed(domain)
        else:
            custom_domain_service.remove_domain(domain)

    assert _ask(client).status_code == 404


def test_ask_404_when_kill_switch_is_off(client: Client, tenant_org: Organization) -> None:
    """With the platform kill switch off the hostname no longer resolves, so no certificate."""
    assert _ask(client).status_code == 200

    tenant_org.custom_domain_enabled = False
    tenant_org.save(update_fields=["custom_domain_enabled"])
    invalidate_tenant_cache(HOST)

    assert _ask(client).status_code == 404


@pytest.mark.parametrize("host", ["app.letsrevel.io", "api.letsrevel.io", "letsrevel.io"])
def test_ask_404_for_platform_hosts(client: Client, tenant_org: Organization, host: str) -> None:
    """The platform's own hosts are never tenant domains."""
    assert _ask(client, host).status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "..",
        "-",
        "a b.example.com",
        "events.example.com:443",
        "https://events.example.com",
        "*.example.com",
        "events.example.com/path",
        "evil_host.example.com",
        "1.2.3.4",
        "events.example.com\nx",
        "%00",
    ],
)
def test_ask_404_for_malformed_input(client: Client, tenant_org: Organization, value: str) -> None:
    """Non-hostnames are refused."""
    assert _ask(client, value).status_code == 404


def test_ask_404_for_missing_param(client: Client, tenant_org: Organization) -> None:
    """No ``domain`` query param is a plain 404, not a validation error."""
    assert _ask(client, None).status_code == 404


def test_ask_404_for_overlong_input_without_touching_the_database(client: Client, tenant_org: Organization) -> None:
    """Over-long input is rejected before any cache or DB access."""
    huge = ("a" * 60 + ".") * 200 + "example.com"

    with CaptureQueriesContext(connection) as queries:
        response = _ask(client, huge)

    assert response.status_code == 404
    assert _data_queries(queries.captured_queries) == []


def test_ask_does_not_leak_anything_beyond_the_status_code(client: Client, tenant_org: Organization) -> None:
    """Hits and misses both have an empty body: no tenant payload, no message."""
    assert _ask(client).content == b""
    assert _ask(client, "unknown.example.com").content == b""


def test_ask_is_not_throttled(client: Client, tenant_org: Organization) -> None:
    """Every ask comes from the proxy's IP: far more than the anonymous 250/day must still pass."""
    statuses = {_ask(client).status_code for _ in range(300)}

    assert statuses == {200}


def test_ask_is_not_in_the_openapi_schema(client: Client) -> None:
    """Only Caddy calls it, so it stays out of the generated API client."""
    schema = client.get("/api/openapi.json").json()

    assert not [path for path in schema["paths"] if "/tls/" in path]


def test_ask_is_served_from_the_cache_on_repeat(client: Client, tenant_org: Organization) -> None:
    """A repeated ask, positive or negative, costs no query (negative answers are cached too)."""
    assert _ask(client).status_code == 200
    assert _ask(client, "unknown.example.com").status_code == 404

    with CaptureQueriesContext(connection) as queries:
        assert _ask(client).status_code == 200
        assert _ask(client, "unknown.example.com").status_code == 404

    assert _data_queries(queries.captured_queries) == []
