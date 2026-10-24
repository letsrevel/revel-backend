"""Notification links follow the organization's custom domain (#1153)."""

import typing as t
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from common.models import SiteSettings
from events.models import Event, Organization
from notifications.service.reminder_service import EventReminderService
from notifications.signals.ticket import _build_base_event_context
from notifications.signals.waitlist import _build_waitlist_spot_context
from notifications.utils import get_formatted_context_for_template

pytestmark = pytest.mark.django_db

HOST = "events.example.com"
TENANT = f"https://{HOST}"


@pytest.fixture(autouse=True)
def platform_url() -> str:
    """Pin the platform origin so assertions do not depend on the local .env."""
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.letsrevel.io"
    site.save()
    return "https://app.letsrevel.io"


def _set_domain(org: Organization, *, domain: str | None = HOST, enabled: bool = True) -> None:
    Organization.objects.filter(pk=org.pk).update(custom_domain=domain, custom_domain_enabled=enabled)
    org.refresh_from_db()


def _context(org: Organization, **extra: t.Any) -> dict[str, t.Any]:
    return {
        "organization_id": str(org.id),
        "organization_name": org.name,
        "organization_slug": org.slug,
        "event_id": "11111111-1111-1111-1111-111111111111",
        "event_name": "Party",
        **extra,
    }


def _org_queries(captured: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in captured.captured_queries if '"events_organization"' in q["sql"]]


def test_enrichment_uses_tenant_origin_for_active_domain(organization: Organization) -> None:
    _set_domain(organization)
    ctx = _context(organization)

    enriched = get_formatted_context_for_template(ctx)

    assert enriched["organization_url"] == f"{TENANT}/org/{organization.slug}"
    assert enriched["event_url"] == f"{TENANT}/events/{ctx['event_id']}"
    assert f'href="{TENANT}/events/{ctx["event_id"]}"' in enriched["event_button_html"]
    assert f'href="{TENANT}/events/{ctx["event_id"]}"' in enriched["event_link_html"]
    assert f"({TENANT}/events/{ctx['event_id']})" in enriched["event_link_md"]
    assert f'href="{TENANT}/org/{organization.slug}"' in enriched["org_signature_html"]
    assert f"({TENANT}/org/{organization.slug})" in enriched["org_signature_md"]


def test_enrichment_identifies_org_by_slug_alone(organization: Organization) -> None:
    _set_domain(organization)
    ctx = _context(organization)
    del ctx["organization_id"]

    enriched = get_formatted_context_for_template(ctx)

    assert enriched["organization_url"] == f"{TENANT}/org/{organization.slug}"
    assert enriched["event_url"].startswith(f"{TENANT}/events/")


@pytest.mark.parametrize(
    ("domain", "enabled"),
    [(None, True), (HOST, False)],
    ids=["no-domain", "kill-switch-off"],
)
def test_enrichment_stays_on_platform_without_an_enabled_domain(
    organization: Organization, platform_url: str, domain: str | None, enabled: bool
) -> None:
    _set_domain(organization, domain=domain, enabled=enabled)
    ctx = _context(organization)

    enriched = get_formatted_context_for_template(ctx)

    assert enriched["organization_url"] == f"{platform_url}/org/{organization.slug}"
    assert enriched["event_url"] == f"{platform_url}/events/{ctx['event_id']}"
    assert HOST not in enriched["event_button_html"] + enriched["org_signature_html"] + enriched["org_signature_md"]


def test_event_only_context_with_org_id_uses_tenant_origin(organization: Organization) -> None:
    """Contexts without an org signature still resolve the origin through organization_id."""
    _set_domain(organization)
    ctx = {"organization_id": str(organization.id), "event_id": "e1", "event_name": "Party"}

    enriched = get_formatted_context_for_template(ctx)

    assert enriched["event_url"] == f"{TENANT}/events/e1"
    assert "org_signature_html" not in enriched


def test_context_without_org_identifier_stays_on_platform(organization: Organization, platform_url: str) -> None:
    _set_domain(organization)

    enriched = get_formatted_context_for_template({"event_id": "e1", "event_name": "Party"})

    assert enriched["event_url"] == f"{platform_url}/events/e1"


def test_unknown_org_falls_back_to_platform(platform_url: str) -> None:
    enriched = get_formatted_context_for_template(
        {
            "organization_id": "22222222-2222-2222-2222-222222222222",
            "organization_name": "Gone",
            "organization_slug": "gone",
        }
    )

    assert enriched["organization_url"] == f"{platform_url}/org/gone"


