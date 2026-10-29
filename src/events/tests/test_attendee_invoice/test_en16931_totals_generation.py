"""New attendee invoices carry EN 16931 totals and the BT-114 rounding amount (#1234)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import TotalsMethod
from events.service.attendee_invoice_service import generate_attendee_invoice
from events.service.en16931_totals_service import compute_en16931_totals
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"):
        yield


@pytest.fixture
def auto_org(organization: Organization) -> Organization:
    org = _make_org_invoicing_ready(organization)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


def _sale(
    user: RevelUser, event: Event, tier: TicketTier, session_id: str, gross: str, rate: str, guest: str = "Guest"
) -> None:
    """A succeeded payment whose checkout-time net/VAT are the legacy per-ticket figures."""
    amount = Decimal(gross)
    net = (amount * 100 / (100 + Decimal(rate))).quantize(Decimal("0.01"))
    _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session_id,
        amount=amount,
        net_amount=net,
        vat_amount=amount - net,
        vat_rate=Decimal(rate),
        buyer_billing_snapshot=_default_billing_snapshot(),
        guest_name=guest,
    )


class TestGeneration:
    def test_new_invoice_is_en16931_with_rounding(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Review focus 1: 24.90 at 13% cannot be reached from any 2-dp net, so BT-114 carries the cent."""
        _sale(member_user, event, event_ticket_tier, "cs_en_1", "24.90", "13.00")
        invoice = generate_attendee_invoice("cs_en_1")
        assert invoice is not None
        assert invoice.total_gross == Decimal("24.90")
        assert invoice.total_net == Decimal("22.04")
        assert invoice.total_vat == Decimal("2.87")
        assert invoice.rounding_amount == Decimal("-0.01")
        assert invoice.totals_method == "en16931"
        assert invoice.line_items[0]["net_amount"] == "22.04"
        assert invoice.line_items[0]["vat_amount"] == "2.87"
        assert invoice.line_items[0]["unit_price_gross"] == "24.90"

    def test_new_invoice_reachable_has_zero_rounding(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _sale(member_user, event, event_ticket_tier, "cs_en_2", "25.00", "13.00")
        invoice = generate_attendee_invoice("cs_en_2")
        assert invoice is not None
        assert (invoice.total_net, invoice.total_vat, invoice.rounding_amount) == (
            Decimal("22.12"),
            Decimal("2.88"),
            Decimal("0.00"),
        )
        assert invoice.totals_method == TotalsMethod.EN16931

    def test_existing_rows_default_to_legacy(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """The column default marks every pre-existing row legacy; only generation stamps en16931."""
        invoice = _create_draft(organization, event, member_user)
        invoice.refresh_from_db()
        assert invoice.totals_method == "legacy"
        assert invoice.rounding_amount == 0

    def test_detail_api_exposes_totals_method_and_rounding(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _sale(member_user, event, event_ticket_tier, "cs_en_3", "24.90", "13.00")
        invoice = generate_attendee_invoice("cs_en_3")
        assert invoice is not None
        refresh = RefreshToken.for_user(auto_org.owner)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
        url = reverse("api:get_attendee_invoice", kwargs={"slug": auto_org.slug, "invoice_id": str(invoice.id)})
        response = client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["totals_method"] == "en16931"
        assert Decimal(data["rounding_amount"]) == Decimal("-0.01")

    def test_buyer_and_list_api_expose_rounding(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Review fix 4: the base invoice schema carries BT-114, so the buyer and list payloads have it too."""
        _sale(member_user, event, event_ticket_tier, "cs_en_5", "24.90", "13.00")
        assert generate_attendee_invoice("cs_en_5") is not None
        for user, url in (
            (member_user, reverse("api:dashboard_invoices")),
            (auto_org.owner, reverse("api:list_attendee_invoices", kwargs={"slug": auto_org.slug})),
        ):
            refresh = RefreshToken.for_user(user)
            client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token!s}")  # type: ignore[attr-defined]
            response = client.get(url)
            assert response.status_code == 200
            payload = response.json()["results"][0]
            assert Decimal(payload["rounding_amount"]) == Decimal("-0.01")
            assert payload["totals_method"] == "en16931"

    def test_vat_breakdown_matches_groups(
        self, auto_org: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """The per-rate buckets a consumer reads are exactly the EN 16931 groups the header was built from."""
        _sale(member_user, event, event_ticket_tier, "cs_en_4", "24.90", "13.00", "A")
        _sale(member_user, event, event_ticket_tier, "cs_en_4", "100.00", "22.00", "B")
        _sale(member_user, event, event_ticket_tier, "cs_en_4", "24.90", "13.00", "C")
        invoice = generate_attendee_invoice("cs_en_4")
        assert invoice is not None
        groups = {g.rate: g for g in compute_en16931_totals(invoice.line_items, invoice.reverse_charge).groups}
        assert len(groups) == 2
        for bucket in invoice.vat_breakdown:
            group = groups[bucket["vat_rate"]]
            assert bucket["net_amount"] == group.net
            assert bucket["vat_amount"] == group.vat
            assert bucket["gross_amount"] == group.gross
        assert invoice.total_net + invoice.total_vat + invoice.rounding_amount == invoice.total_gross
