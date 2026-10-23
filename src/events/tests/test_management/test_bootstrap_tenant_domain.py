"""The e2e bootstrap seeds one ACTIVE white-label tenant domain (#1152)."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.management.commands.bootstrap_test_events import (
    ORG_ALPHA_SLUG,
    ORG_ALPHA_TENANT_E2E_HOSTNAME,
    TENANT_E2E_HOSTNAME,
    Command,
)
from events.models import Organization, OrganizationDomain
from events.service import tenant_service

pytestmark = pytest.mark.django_db


def test_tenant_domain_is_seeded_and_resolves(settings: t.Any) -> None:
    """``tenant.localhost`` and ``alpha.localhost`` become the ACTIVE domains of their organizations."""
    settings.DEBUG = True  # *.localhost hostnames are only valid under DEBUG
    owner = RevelUser.objects.create_user(username="owner@test.com", email="owner@test.com", password="x")
    command = Command()
    command.org = Organization.objects.create(name="Org", slug="org", owner=owner)
    alpha = Organization.objects.create(name="Alpha", slug=ORG_ALPHA_SLUG, owner=owner)

    command._create_tenant_domain()
    command._create_alpha_tenant_domain()

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
    alpha.refresh_from_db()
    alpha_domain = alpha.domains.get()
    assert ORG_ALPHA_TENANT_E2E_HOSTNAME == "alpha.localhost"
    assert alpha_domain.hostname == ORG_ALPHA_TENANT_E2E_HOSTNAME
    assert alpha_domain.status == OrganizationDomain.Status.ACTIVE
    assert alpha.custom_domain_enabled is True
    assert alpha.custom_domain == ORG_ALPHA_TENANT_E2E_HOSTNAME
    resolved_alpha = tenant_service.get_tenant_organization(ORG_ALPHA_TENANT_E2E_HOSTNAME)
    assert resolved_alpha is not None
    assert resolved_alpha.pk == alpha.pk


def test_alpha_tenant_domain_skipped_without_org_alpha(settings: t.Any) -> None:
    """Without Org Alpha (seeded by bootstrap_events) the helper logs and seeds nothing."""
    settings.DEBUG = True
    command = Command()

    command._create_alpha_tenant_domain()

    assert not OrganizationDomain.objects.filter(hostname=ORG_ALPHA_TENANT_E2E_HOSTNAME).exists()
