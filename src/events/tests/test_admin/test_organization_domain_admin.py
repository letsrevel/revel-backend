"""Tests for the OrganizationDomain admin and the Organization admin's custom-domain fields (#1152)."""

import typing as t
from types import SimpleNamespace

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage

from accounts.models import RevelUser
from events.admin.organization import OrganizationAdmin
from events.admin.organization_domain import OrganizationDomainAdmin
from events.models import Organization, OrganizationDomain
from events.service import custom_domain_service, tenant_service

pytestmark = pytest.mark.django_db

Status = OrganizationDomain.Status


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


def _admin() -> OrganizationDomainAdmin:
    return t.cast(OrganizationDomainAdmin, site._registry[OrganizationDomain])


def _request(rf: t.Any, user: RevelUser) -> t.Any:
    request = rf.post("/")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _messages(request: t.Any) -> list[str]:
    return [str(m) for m in get_messages(request)]


@pytest.fixture
def pending(organization: Organization) -> OrganizationDomain:
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    return OrganizationDomain.objects.create(organization=organization, hostname="events.example.com")


def _qs(domain: OrganizationDomain) -> t.Any:
    return OrganizationDomain.objects.filter(pk=domain.pk)


def test_registered_with_expected_list_configuration() -> None:
    """The changelist shows hostname/org/status/verified_at and can be filtered and searched."""
    admin_instance = _admin()

    assert {"hostname", "organization_link", "status", "verified_at"} <= set(admin_instance.list_display)
    assert "status" in admin_instance.list_filter
    assert "hostname" in admin_instance.search_fields


def test_activate_action_publishes_hostname(rf: t.Any, superuser: RevelUser, pending: OrganizationDomain) -> None:
    """The Activate action goes through the service."""
    request = _request(rf, superuser)

    _admin().activate_domains(request, _qs(pending))

    pending.refresh_from_db()
    assert pending.status == Status.ACTIVE
    assert Organization.objects.get(pk=pending.organization_id).custom_domain == "events.example.com"
    assert _messages(request) == ["1 domain(s) activated."]


def test_activate_action_reports_gate_refusal(
    rf: t.Any, superuser: RevelUser, pending: OrganizationDomain, organization: Organization
) -> None:
    """A disabled organization is refused with a readable message and nothing changes."""
    organization.custom_domain_enabled = False
    organization.save(update_fields=["custom_domain_enabled"])
    request = _request(rf, superuser)

    _admin().activate_domains(request, _qs(pending))

    pending.refresh_from_db()
    assert pending.status == Status.PENDING
    assert _messages(request) == ["events.example.com: Custom domains are not enabled for this organization."]


def test_activate_action_reports_removed_domain(rf: t.Any, superuser: RevelUser, pending: OrganizationDomain) -> None:
    """A removed domain is refused with the validation message."""
    pending.status = Status.REMOVED
    pending.save(update_fields=["status"])
    request = _request(rf, superuser)

    _admin().activate_domains(request, _qs(pending))

    assert _messages(request) == ["events.example.com: A removed domain cannot be activated."]


def test_mark_failed_and_remove_actions(rf: t.Any, superuser: RevelUser, pending: OrganizationDomain) -> None:
    """Fail and Remove withdraw an active hostname."""
    request = _request(rf, superuser)
    _admin().activate_domains(request, _qs(pending))

    _admin().mark_domains_failed(request, _qs(pending))
    pending.refresh_from_db()
    assert pending.status == Status.FAILED
    assert Organization.objects.get(pk=pending.organization_id).custom_domain is None

    _admin().activate_domains(request, _qs(pending))
    _admin().remove_domains(request, _qs(pending))
    pending.refresh_from_db()
    assert pending.status == Status.REMOVED
    assert Organization.objects.get(pk=pending.organization_id).custom_domain is None


