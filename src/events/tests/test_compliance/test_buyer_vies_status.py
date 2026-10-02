"""The BE/PL domestic-B2B gate honours the checkout VIES outcome (EU layer 1, #1066/#1067).

A buyer whose VAT ID VIES rejected is a consumer, so their invoice is issued; a valid
or unverifiable ID counts as a domestic business. Legacy snapshots keep the old
prefix-only behaviour.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.service.vies_service import VIESUnavailableError, VIESValidationResult
from events.compliance import BuyerContext, get_policy_for_country
from events.compliance.base import Nexus
from events.models import Event, Organization, Payment, TicketTier
from events.models.attendee_invoice import VatIdStatus
from events.schema import TicketPurchaseItem
from events.schema.checkout import BuyerBillingInfoSchema
from events.service.attendee_invoice_service import generate_attendee_invoice
from events.service.batch_ticket_service import BatchTicketService
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)

pytestmark = pytest.mark.django_db

EST = frozenset({Nexus.ESTABLISHMENT})
MOCK_VIES = "common.service.vies_service.validate_vat_id_cached"


def _snapshot(vat_id: str, **extra: t.Any) -> dict[str, t.Any]:
    return {"vat_id": vat_id, "vat_country_code": vat_id[:2], **extra}


@pytest.mark.parametrize("code", ["BE", "PL"])
@pytest.mark.parametrize(
    ("label", "extra", "domestic_business"),
    [
        ("valid", {"vat_id_status": "valid", "vat_id_validated": True}, True),
        ("invalid", {"vat_id_status": "invalid", "vat_id_validated": False}, False),
        ("unavailable", {"vat_id_status": "unavailable", "vat_id_validated": False}, True),
        ("legacy validated", {"vat_id_validated": True}, True),
        ("legacy no key", {}, True),
    ],
)
def test_status_decides_the_domestic_b2b_gate(
    code: str, label: str, extra: dict[str, t.Any], domestic_business: bool
) -> None:
    buyer = BuyerContext.from_billing_snapshot(_snapshot(f"{code}0123456789", **extra))

    decision = get_policy_for_country(code).attendee_invoicing(buyer, EST)

    assert decision.allowed is not domestic_business, label


@pytest.mark.parametrize(("code", "blocked"), [("BE", False), ("PL", True)])
def test_foreign_business_buyer(code: str, blocked: bool) -> None:
    """A valid foreign VAT ID: outside Peppol's domestic scope, inside KSeF's."""
    buyer = BuyerContext.from_billing_snapshot(_snapshot("DE123456789", vat_id_status="valid"))
    assert get_policy_for_country(code).attendee_invoicing(buyer, EST).allowed is not blocked


@pytest.mark.parametrize("code", ["BE", "PL"])
def test_vies_invalid_foreign_id_is_a_consumer(code: str) -> None:
    buyer = BuyerContext.from_billing_snapshot(_snapshot("DE123456789", vat_id_status="invalid"))
    assert get_policy_for_country(code).attendee_invoicing(buyer, EST).allowed


def test_no_snapshot_is_a_consumer() -> None:
    assert BuyerContext.from_billing_snapshot(None) == BuyerContext()


@pytest.mark.parametrize(
    ("vat_id", "valid", "status"),
    [("BE1", True, "valid"), ("BE1", False, "invalid"), ("BE1", None, "unavailable"), ("", None, "")],
)
def test_vat_id_status_from_vies(vat_id: str, valid: bool | None, status: str) -> None:
    assert VatIdStatus.from_vies(vat_id, valid) == status


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@pytest.mark.parametrize(("status", "invoiced"), [("invalid", True), ("valid", False), ("unavailable", False)])
def test_generation_issues_the_invoice_for_a_rejected_vat_id(
    _pdf: t.Any,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
    status: str,
    invoiced: bool,
) -> None:
    _make_org_invoicing_ready(organization)
    organization.vat_country_code = "BE"
    organization.vat_id = "BE0999999999"
    organization.invoicing_mode = Organization.InvoicingMode.AUTO
    organization.save()
    snapshot = {**_default_billing_snapshot(), "vat_id": "BE0123456789", "vat_country_code": "BE"}
    snapshot["vat_id_status"] = status
    _create_payment(user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=snapshot)

    assert (generate_attendee_invoice("cs_test_123") is not None) is invoiced


class TestCheckoutRecordsTheStatus:
    @pytest.fixture
    def online_tier(self, organization: Organization) -> TicketTier:
        organization.stripe_account_id = "acct_vies"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.vat_country_code = "BE"
        organization.vat_rate = Decimal("21.00")
        organization.billing_name = "Org Legal Entity"
        organization.billing_address = "Main Street 1"
        organization.save()
        event = Event.objects.create(
            organization=organization,
            name="Gig",
            slug="gig",
            event_type=Event.EventType.PUBLIC,
            start=timezone.now() + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )
        return TicketTier.objects.create(
            event=event, name="Online", price=Decimal("20.00"), payment_method=TicketTier.PaymentMethod.ONLINE
        )

    def _checkout(self, tier: TicketTier, user: RevelUser, vat_id: str) -> dict[str, t.Any]:
        billing = BuyerBillingInfoSchema(billing_name="Buyer", vat_id=vat_id)  # type: ignore[call-arg]
        BatchTicketService(tier.event, tier, user).create_batch(
            [TicketPurchaseItem(guest_name="Guest")], billing_info=billing
        )
        payment = Payment.objects.get(ticket__tier=tier)
        return t.cast(dict[str, t.Any], payment.buyer_billing_snapshot)

    @pytest.mark.parametrize(
        ("vies", "status"),
        [
            (VIESValidationResult(valid=True, name="", address="", request_identifier=""), "valid"),
            (VIESValidationResult(valid=False, name="", address="", request_identifier=""), "invalid"),
            (VIESUnavailableError("down"), "unavailable"),
        ],
    )
    def test_each_vies_outcome_is_recorded(
        self, online_tier: TicketTier, member_user: RevelUser, vies: t.Any, status: str
    ) -> None:
        with patch(MOCK_VIES, side_effect=vies if isinstance(vies, Exception) else None, return_value=vies):
            snapshot = self._checkout(online_tier, member_user, "BE0123456789")

        assert snapshot["vat_id_status"] == status
        assert snapshot["vat_id_validated"] is (status == "valid")

    def test_no_vat_id_records_empty_status(self, online_tier: TicketTier, member_user: RevelUser) -> None:
        with patch(MOCK_VIES) as vies:
            snapshot = self._checkout(online_tier, member_user, "")

        vies.assert_not_called()
        assert snapshot["vat_id_status"] == ""
