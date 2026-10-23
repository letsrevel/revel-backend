"""Org logo in notification emails resolves to the absolute organization logo endpoint (#1150)."""

import typing as t
import uuid

import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from events.models import Organization
from notifications.utils import get_formatted_context_for_template

pytestmark = pytest.mark.django_db

API_BASE_URL = "https://api.example.test"


@pytest.fixture(autouse=True)
def _api_base_url(settings: t.Any) -> None:
    settings.BASE_URL = API_BASE_URL


def _context(org: Organization, **extra: t.Any) -> dict[str, t.Any]:
    return {"organization_id": str(org.id), "organization_name": org.name, "organization_slug": org.slug, **extra}


def _give_logo(org: Organization, field: str = "logo") -> None:
    Organization.objects.filter(pk=org.pk).update(**{field: "logos/org.png"})


def _expected_url(org: Organization) -> str:
    return f"{API_BASE_URL}{reverse('api:organization_logo', kwargs={'organization_id': org.id})}"


def _org_queries(captured: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in captured.captured_queries if '"events_organization"' in q["sql"]]


@pytest.mark.parametrize("field", ["logo", "logo_thumbnail"])
def test_enrichment_sets_absolute_logo_url_by_id(organization: Organization, field: str) -> None:
    """A logo (thumbnail or original) yields the absolute endpoint URL, looked up by id."""
    _give_logo(organization, field)

    enriched = get_formatted_context_for_template(_context(organization))

    assert enriched["organization_logo_url"] == _expected_url(organization)


def test_enrichment_resolves_logo_by_slug_when_no_id(organization: Organization) -> None:
    """Without an organization_id, the org is identified by slug."""
    _give_logo(organization)

    enriched = get_formatted_context_for_template(
        {"organization_name": organization.name, "organization_slug": organization.slug}
    )

    assert enriched["organization_logo_url"] == _expected_url(organization)


def test_enrichment_omits_logo_key_when_org_has_no_logo(organization: Organization) -> None:
    """No logo means no key, and the signature has no <img> at all."""
    enriched = get_formatted_context_for_template(_context(organization))

    assert "organization_logo_url" not in enriched
    assert "<img" not in enriched["org_signature_html"]


def test_enrichment_omits_logo_key_for_unknown_org() -> None:
    """A context pointing at a missing org resolves no logo and does not raise."""
    enriched = get_formatted_context_for_template(
        {"organization_id": str(uuid.uuid4()), "organization_name": "Gone", "organization_slug": "gone"}
    )

    assert "organization_logo_url" not in enriched


def test_enrichment_keeps_caller_supplied_logo_without_query(organization: Organization) -> None:
    """An explicit organization_logo_url wins and skips the organization lookup."""
    _give_logo(organization)

    with CaptureQueriesContext(connection) as captured:
        enriched = get_formatted_context_for_template(
            _context(organization, organization_logo_url="https://cdn.example.test/logo.png")
        )

    assert enriched["organization_logo_url"] == "https://cdn.example.test/logo.png"
    assert _org_queries(captured) == []


def test_enrichment_looks_up_org_with_exactly_one_query(organization: Organization) -> None:
    """Resolving the logo costs a single organization query."""
    _give_logo(organization)

    with CaptureQueriesContext(connection) as captured:
        get_formatted_context_for_template(_context(organization))

    assert len(_org_queries(captured)) == 1


def test_logo_endpoint_url_is_routable(organization: Organization) -> None:
    """The emitted URL path is a real route that answers without auth."""
    _give_logo(organization)
    enriched = get_formatted_context_for_template(_context(organization))
    path = enriched["organization_logo_url"].removeprefix(API_BASE_URL)

    response = Client().get(path)

    assert response.status_code == 200


def test_footer_renders_logo_with_org_name_as_alt(organization: Organization) -> None:
    """The email footer renders the logo img with alt text set to the org name."""
    _give_logo(organization)
    enriched = get_formatted_context_for_template(_context(organization))

    html = render_to_string("notifications/email/_footer.html", {"context": enriched})

    assert f'<img src="{_expected_url(organization)}" alt="{organization.name}"' in html


def test_footer_renders_no_img_without_logo(organization: Organization) -> None:
    """Without a logo the footer still renders the org link, but no <img>."""
    enriched = get_formatted_context_for_template(_context(organization))

    html = render_to_string("notifications/email/_footer.html", {"context": enriched})

    assert "<img" not in html
    assert organization.name in html