def test_status_and_identity_are_read_only(rf: t.Any, superuser: RevelUser, pending: OrganizationDomain) -> None:
    """Status only moves through the service; hostname/organization lock once created."""
    request = _request(rf, superuser)

    on_existing = _admin().get_readonly_fields(request, pending)
    on_add = _admin().get_readonly_fields(request, None)

    assert {"status", "verified_at", "hostname", "organization"} <= set(on_existing)
    assert "status" in on_add
    assert "hostname" not in on_add
    assert _admin().has_delete_permission(request, pending) is False


def test_organization_admin_exposes_gate_but_not_the_synced_hostname(rf: t.Any, superuser: RevelUser) -> None:
    """custom_domain_enabled is editable in the admin; custom_domain is read-only."""
    org_admin = t.cast(OrganizationAdmin, site._registry[Organization])
    fieldset_fields = [f for _, opts in org_admin.fieldsets for f in opts["fields"]]
    request = _request(rf, superuser)

    assert "custom_domain_enabled" in fieldset_fields
    assert "custom_domain" in org_admin.get_readonly_fields(request, None)
    assert "custom_domain_enabled" not in org_admin.get_readonly_fields(request, None)


@pytest.fixture
def live_org(organization: Organization, django_capture_on_commit_callbacks: t.Any) -> Organization:
    """An enabled organization whose domain is ACTIVE and already resolving (cache warmed)."""
    organization.custom_domain_enabled = True
    organization.save(update_fields=["custom_domain_enabled"])
    domain = OrganizationDomain.objects.create(organization=organization, hostname="events.example.com")
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(domain)
    organization.refresh_from_db()
    assert tenant_service.get_tenant_organization("events.example.com") is not None
    return organization


def _org_save(rf: t.Any, superuser: RevelUser, organization: Organization, changed: list[str]) -> None:
    """Run the Organization admin's save_model the way the change form would, with the given changed fields."""
    org_admin = t.cast(OrganizationAdmin, site._registry[Organization])
    org_admin.save_model(_request(rf, superuser), organization, SimpleNamespace(changed_data=changed), change=True)


def test_admin_flipping_kill_switch_invalidates_cached_hostname(
    rf: t.Any, superuser: RevelUser, live_org: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Switching custom_domain_enabled off drops the cache on commit, so the hostname stops resolving."""
    live_org.custom_domain_enabled = False

    with django_capture_on_commit_callbacks(execute=True):
        _org_save(rf, superuser, live_org, ["custom_domain_enabled"])

    assert tenant_service.get_tenant_organization("events.example.com") is None


def test_admin_flipping_kill_switch_back_on_restores_hostname(
    rf: t.Any, superuser: RevelUser, live_org: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Switching it back on invalidates again, so the active domain resolves without redoing anything."""
    live_org.custom_domain_enabled = False
    with django_capture_on_commit_callbacks(execute=True):
        _org_save(rf, superuser, live_org, ["custom_domain_enabled"])
    live_org.custom_domain_enabled = True

    with django_capture_on_commit_callbacks(execute=True):
        _org_save(rf, superuser, live_org, ["custom_domain_enabled"])

    resolved = tenant_service.get_tenant_organization("events.example.com")
    assert resolved is not None
    assert resolved.pk == live_org.pk


def test_admin_save_without_flag_change_keeps_cache(
    rf: t.Any, superuser: RevelUser, live_org: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Saving other fields does not touch the tenant cache."""
    with django_capture_on_commit_callbacks(execute=True):
        _org_save(rf, superuser, live_org, ["name"])

    assert tenant_service.get_tenant_organization("events.example.com") is not None


def test_admin_save_without_active_domain_registers_no_invalidation(
    rf: t.Any, superuser: RevelUser, organization: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """With no active hostname there is nothing to invalidate, even when the flag changed."""
    organization.custom_domain_enabled = True

    with django_capture_on_commit_callbacks() as callbacks:
        _org_save(rf, superuser, organization, ["custom_domain_enabled"])

    assert callbacks == []
