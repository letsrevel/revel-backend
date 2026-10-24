"""Tests for the custom-domain lifecycle service and tenant resolution (#1152)."""

import typing as t

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError

from events.exceptions import CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.service import custom_domain_service, tenant_service

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.fixture
def enabled_org(organization: Organization) -> Organization:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return organization


@pytest.fixture
def domain(enabled_org: Organization) -> OrganizationDomain:
    return OrganizationDomain.objects.create(organization=enabled_org, hostname="events.example.com")


def test_activate_sets_status_and_syncs_organization(
    domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Activation marks the domain ACTIVE, stamps verified_at and publishes the hostname."""
    with django_capture_on_commit_callbacks(execute=True):
        result = custom_domain_service.activate_domain(domain)

    domain.refresh_from_db()
    assert result.status == Status.ACTIVE
    assert domain.status == Status.ACTIVE
    assert domain.verified_at is not None
    assert Organization.objects.get(pk=domain.organization_id).custom_domain == "events.example.com"
    assert domain.organization.custom_domain == "events.example.com"


def test_activate_refused_when_not_enabled(organization: Organization) -> None:
    """The platform gate is enforced by the service, not just the UI."""
    domain = OrganizationDomain.objects.create(organization=organization, hostname="events.example.com")

    with pytest.raises(CustomDomainNotEnabledError):
        custom_domain_service.activate_domain(domain)

    domain.refresh_from_db()
    organization.refresh_from_db()
    assert domain.status == Status.PENDING
    assert organization.custom_domain is None


def test_activate_refused_for_removed_domain(domain: OrganizationDomain) -> None:
    """A removed domain cannot be brought back."""
    custom_domain_service.remove_domain(domain)

    with pytest.raises(ValidationError, match="removed domain"):
        custom_domain_service.activate_domain(domain)


def test_activate_is_idempotent(domain: OrganizationDomain) -> None:
    """Re-activating an active domain keeps everything consistent."""
    custom_domain_service.activate_domain(domain)
    custom_domain_service.activate_domain(domain)

    assert Organization.objects.get(pk=domain.organization_id).custom_domain == "events.example.com"


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        (custom_domain_service.mark_domain_failed, Status.FAILED),
        (custom_domain_service.remove_domain, Status.REMOVED),
    ],
)
def test_failed_and_removed_clear_the_active_hostname(
    domain: OrganizationDomain,
    transition: t.Callable[[OrganizationDomain], OrganizationDomain],
    expected: OrganizationDomainStatus,
) -> None:
    """Leaving ACTIVE withdraws the hostname from the organization."""
    custom_domain_service.activate_domain(domain)

    transition(domain)

    domain.refresh_from_db()
    assert domain.status == expected
    assert Organization.objects.get(pk=domain.organization_id).custom_domain is None


def test_failing_a_pending_domain_leaves_organization_untouched(domain: OrganizationDomain) -> None:
    """A domain that never went live does not touch Organization.custom_domain."""
    custom_domain_service.mark_domain_failed(domain)

    assert Organization.objects.get(pk=domain.organization_id).custom_domain is None


def test_removing_a_stale_domain_does_not_clear_another_hostname(enabled_org: Organization) -> None:
    """Only a hostname that points at *this* domain is cleared."""
    old = OrganizationDomain.objects.create(organization=enabled_org, hostname="old.example.com", status=Status.FAILED)
    new = OrganizationDomain.objects.create(organization=enabled_org, hostname="new.example.com")
    custom_domain_service.activate_domain(new)

    custom_domain_service.remove_domain(old)

    assert Organization.objects.get(pk=enabled_org.pk).custom_domain == "new.example.com"


def test_tenant_lookup_follows_the_domain_lifecycle(
    domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Pending is invisible, active resolves, and every transition invalidates the cache."""
    assert tenant_service.get_tenant_organization("events.example.com") is None  # caches a negative

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(domain)
    resolved = tenant_service.get_tenant_organization("Events.Example.com.")
    assert resolved is not None
    assert resolved.pk == domain.organization_id
    assert tenant_service.is_tenant_origin("events.example.com") is True

    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.mark_domain_failed(domain)
    assert tenant_service.get_tenant_organization("events.example.com") is None
    assert tenant_service.is_tenant_origin("events.example.com") is False


def test_invalidation_runs_on_commit_not_before(domain: OrganizationDomain) -> None:
    """The stale entry survives until the transaction commits (no re-caching of old state)."""
    assert tenant_service.is_tenant_origin("events.example.com") is False  # caches a negative

    # Without executing on_commit callbacks (pytest-django rolls back), the entry is still there.
    custom_domain_service.activate_domain(domain)

    assert cache.get("tenant:host:v1:events.example.com") == ""


def test_tenant_lookup_caches_negative_answers(enabled_org: Organization) -> None:
    """A miss is cached, so a later change is only seen after invalidation or expiry."""
    assert tenant_service.is_tenant_origin("late.example.com") is False
    assert cache.get("tenant:host:v1:late.example.com") == ""

    Organization.objects.filter(pk=enabled_org.pk).update(custom_domain="late.example.com")

    assert tenant_service.is_tenant_origin("late.example.com") is False
    tenant_service.invalidate_tenant_cache("late.example.com")
    assert tenant_service.is_tenant_origin("late.example.com") is True
    assert cache.get("tenant:host:v1:late.example.com") == str(enabled_org.pk)


def test_tenant_lookup_ignores_junk_without_caching() -> None:
    """Hostnames that cannot be tenants never reach the cache."""
    assert tenant_service.get_tenant_organization("not a host!") is None
    assert tenant_service.get_tenant_organization("a.com:8080") is None
    assert tenant_service.get_tenant_organization("") is None
    assert tenant_service.get_tenant_organization("x" * 300 + ".com") is None


def test_tenant_organization_is_annotated(domain: OrganizationDomain) -> None:
    """The resolved organization carries the EXISTS flags."""
    custom_domain_service.activate_domain(domain)

    org = tenant_service.get_tenant_organization("events.example.com")

    assert org is not None
    assert org.has_membership_plans is False  # type: ignore[attr-defined]
    assert org.has_resources is False  # type: ignore[attr-defined]


def test_removed_is_terminal_for_every_transition(domain: OrganizationDomain) -> None:
    """Once removed, a domain can be neither failed, removed again nor activated."""
    custom_domain_service.remove_domain(domain)

    with pytest.raises(ValidationError, match="removed domain"):
        custom_domain_service.mark_domain_failed(domain)
    with pytest.raises(ValidationError, match="removed domain"):
        custom_domain_service.remove_domain(domain)

    domain.refresh_from_db()
    assert domain.status == Status.REMOVED


def test_removed_domain_cannot_be_revived_through_failed(domain: OrganizationDomain) -> None:
    """remove -> fail is refused, so fail -> activate cannot be used to revive it either."""
    custom_domain_service.remove_domain(domain)

    with pytest.raises(ValidationError):
        custom_domain_service.mark_domain_failed(domain)
    with pytest.raises(ValidationError):
        custom_domain_service.activate_domain(domain)

    domain.refresh_from_db()
    assert domain.status == Status.REMOVED
    assert Organization.objects.get(pk=domain.organization_id).custom_domain is None


@pytest.mark.parametrize("transition", [custom_domain_service.remove_domain, custom_domain_service.mark_domain_failed])
def test_status_change_survives_a_hostname_that_is_no_longer_valid(
    enabled_org: Organization,
    settings: t.Any,
    transition: t.Callable[[OrganizationDomain], OrganizationDomain],
) -> None:
    """``*.localhost`` is valid only under DEBUG; leaving ACTIVE must not re-run the hostname validator."""
    settings.DEBUG = True
    domain = OrganizationDomain.objects.create(organization=enabled_org, hostname="tenant.localhost")
    custom_domain_service.activate_domain(domain)
    settings.DEBUG = False

    transition(domain)

    domain.refresh_from_db()
    assert domain.status != Status.ACTIVE
    assert Organization.objects.get(pk=enabled_org.pk).custom_domain is None


def test_status_change_bumps_updated_at(domain: OrganizationDomain) -> None:
    """The queryset update stamps ``updated_at`` explicitly (auto_now only fires on save)."""
    before = OrganizationDomain.objects.get(pk=domain.pk).updated_at

    custom_domain_service.mark_domain_failed(domain)

    assert OrganizationDomain.objects.get(pk=domain.pk).updated_at > before


def test_activating_a_failed_domain_is_refused_while_the_org_has_another_live_domain(
    enabled_org: Organization,
) -> None:
    """The one-live-domain constraint surfaces as a ValidationError, not an IntegrityError."""
    failed = OrganizationDomain.objects.create(
        organization=enabled_org, hostname="old.example.com", status=Status.FAILED
    )
    OrganizationDomain.objects.create(organization=enabled_org, hostname="new.example.com")

    with pytest.raises(ValidationError, match="already has a pending or active domain"):
        custom_domain_service.activate_domain(failed)

    failed.refresh_from_db()
    assert failed.status == Status.FAILED
    assert Organization.objects.get(pk=enabled_org.pk).custom_domain is None


def test_failed_constraint_check_leaves_the_instance_and_database_unchanged(
    domain: OrganizationDomain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostname conflict (raised by ``validate_constraints``) persists nothing and restores the instance.

    The partial unique indexes make a live hostname clash unreachable through the service (a FAILED row
    still holds its hostname and REMOVED is terminal), so the failure is injected.
    """

    def clash(*args: t.Any, **kwargs: t.Any) -> None:
        raise ValidationError("This hostname is already in use.")

    monkeypatch.setattr(OrganizationDomain, "validate_constraints", clash)

    with pytest.raises(ValidationError, match="already in use"):
        custom_domain_service.activate_domain(domain)

    assert domain.status == Status.PENDING
    assert domain.verified_at is None
    domain.refresh_from_db()
    assert domain.status == Status.PENDING
    assert Organization.objects.get(pk=domain.organization_id).custom_domain is None


def test_deleting_an_organization_invalidates_its_tenant_entry(
    domain: OrganizationDomain, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The cascade bypasses the domain service, so a signal must drop the cached hostname."""
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(domain)
    assert tenant_service.is_tenant_origin("events.example.com") is True  # caches the positive answer
    assert cache.get("tenant:host:v1:events.example.com") is not None

    with django_capture_on_commit_callbacks(execute=True):
        Organization.objects.get(pk=domain.organization_id).delete()

    assert cache.get("tenant:host:v1:events.example.com") is None
    assert tenant_service.is_tenant_origin("events.example.com") is False


def test_deleting_an_organization_without_custom_domain_registers_nothing(
    organization: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """No hostname, nothing to invalidate."""
    with django_capture_on_commit_callbacks() as callbacks:
        organization.delete()

    assert callbacks == []
