"""Tests for the tenant ``return_host`` hint helper (white-label, #1156)."""

import typing as t
from unittest.mock import patch

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from accounts.models import RevelUser
from events.models import Organization, OrganizationDomain
from events.service import custom_domain_service, tenant_service
from events.signals import allow_cors_for_tenant_origin

pytestmark = pytest.mark.django_db

HOST = "events.example.com"


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts and DEBUG so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.fixture
def enabled_org(django_user_model: type[RevelUser]) -> Organization:
    owner = django_user_model.objects.create_user(username="tenant_owner", email="tenant_owner@example.com")
    return Organization.objects.create(name="Tenant Org", slug="tenant-org", owner=owner, custom_domain_enabled=True)


@pytest.fixture
def active_domain(enabled_org: Organization, django_capture_on_commit_callbacks: t.Any) -> OrganizationDomain:
    domain = OrganizationDomain.objects.create(organization=enabled_org, hostname=HOST)
    with django_capture_on_commit_callbacks(execute=True):
        custom_domain_service.activate_domain(domain)
    return domain


def _request(origin: str | None) -> HttpRequest:
    headers = {"Origin": origin} if origin is not None else {}
    return RequestFactory().post("/api/account/register", headers=headers)


def test_no_origin_header_gives_no_hint(active_domain: OrganizationDomain) -> None:
    assert tenant_service.tenant_return_host(_request(None)) is None


@pytest.mark.parametrize(
    "origin, expected",
    [
        (f"https://{HOST}", HOST),
        (f"https://{HOST}:8443", HOST),  # port is ignored, like the CORS receiver
        (f"https://{HOST.upper()}", HOST),  # normalized to lowercase
    ],
)
def test_active_tenant_origin_gives_hint(active_domain: OrganizationDomain, origin: str, expected: str) -> None:
    assert tenant_service.tenant_return_host(_request(origin)) == expected


def test_pending_domain_gives_no_hint(enabled_org: Organization) -> None:
    OrganizationDomain.objects.create(organization=enabled_org, hostname=HOST)  # PENDING by default

    assert tenant_service.tenant_return_host(_request(f"https://{HOST}")) is None


def test_unknown_host_gives_no_hint(active_domain: OrganizationDomain) -> None:
    assert tenant_service.tenant_return_host(_request("https://other.example.com")) is None


def test_kill_switch_off_gives_no_hint(active_domain: OrganizationDomain, enabled_org: Organization) -> None:
    Organization.objects.filter(pk=enabled_org.pk).update(custom_domain_enabled=False)
    tenant_service.invalidate_tenant_cache(HOST)

    assert tenant_service.tenant_return_host(_request(f"https://{HOST}")) is None


def test_http_scheme_gives_no_hint_without_debug(active_domain: OrganizationDomain) -> None:
    assert tenant_service.tenant_return_host(_request(f"http://{HOST}")) is None


def test_http_scheme_allowed_under_debug(active_domain: OrganizationDomain, settings: t.Any) -> None:
    settings.DEBUG = True

    assert tenant_service.tenant_return_host(_request(f"http://{HOST}")) == HOST


@pytest.mark.parametrize("origin", ["https://app.letsrevel.io", "https://api.letsrevel.io", "null", "not a url", ""])
def test_platform_or_garbage_origin_gives_no_hint(active_domain: OrganizationDomain, origin: str) -> None:
    """A spoofed Origin naming a platform host (or garbage) is not a tenant origin."""
    assert tenant_service.tenant_return_host(_request(origin)) is None


def test_cache_failure_fails_open_to_db_lookup(active_domain: OrganizationDomain) -> None:
    """A broken cache degrades to the DB lookup instead of failing the request."""
    with (
        patch("events.service.tenant_service.cache.get", side_effect=RuntimeError("cache down")),
        patch("events.service.tenant_service.cache.set", side_effect=RuntimeError("cache down")),
    ):
        assert tenant_service.tenant_return_host(_request(f"https://{HOST}")) == HOST
        assert tenant_service.tenant_return_host(_request("https://other.example.com")) is None


def test_cors_receiver_shares_the_origin_rule(active_domain: OrganizationDomain, settings: t.Any) -> None:
    """The CORS receiver and the return hint can never disagree about what a tenant origin is."""
    assert allow_cors_for_tenant_origin(None, _request(f"https://{HOST}")) is True
    assert allow_cors_for_tenant_origin(None, _request(f"http://{HOST}")) is False
    assert allow_cors_for_tenant_origin(None, _request("https://other.example.com")) is False
    assert allow_cors_for_tenant_origin(None, _request(None)) is False
