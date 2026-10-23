"""The e2e bootstrap seeds one ACTIVE white-label tenant domain (#1152)."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.management.commands.bootstrap_test_events import TENANT_E2E_HOSTNAME, Command
from events.models import Organization, OrganizationDomain
from events.service import tenant_service

pytestmark = pytest.mark.django_db


def test_tenant_domain_is_seeded_and_resolves(settings: t.Any) -> None:
    """``tenant.localhost`` becomes the ACTIVE domain of the bootstrapped organization."""
    settings.DEBUG = True  # *.localhost hostnames are only valid under DEBUG
    owner = RevelUser.objects.create_user(username="owner@test.com", email="owner@test.com", password="x")
    command = Command()
    command.org = Organization.objects.create(name="Org", slug="org", owner=owner)

    command._create_tenant_domain()

    command.org.refresh_from_db()
    domain = command.org.domains.get()
    assert TENANT_E2E_HOSTNAME == "tenant.localhost"
    assert domain.hostname == TENANT_E2E_HOSTNAME
    assert domain.status == OrganizationDomain.Status.ACTIVE
    assert command.org.custom_domain_enabled is True
    assert command.org.custom_domain == TENANT_E2E_HOSTNAME
    resolved = tenant_service.get_tenant_organization(TENANT_E2E_HOSTNAME)
    assert resolved is not None
    assert resolved.pk == command.org.pk
