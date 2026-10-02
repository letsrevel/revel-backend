"""Capabilities exposed to the frontend, and the invoicing-mode data migration (EU layer 1)."""

import importlib
import typing as t

import pytest
from django.apps import apps as django_apps
from django.test.client import Client
from django.urls import reverse

from events.compliance.policies.hr import FISCALIZATION_NOTICE
from events.models import Organization, TicketTier
from events.schema import TicketTierSchema

pytestmark = pytest.mark.django_db

_disable_blocked = importlib.import_module("events.migrations.0128_disable_blocked_attendee_invoicing").disable


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        (
            "IT",
            {
                "country": "IT",
                "attendee_invoicing": "allowed",
                "online_payment": "blocked",
                "offline_payment": "allowed",
            },
        ),
        (
            "HR",
            {
                "country": "HR",
                "attendee_invoicing": "blocked",
                "online_payment": "allowed",
                "offline_payment": "allowed",
                "notices": [
                    {
                        "key": "hr_fiscalization",
                        "applies_to": "attendee_invoicing",
                        "message": str(FISCALIZATION_NOTICE),
                    }
                ],
            },
        ),
        (
            "BE",
            {
                "country": "BE",
                "attendee_invoicing": "blocked_for_business_buyers",
                "online_payment": "allowed",
                "offline_payment": "allowed",
            },
        ),
        (
            "",
            {
                "country": "",
                "attendee_invoicing": "allowed",
                "online_payment": "allowed",
                "offline_payment": "allowed",
                "notices": [],
            },
        ),
    ],
)
def test_org_admin_detail_exposes_compliance(
    owner_client: Client, organization: Organization, country: str, expected: dict[str, t.Any]
) -> None:
    organization.vat_country_code = country
    organization.save(update_fields=["vat_country_code"])

    response = owner_client.get(reverse("api:get_organization_admin", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    assert response.json()["compliance"] == {"notices": [], "region": "", **expected}


def test_billing_info_exposes_compliance(owner_client: Client, organization: Organization) -> None:
    organization.vat_id = "EL123456789"
    organization.save(update_fields=["vat_id"])

    response = owner_client.get(reverse("api:get_billing_info", kwargs={"slug": organization.slug}))

    assert response.status_code == 200
    assert response.json()["compliance"]["country"] == "GR"
    assert response.json()["compliance"]["attendee_invoicing"] == "blocked"


@pytest.mark.parametrize(("country", "available"), [("PT", False), ("PL", True), ("AT", True)])
def test_tier_invoicing_flag_follows_the_policy(
    organization: Organization, event_ticket_tier: TicketTier, country: str, available: bool
) -> None:
    """A mode stored before the gate does not advertise invoices the gate will refuse."""
    organization.vat_country_code = country
    organization.invoicing_mode = Organization.InvoicingMode.AUTO
    organization.save(update_fields=["vat_country_code", "invoicing_mode"])

    assert TicketTierSchema.resolve_invoicing_available(event_ticket_tier) is available


def test_migration_switches_blocked_countries_to_none(organization: Organization) -> None:
    orgs = {"HR": organization}
    for country in ("EL", "BE", "AT", "ES"):
        orgs[country] = Organization.objects.create(
            name=f"Org {country}", slug=f"org-{country.lower()}", owner=organization.owner
        )
    orgs["HR"].vat_country_code = "HR"
    orgs["EL"].vat_id = "EL123456789"  # Greece via the VAT prefix
    orgs["BE"].vat_country_code = "BE"
    orgs["AT"].vat_country_code = "AT"
    orgs["ES"].vat_country_code = "ES"  # VERI*FACTU only from 2027: left alone
    for org in orgs.values():
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()

    _disable_blocked(django_apps, None)

    modes = {code: Organization.objects.get(pk=org.pk).invoicing_mode for code, org in orgs.items()}
    assert modes == {
        "HR": Organization.InvoicingMode.NONE,
        "EL": Organization.InvoicingMode.NONE,
        "BE": Organization.InvoicingMode.HYBRID,
        "AT": Organization.InvoicingMode.HYBRID,
        "ES": Organization.InvoicingMode.HYBRID,
    }
