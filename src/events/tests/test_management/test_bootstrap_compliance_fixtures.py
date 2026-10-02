"""The EU compliance E2E fixtures (Journey 29) seed what the journeys assert."""

import pytest
from django.utils import timezone

from events.compliance import AttendeeInvoicingCapability, PaymentChannelCapability, get_policy
from events.compliance.base import ALL_NEXUS
from events.compliance.enforcement import event_compliance
from events.compliance.policies.es import NavarrePolicy
from events.management.commands.bootstrap_helpers.compliance import OWNER_EMAIL, create_compliance_fixtures
from events.models import Event, Organization, SeriesPass, SkippedFiscalDocument, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_invoice_service import invoice_compliance_decision

pytestmark = pytest.mark.django_db


def test_seed_matches_the_journey_preconditions() -> None:
    orgs = create_compliance_fixtures(timezone.now())

    assert {slug: get_policy(org).jurisdiction for slug, org in orgs.items()} == {
        "compliance-it": "IT",
        "compliance-at": "AT",
        "compliance-hr": "HR",
        "compliance-es": "ES",
        "compliance-es-pv": "ES-PV",
        "compliance-es-nc": "ES-NC",
        "compliance-be": "BE",
        "compliance-pl": "PL",
        "compliance-dk": "DK",
        "compliance-us": "US",
        "compliance-unknown": "",
    }
    assert all(org.owner.email == OWNER_EMAIL for org in orgs.values())
    assert get_policy(orgs["compliance-hr"]).attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED
    # Basque Country (#1086): blocked from today (TicketBAI), with no Verifactu heads-up.
    basque = get_policy(orgs["compliance-es-pv"])
    assert basque.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED
    assert basque.organizer_notices(ALL_NEXUS) == []
    # Navarre: Spain's 2027 date, NaTicket wording, never the Verifactu notice.
    navarre = get_policy(orgs["compliance-es-nc"])
    block_from = NavarrePolicy.fiscal_invoicing_from
    assert block_from is not None
    before_block = timezone.localdate() < block_from  # stays valid across the date
    assert navarre.attendee_invoicing_capability() == (
        AttendeeInvoicingCapability.ALLOWED if before_block else AttendeeInvoicingCapability.BLOCKED
    )
    assert [n.key for n in navarre.organizer_notices(ALL_NEXUS)] == (["es_nc_naticket"] if before_block else [])

    club = Event.objects.get(slug="it-club-night")
    assert event_compliance(club).online_payment == PaymentChannelCapability.BLOCKED
    assert TicketTier.objects.filter(event=club, payment_method=TicketTier.PaymentMethod.ONLINE).count() == 2
    assert TicketTier.objects.get(event=club, name="Card (paused)").sales_paused
    assert event_compliance(Event.objects.get(slug="it-online-talk")).online_payment == PaymentChannelCapability.ALLOWED
    assert (
        event_compliance(Event.objects.get(slug="at-gig-in-italy")).online_payment == PaymentChannelCapability.BLOCKED
    )
    assert [n.key for n in event_compliance(Event.objects.get(slug="at-gig-vienna")).notices] == ["at_registrierkasse"]
    assert [n.key for n in event_compliance(Event.objects.get(slug="pl-dance-night")).notices] == ["pl_kasa_fiskalna"]
    assert [n.key for n in event_compliance(Event.objects.get(slug="hr-concert")).notices] == ["hr_fiscalization"]
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


def test_reseed_reattaches_the_hr_draft_after_a_reset() -> None:
    """A reset deletes the orgs; the draft survives orphaned (SET_NULL) and the next seed must re-attach it (#1083)."""
    now = timezone.now()
    create_compliance_fixtures(now)
    Organization.objects.filter(slug="compliance-hr").delete()
    assert AttendeeInvoice.objects.get(stripe_session_id="cs_e2e_compliance_hr_draft").organization is None

    create_compliance_fixtures(now)

    draft = AttendeeInvoice.objects.get(stripe_session_id="cs_e2e_compliance_hr_draft")
    assert draft.organization is not None and draft.organization.slug == "compliance-hr"
    assert draft.event is not None and draft.event.slug == "hr-concert"
    assert draft.status == AttendeeInvoice.InvoiceStatus.DRAFT


def test_seed_has_one_skipped_be_and_one_skipped_pl_invoice() -> None:
    """Journey 29.9: the BE domestic and PL foreign business sales, each linked to its ticket (#1091)."""
    create_compliance_fixtures(timezone.now())

    docs = {d.policy_country: d for d in SkippedFiscalDocument.objects.select_related("organization")}
    assert set(docs) == {"BE", "PL"}
    assert docs["BE"].organization is not None and docs["BE"].organization.slug == "compliance-be"
    assert (docs["BE"].buyer_vat_id, docs["PL"].buyer_vat_id) == ("BE0123456789", "NL123456789B01")
    assert all(d.reason_code == SkippedFiscalDocument.ReasonCode.B2B_E_INVOICING for d in docs.values())
    assert all(d.payments.count() == 1 for d in docs.values())


def test_reseed_reopens_and_reattaches_skipped_documents() -> None:
    now = timezone.now()
    create_compliance_fixtures(now)
    SkippedFiscalDocument.objects.update(resolved_at=now, external_reference="EXT-1")
    Organization.objects.filter(slug="compliance-be").delete()

    create_compliance_fixtures(now)

    assert SkippedFiscalDocument.objects.count() == 2
    be = SkippedFiscalDocument.objects.get(policy_country="BE")
    assert be.organization is not None and be.organization.slug == "compliance-be"
    assert be.payments.get().ticket.event.slug == "be-business-summit"
    assert not SkippedFiscalDocument.objects.filter(resolved_at__isnull=False).exists()
