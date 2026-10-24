"""Unit tests for the org-aware frontend base URL helper (#1153)."""

import typing as t

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from common.models import SiteSettings
from events.models import Organization
from events.utils.frontend_urls import frontend_base_url_for

pytestmark = pytest.mark.django_db

HOST = "events.example.com"


@pytest.fixture(autouse=True)
def platform_url() -> None:
    """Pin the platform URL (with a trailing slash, to prove it is stripped)."""
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.letsrevel.io/"
    site.save()


def _org(organization: Organization, *, domain: str | None, enabled: bool) -> Organization:
    organization.custom_domain = domain
    organization.custom_domain_enabled = enabled
    return organization


def test_none_org_uses_platform_url() -> None:
    assert frontend_base_url_for(None) == "https://app.letsrevel.io"


def test_org_without_domain_uses_platform_url(organization: Organization) -> None:
    assert frontend_base_url_for(_org(organization, domain=None, enabled=True)) == "https://app.letsrevel.io"


def test_active_domain_with_kill_switch_off_uses_platform_url(organization: Organization) -> None:
    assert frontend_base_url_for(_org(organization, domain=HOST, enabled=False)) == "https://app.letsrevel.io"


def test_active_enabled_domain_uses_tenant_origin(organization: Organization) -> None:
    assert frontend_base_url_for(_org(organization, domain=HOST, enabled=True)) == f"https://{HOST}"


def test_template_setting_is_honoured_and_trailing_slash_stripped(organization: Organization, settings: t.Any) -> None:
    settings.CUSTOM_DOMAIN_URL_TEMPLATE = "http://{hostname}:5173/"

    assert frontend_base_url_for(_org(organization, domain=HOST, enabled=True)) == f"http://{HOST}:5173"


def test_resolves_without_queries_for_a_loaded_org(organization: Organization) -> None:
    """Only the solo-cached SiteSettings is touched; the org's own columns cost nothing."""
    org = _org(organization, domain=HOST, enabled=True)
    frontend_base_url_for(None)  # warm the SiteSettings cache

    with CaptureQueriesContext(connection) as captured:
        frontend_base_url_for(org)
        frontend_base_url_for(None)

    assert len(captured) == 0
