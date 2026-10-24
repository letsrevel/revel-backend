"""Tests for the DNS pre-flight wiring in ``sync_domain_status`` (#1148): PENDING domains only. Cloudflare is mocked."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service, dns_diagnostics

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status
HOST = "events.example.com"
HINT = "No CNAME record was found for events.example.com; add one pointing to customers.letsrevel.io."
TXT_HINT = (
    "Add the TXT record _cf-custom-hostname.events.example.com with value owner-token to prove you own this hostname."
)


def hosted(status: str = "pending", ssl_status: str = "pending_validation", errors: list[str] | None = None) -> t.Any:
    return cloudflare.CustomHostname(
        id="cf-1",
        hostname=HOST,
        status=status,
        ssl=cloudflare.CustomHostnameSsl(status=ssl_status),
        ownership_verification=cloudflare.OwnershipVerification(
            type="txt", name="_cf-custom-hostname.events.example.com", value="owner-token"
        ),
        verification_errors=errors or [],
    )


@pytest.fixture(autouse=True)
def platform_settings(settings: t.Any) -> None:
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    settings.CUSTOM_DOMAIN_CNAME_TARGET = "customers.letsrevel.io"
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone"


@pytest.fixture(autouse=True)
def txt_published() -> t.Iterator[MagicMock]:
    with patch.object(cloudflare, "txt_record_published", return_value=True) as mock:
        yield mock


@pytest.fixture
def diagnose() -> t.Iterator[MagicMock]:
    with patch.object(dns_diagnostics, "diagnose_dns", return_value=[HINT]) as mock:
        yield mock


@pytest.fixture
def domain(organization: Organization) -> OrganizationDomain:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return OrganizationDomain.objects.create(
        organization=organization, hostname=HOST, cloudflare_hostname_id="cf-1", status=Status.PENDING
    )


def sync(domain: OrganizationDomain, result: t.Any) -> None:
    with patch.object(cloudflare, "get_custom_hostname", return_value=result):
        custom_domain_service.sync_domain_status(domain)
    domain.refresh_from_db()


def test_a_pending_domain_gets_the_hints_after_cloudflares_problems(
    domain: OrganizationDomain, diagnose: MagicMock
) -> None:
    sync(domain, hosted(errors=["custom hostname does not CNAME to this zone."]))

    diagnose.assert_called_once_with(HOST, ownership_only=False)
    assert domain.status == Status.PENDING
    assert domain.last_errors == ["custom hostname does not CNAME to this zone.", HINT]


def test_hints_already_in_the_errors_are_not_duplicated(domain: OrganizationDomain, diagnose: MagicMock) -> None:
    sync(domain, hosted(errors=[HINT]))

    assert domain.last_errors == [HINT]


def test_hints_disappear_once_the_dns_is_fixed(domain: OrganizationDomain, diagnose: MagicMock) -> None:
    sync(domain, hosted())
    assert domain.last_errors == [HINT]

    diagnose.return_value = []
    sync(domain, hosted())

    assert domain.last_errors == []


def test_a_ready_hostname_with_a_missing_txt_only_checks_the_ownership_mistake(
    domain: OrganizationDomain, diagnose: MagicMock, txt_published: MagicMock
) -> None:
    txt_published.return_value = False

    sync(domain, hosted("active", "active"))

    diagnose.assert_called_once_with(HOST, ownership_only=True)
    assert domain.status == Status.PENDING
    assert domain.last_errors == [TXT_HINT, HINT]


def test_a_domain_that_activates_is_not_diagnosed(domain: OrganizationDomain, diagnose: MagicMock) -> None:
    sync(domain, hosted("active", "active"))

    diagnose.assert_not_called()
    assert domain.status == Status.ACTIVE
    assert domain.last_errors == []


def test_a_pending_domain_that_fails_keeps_the_hints_that_explain_why(
    domain: OrganizationDomain, diagnose: MagicMock
) -> None:
    sync(domain, hosted("pending", "validation_timed_out"))

    assert domain.status == Status.FAILED
    assert HINT in domain.last_errors


def test_an_active_domain_is_never_diagnosed(domain: OrganizationDomain, diagnose: MagicMock) -> None:
    custom_domain_service.activate_domain(domain)

    sync(domain, hosted("active", "active"))
    sync(domain, hosted("active", "validation_timed_out"))  # a problem on an ACTIVE domain

    diagnose.assert_not_called()
    assert domain.status == Status.ACTIVE
    assert HINT not in domain.last_errors


def test_a_domain_never_provisioned_on_cloudflare_is_not_diagnosed(
    organization: Organization, diagnose: MagicMock
) -> None:
    unprovisioned = OrganizationDomain.objects.create(organization=organization, hostname=HOST, status=Status.PENDING)

    custom_domain_service.sync_domain_status(unprovisioned)

    diagnose.assert_not_called()


def test_an_unreachable_resolver_does_not_break_the_sync(domain: OrganizationDomain) -> None:
    """The real diagnostics run, but every lookup fails: no hints, the sync still completes."""
    with patch.object(cloudflare, "dns_query", return_value=None):
        sync(domain, hosted(errors=["a Cloudflare problem"]))

    assert domain.status == Status.PENDING
    assert domain.last_errors == ["a Cloudflare problem"]
