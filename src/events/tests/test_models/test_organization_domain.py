"""Model-level tests for OrganizationDomain (#1152)."""

import typing as t

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import Organization, OrganizationDomain

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.fixture
def other_organization(organization_staff_user: RevelUser) -> Organization:
    return Organization.objects.create(name="Other Org", slug="other-org", owner=organization_staff_user)


def test_defaults_and_normalization(organization: Organization) -> None:
    """A new domain is PENDING and its hostname is stored normalized."""
    domain = OrganizationDomain.objects.create(organization=organization, hostname="Events.Example.COM.")

    assert domain.status == Status.PENDING
    assert domain.hostname == "events.example.com"
    assert domain.verification_records == []
    assert domain.verified_at is None
    assert domain.cloudflare_hostname_id is None


def test_invalid_hostname_is_rejected_on_save(organization: Organization) -> None:
    """The field validator runs on save (TimeStampedModel.save runs full_clean)."""
    with pytest.raises(ValidationError) as exc:
        OrganizationDomain.objects.create(organization=organization, hostname="app.letsrevel.io")
    assert "hostname" in exc.value.message_dict


def test_hostname_unique_among_non_removed(organization: Organization, other_organization: Organization) -> None:
    """Two live rows cannot share a hostname, whatever their organization."""
    OrganizationDomain.objects.create(organization=organization, hostname="events.example.com")

    with pytest.raises(ValidationError, match="already in use"):
        OrganizationDomain.objects.create(organization=other_organization, hostname="Events.Example.com")


def test_failed_domain_still_reserves_hostname(organization: Organization, other_organization: Organization) -> None:
    """Only REMOVED frees a hostname; FAILED keeps it reserved."""
    OrganizationDomain.objects.create(organization=organization, hostname="events.example.com", status=Status.FAILED)

    with pytest.raises(ValidationError, match="already in use"):
        OrganizationDomain.objects.create(organization=other_organization, hostname="events.example.com")


def test_removed_hostname_can_be_reused(organization: Organization, other_organization: Organization) -> None:
    """A REMOVED row releases its hostname, including to another organization."""
    OrganizationDomain.objects.create(organization=organization, hostname="events.example.com", status=Status.REMOVED)

    domain = OrganizationDomain.objects.create(organization=other_organization, hostname="events.example.com")

    assert domain.status == Status.PENDING


@pytest.mark.parametrize("status", [Status.PENDING, Status.ACTIVE])
def test_one_live_domain_per_organization(organization: Organization, status: OrganizationDomain.Status) -> None:
    """At most one PENDING-or-ACTIVE domain per organization."""
    OrganizationDomain.objects.create(organization=organization, hostname="one.example.com", status=status)

    with pytest.raises(ValidationError, match="pending or active domain"):
        OrganizationDomain.objects.create(organization=organization, hostname="two.example.com")


@pytest.mark.parametrize("status", [Status.FAILED, Status.REMOVED])
def test_dead_domains_do_not_count_towards_the_live_limit(
    organization: Organization, status: OrganizationDomain.Status
) -> None:
    """FAILED and REMOVED rows leave room for a new request."""
    OrganizationDomain.objects.create(organization=organization, hostname="old.example.com", status=status)

    OrganizationDomain.objects.create(organization=organization, hostname="new.example.com")

    assert organization.domains.count() == 2


def test_organization_custom_domain_defaults(organization: Organization) -> None:
    """New organizations are not enabled and have no active hostname."""
    assert organization.custom_domain_enabled is False
    assert organization.custom_domain is None
    assert Organization._meta.get_field("custom_domain").editable is False
