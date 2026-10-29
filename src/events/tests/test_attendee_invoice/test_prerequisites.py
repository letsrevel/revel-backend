"""Tests for the structured seller-address requirement of attendee invoicing."""

import pytest
from django.test import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.service.attendee_invoice_service import einvoice_missing_fields, set_invoicing_mode
from events.tests.test_attendee_invoice._helpers import _make_org_invoicing_ready

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for the organization owner."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


ALL_MISSING = ["billing_address_line1", "billing_postal_code", "billing_city", "billing_country_code"]


def _clear_structured_address(org: Organization) -> None:
    org.billing_address_line1 = ""
    org.billing_postal_code = ""
    org.billing_city = ""
    org.billing_country_code = ""
    org.save()


def test_enabling_invoicing_requires_structured_address(organization: Organization) -> None:
    """A legacy free-text address alone no longer satisfies the prerequisites."""
    org = _make_org_invoicing_ready(organization)
    _clear_structured_address(org)
    with pytest.raises(HttpError) as exc_info:
        set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
    assert exc_info.value.status_code == 422
    assert "Billing address (street, postal code, city and country) is required" in str(exc_info.value)


def test_enabling_succeeds_with_structured_address(organization: Organization) -> None:
    org = _make_org_invoicing_ready(organization)
    set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
    org.refresh_from_db()
    assert org.invoicing_mode == Organization.InvoicingMode.AUTO


def test_existing_invoicing_org_is_not_switched_off(
    organization: Organization, organization_owner_client: Client
) -> None:
    org = _make_org_invoicing_ready(organization)
    _clear_structured_address(org)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()

    response = organization_owner_client.get(reverse("api:get_billing_info", kwargs={"slug": org.slug}))

    assert response.status_code == 200
    data = response.json()
    assert data["invoicing_mode"] == Organization.InvoicingMode.AUTO
    assert data["einvoice_ready"] is False
    assert data["einvoice_missing_fields"] == ALL_MISSING


def test_einvoice_ready_true_when_complete(organization: Organization, organization_owner_client: Client) -> None:
    org = _make_org_invoicing_ready(organization)
    assert einvoice_missing_fields(org) == []

    response = organization_owner_client.get(reverse("api:get_billing_info", kwargs={"slug": org.slug}))

    data = response.json()
    assert data["einvoice_ready"] is True
    assert data["einvoice_missing_fields"] == []


@pytest.mark.parametrize(
    "current,target",
    [
        (Organization.InvoicingMode.AUTO, Organization.InvoicingMode.HYBRID),
        (Organization.InvoicingMode.HYBRID, Organization.InvoicingMode.AUTO),
    ],
)
def test_switching_between_active_modes_does_not_need_structured_address(
    organization: Organization, current: Organization.InvoicingMode, target: Organization.InvoicingMode
) -> None:
    """An org already invoicing with a legacy-only address can switch AUTO <-> HYBRID."""
    org = _make_org_invoicing_ready(organization)
    _clear_structured_address(org)
    org.invoicing_mode = current
    org.save()

    set_invoicing_mode(org, target)

    org.refresh_from_db()
    assert org.invoicing_mode == target


def test_switching_between_active_modes_still_checks_other_prerequisites(organization: Organization) -> None:
    org = _make_org_invoicing_ready(organization)
    _clear_structured_address(org)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.billing_name = ""
    org.save()
    with pytest.raises(HttpError) as exc_info:
        set_invoicing_mode(org, Organization.InvoicingMode.HYBRID)
    assert "Billing name" in str(exc_info.value)
    assert "Billing address" not in str(exc_info.value)


def test_einvoice_ready_false_without_billing_name_and_validated_vat(
    organization: Organization, organization_owner_client: Client
) -> None:
    """A complete address is not enough: billing name and a VIES-validated VAT ID are also required."""
    org = _make_org_invoicing_ready(organization)
    org.billing_name = ""
    org.vat_id_validated = False
    org.save()

    expected = ["billing_name", "vat_id"]
    assert einvoice_missing_fields(org) == expected
    data = organization_owner_client.get(reverse("api:get_billing_info", kwargs={"slug": org.slug})).json()
    assert data["einvoice_ready"] is False
    assert data["einvoice_missing_fields"] == expected


def test_einvoice_missing_fields_lists_address_before_other_prerequisites(organization: Organization) -> None:
    org = _make_org_invoicing_ready(organization)
    _clear_structured_address(org)
    org.billing_name = ""
    org.vat_id_validated = False
    org.save()
    assert einvoice_missing_fields(org) == [*ALL_MISSING, "billing_name", "vat_id"]


def _online_tier(event: Event, **kwargs: object) -> TicketTier:
    defaults: dict[str, object] = {"payment_method": TicketTier.PaymentMethod.ONLINE, "price": 10, "name": "T"}
    return TicketTier.objects.create(event=event, **{**defaults, **kwargs})


def test_einvoice_missing_lists_exemption_reason_for_zero_rate_online_tier(
    organization: Organization, event: Event
) -> None:
    org = _make_org_invoicing_ready(organization)
    org.vat_rate = 0
    org.save()
    assert einvoice_missing_fields(org) == []  # no paid online tier: nothing is invoiced

    _online_tier(event)
    assert einvoice_missing_fields(org) == ["vat_exemption_reason"]

    org.vat_exemption_reason = "Art. 132 VAT Directive"
    org.save()
    assert einvoice_missing_fields(org) == []


def test_einvoice_missing_ignores_free_and_offline_zero_rate_tiers(organization: Organization, event: Event) -> None:
    org = _make_org_invoicing_ready(organization)
    org.vat_rate = 0
    org.save()
    _online_tier(event, price=0, name="Free")
    _online_tier(event, payment_method=TicketTier.PaymentMethod.OFFLINE, name="Offline")
    assert einvoice_missing_fields(org) == []


def test_einvoice_missing_not_flagged_when_every_tier_overrides_rate(organization: Organization, event: Event) -> None:
    org = _make_org_invoicing_ready(organization)
    org.vat_rate = 0
    org.save()
    _online_tier(event, vat_rate=20)
    assert einvoice_missing_fields(org) == []