@pytest.mark.parametrize("with_domain", [False, True], ids=["platform", "tenant"])
def test_enrichment_costs_one_org_query_regardless_of_domain(organization: Organization, with_domain: bool) -> None:
    """The tenant origin rides the logo lookup: still exactly one organization query per render."""
    if with_domain:
        _set_domain(organization)
    ctx = _context(organization)
    SiteSettings.get_solo()  # warm the SiteSettings cache

    with CaptureQueriesContext(connection) as captured:
        get_formatted_context_for_template(ctx)

    assert len(_org_queries(captured)) == 1
    assert len(captured) == 1


def test_repeated_renders_of_the_same_org_issue_at_most_one_org_query(organization: Organization) -> None:
    """Bulk sends render several bodies per recipient: the org lookup is cached by id (#1153)."""
    _set_domain(organization)
    ctx = _context(organization)
    SiteSettings.get_solo()

    with CaptureQueriesContext(connection) as captured:
        first = get_formatted_context_for_template(ctx)
        second = get_formatted_context_for_template(ctx)

    assert len(_org_queries(captured)) == 1
    assert first["event_url"] == second["event_url"] == f"{TENANT}/events/{ctx['event_id']}"


def test_cache_failure_falls_back_to_the_db_and_still_renders(organization: Organization) -> None:
    _set_domain(organization)
    ctx = _context(organization)

    with (
        patch("django.core.cache.cache.get", side_effect=RuntimeError("redis down")),
        patch("django.core.cache.cache.set", side_effect=RuntimeError("redis down")),
        CaptureQueriesContext(connection) as captured,
    ):
        first = get_formatted_context_for_template(ctx)
        second = get_formatted_context_for_template(ctx)

    assert first["event_url"] == second["event_url"] == f"{TENANT}/events/{ctx['event_id']}"
    assert len(_org_queries(captured)) == 2


def test_cached_entry_is_keyed_by_org_id_with_the_ttl(organization: Organization) -> None:
    _set_domain(organization)

    get_formatted_context_for_template(_context(organization))

    entry = cache.get(f"org_link:v1:{organization.id}")
    assert entry == {
        "id": str(organization.id),
        "has_logo": False,
        "custom_domain": HOST,
        "custom_domain_enabled": True,
    }


def test_lookup_only_selects_the_columns_it_needs(organization: Organization) -> None:
    ctx = _context(organization)

    with CaptureQueriesContext(connection) as captured:
        get_formatted_context_for_template(ctx)

    sql = _org_queries(captured)[0]
    assert '"custom_domain"' in sql
    assert '"custom_domain_enabled"' in sql
    assert '"description"' not in sql


# ---- builders that already hold the org ----


@pytest.fixture
def tenant_event(public_event: Event) -> Event:
    _set_domain(public_event.organization)
    return Event.objects.select_related("organization").get(pk=public_event.pk)


def test_ticket_base_context_uses_tenant_origin(tenant_event: Event) -> None:
    ctx = _build_base_event_context(tenant_event)

    assert ctx["event_url"] == f"{TENANT}/events/{tenant_event.id}"
    assert ctx["organization_id"] == str(tenant_event.organization_id)


def test_waitlist_context_uses_tenant_origin(tenant_event: Event) -> None:
    ctx = _build_waitlist_spot_context(tenant_event, 1)

    assert ctx["event_url"] == f"{TENANT}/events/{tenant_event.id}"


def test_ticket_base_context_stays_on_platform_when_kill_switch_off(public_event: Event, platform_url: str) -> None:
    _set_domain(public_event.organization, enabled=False)

    ctx = _build_base_event_context(Event.objects.select_related("organization").get(pk=public_event.pk))

    assert ctx["event_url"] == f"{platform_url}/events/{public_event.id}"


def test_reminder_context_is_org_aware_and_carries_the_org_id(tenant_event: Event) -> None:
    ctx = EventReminderService().build_event_context(tenant_event, 1)

    assert ctx["event_url"] == f"{TENANT}/events/{tenant_event.id}"
    assert ctx["organization_id"] == str(tenant_event.organization_id)


def test_reminder_explicit_base_url_wins(tenant_event: Event) -> None:
    ctx = EventReminderService(frontend_base_url="https://override.test").build_event_context(tenant_event, 1)

    assert ctx["event_url"] == f"https://override.test/events/{tenant_event.id}"
