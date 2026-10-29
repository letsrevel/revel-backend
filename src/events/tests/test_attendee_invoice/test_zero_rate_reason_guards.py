"""Org-level guards for the "paid online 0% tier needs an exemption reason" rule (#1234)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSector
from events.service.attendee_invoice_service import set_invoicing_mode
from events.service.zero_rate_guard_service import zero_rate_tiers_missing_reason
from events.tests.test_attendee_invoice._helpers import _make_org_invoicing_ready

pytestmark = pytest.mark.django_db

MESSAGE = "Some paid ticket tiers have a 0% VAT rate and no exemption reason"


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _tier(event: Event, **kwargs: t.Any) -> TicketTier:
    defaults: dict[str, t.Any] = {"payment_method": TicketTier.PaymentMethod.ONLINE, "price": 10, "name": "T"}
    return TicketTier.objects.create(event=event, **{**defaults, **kwargs})


def _org(organization: Organization, *, mode: Organization.InvoicingMode, vat_rate: str = "22.00") -> Organization:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = mode
    org.vat_rate = Decimal(vat_rate)
    org.save()
    return org


def _patch(client: Client, org: Organization, data: dict[str, t.Any]) -> t.Any:
    return client.patch(
        reverse("api:update_billing_info", kwargs={"slug": org.slug}),
        data=orjson.dumps(data),
        content_type="application/json",
    )


class TestZeroRateTiersMissingReason:
    def test_upcoming_flags_and_past_does_not(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event)
        assert zero_rate_tiers_missing_reason(org) is True

        Event.objects.filter(pk=event.pk).update(
            start=timezone.now() - timedelta(days=3), end=timezone.now() - timedelta(days=2)
        )
        assert zero_rate_tiers_missing_reason(org) is False

    def test_org_reason_covers_every_tier(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event)
        org.vat_exemption_reason = "Art. 132"
        assert zero_rate_tiers_missing_reason(org) is False

    def test_tier_override_and_pwyc_are_handled(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="22.00")
        _tier(event, vat_rate=Decimal("20"))
        _tier(event, name="Free", price=0, vat_rate=Decimal("0"))
        assert zero_rate_tiers_missing_reason(org) is False
        _tier(
            event,
            name="Pwyc",
            price=0,
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5"),
            vat_rate=Decimal("0"),
        )
        assert zero_rate_tiers_missing_reason(org) is True

    def test_category_priced_seated_tier_counts_as_paid(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        venue = Venue.objects.create(organization=organization, name="V", capacity=10)
        cat = PriceCategory.objects.create(venue=venue, name="P", color="#aa0000", display_order=0)
        _tier(
            event,
            price=0,
            venue=venue,
            sector=VenueSector.objects.create(venue=venue, name="S"),
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            category_prices={str(cat.id): "25.00"},
        )
        assert zero_rate_tiers_missing_reason(org) is True

    def test_query_count_does_not_grow_with_tiers(
        self, organization: Organization, event: Event, django_assert_max_num_queries: t.Any
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        for i in range(8):
            _tier(event, name=f"T{i}")
        with django_assert_max_num_queries(1):
            assert zero_rate_tiers_missing_reason(org) is True

    def test_billing_info_get_runs_the_scan_once_and_is_flat_in_tier_count(
        self, organization: Organization, event: Event, owner_client: Client
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        url = reverse("api:get_billing_info", kwargs={"slug": org.slug})
        _tier(event)
        with CaptureQueriesContext(connection) as few:
            first = owner_client.get(url)
        for i in range(8):
            _tier(event, name=f"T{i}")
        with CaptureQueriesContext(connection) as many:
            second = owner_client.get(url)

        assert first.json()["einvoice_missing_fields"] == ["vat_exemption_reason"]
        assert second.json()["einvoice_ready"] is False
        assert len(many) == len(few)
        scans = [q for q in many.captured_queries if q["sql"].startswith('SELECT 1 AS "a" FROM "events_tickettier"')]
        assert len(scans) == 1


class TestEnablingInvoicing:
    def test_enabling_with_zero_rate_tier_without_reason_is_422(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event)
        with pytest.raises(HttpError) as exc_info:
            set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
        assert exc_info.value.status_code == 422
        assert MESSAGE in str(exc_info.value)

    def test_enabling_with_reason_succeeds(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        org.vat_exemption_reason = "Art. 132"
        org.save()
        _tier(event)
        set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
        assert org.invoicing_mode == Organization.InvoicingMode.AUTO


class TestBillingInfoPatch:
    def test_zero_vat_rate_with_inheriting_tiers_is_422(
        self, organization: Organization, event: Event, owner_client: Client
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.AUTO)
        _tier(event)
        response = _patch(owner_client, org, {"vat_rate": "0"})
        assert response.status_code == 422
        assert MESSAGE in response.json()["detail"]
        org.refresh_from_db()
        assert org.vat_rate == Decimal("22.00")

    def test_clearing_the_org_reason_tiers_rely_on_is_422(
        self, organization: Organization, event: Event, owner_client: Client
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.AUTO, vat_rate="0.00")
        org.vat_exemption_reason = "Art. 132"
        org.save()
        _tier(event)
        response = _patch(owner_client, org, {"vat_exemption_reason": ""})
        assert response.status_code == 422
        org.refresh_from_db()
        assert org.vat_exemption_reason == "Art. 132"

    def test_same_patches_succeed_with_invoicing_none(
        self, organization: Organization, event: Event, owner_client: Client
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE)
        _tier(event)
        assert _patch(owner_client, org, {"vat_rate": "0"}).status_code == 200
        org.vat_exemption_reason = "Art. 132"
        org.save()
        assert _patch(owner_client, org, {"vat_exemption_reason": ""}).status_code == 200

    def test_zero_rate_patch_with_reason_in_same_request_succeeds(
        self, organization: Organization, event: Event, owner_client: Client
    ) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.AUTO)
        _tier(event)
        response = _patch(owner_client, org, {"vat_rate": "0", "vat_exemption_reason": "Art. 132"})
        assert response.status_code == 200


class TestZeroRateExclusions:
    """Tiers that can no longer sell never block enabling invoicing."""

    def test_paused_tier_does_not_flag(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event, sales_paused=True)
        assert zero_rate_tiers_missing_reason(org) is False

    def test_tier_with_ended_sales_window_does_not_flag(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event, sales_end_at=timezone.now() - timedelta(hours=1))
        assert zero_rate_tiers_missing_reason(org) is False
        _tier(event, name="Open", sales_end_at=timezone.now() + timedelta(hours=1))
        assert zero_rate_tiers_missing_reason(org) is True

    def test_cancelled_event_does_not_flag(self, organization: Organization, event: Event) -> None:
        org = _org(organization, mode=Organization.InvoicingMode.NONE, vat_rate="0.00")
        _tier(event)
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.CANCELLED)
        assert zero_rate_tiers_missing_reason(org) is False
