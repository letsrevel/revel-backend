"""The EU compliance E2E fixtures (Journey 29) seed what the journeys assert."""

import pytest
from django.utils import timezone

from events.compliance import AttendeeInvoicingCapability, PaymentChannelCapability, get_policy
from events.compliance.enforcement import event_compliance
from events.management.commands.bootstrap_helpers.compliance import OWNER_EMAIL, create_compliance_fixtures
from events.models import Event, SeriesPass, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_service import invoice_compliance_decision

pytestmark = pytest.mark.django_db


def test_seed_matches_the_journey_preconditions() -> None:
    orgs = create_compliance_fixtures(timezone.now())

    assert {slug: get_policy(org).country for slug, org in orgs.items()} == {
        "compliance-it": "IT",
        "compliance-at": "AT",
        "compliance-hr": "HR",
        "compliance-es": "ES",
        "compliance-be": "BE",
        "compliance-pl": "PL",
        "compliance-dk": "DK",
        "compliance-us": "US",
        "compliance-unknown": "",
    }
    assert all(org.owner.email == OWNER_EMAIL for org in orgs.values())
    assert get_policy(orgs["compliance-hr"]).attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED

    club = Event.objects.get(slug="it-club-night")
    assert event_compliance(club).online_payment == PaymentChannelCapability.BLOCKED
    assert TicketTier.objects.filter(event=club, payment_method=TicketTier.PaymentMethod.ONLINE).count() == 2
    assert TicketTier.objects.get(event=club, name="Card (paused)").sales_paused
    assert event_compliance(Event.objects.get(slug="it-online-talk")).online_payment == PaymentChannelCapability.ALLOWED
    assert (
        event_compliance(Event.objects.get(slug="at-gig-in-italy")).online_payment == PaymentChannelCapability.BLOCKED
    )
    assert [n.key for n in event_compliance(Event.objects.get(slug="at-gig-vienna")).notices] == ["at_registrierkasse"]
    assert SeriesPass.objects.get(name="IT Season Pass").tier_links.count() == 2

    draft = AttendeeInvoice.objects.get(stripe_session_id="cs_e2e_compliance_hr_draft")
    assert draft.status == AttendeeInvoice.InvoiceStatus.DRAFT
    assert not invoice_compliance_decision(draft).allowed


def test_seed_is_idempotent() -> None:
    now = timezone.now()
    create_compliance_fixtures(now)
    counts = (Event.objects.count(), TicketTier.objects.count(), AttendeeInvoice.objects.count())

    create_compliance_fixtures(now)

    assert (Event.objects.count(), TicketTier.objects.count(), AttendeeInvoice.objects.count()) == counts
