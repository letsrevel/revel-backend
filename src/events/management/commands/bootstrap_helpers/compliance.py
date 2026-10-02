"""E2E fixtures for EU country compliance (Journey 29 in USER_JOURNEYS.md, #1057-#1067).

One owner (``test.compliance@example.com`` / ``password123``) owns one organization per
country the journeys exercise. A country comes from ``vat_country_code`` (what the
billing-info API sets); an event held elsewhere carries its own ``vat_country_code``.
Everything here could be arranged through the API except tiers and drafts that must
*predate* the gate (an ONLINE tier in Italy, a draft invoice in Croatia), which only
the ORM can create, so they are seeded. So are two paid B2B sales whose invoice was
skipped (BE domestic, PL foreign buyer, #1091): a real one needs a Stripe checkout.
A subdivision with its own policy (the Basque Country, #1086) comes from the org's city.

Idempotent: re-running reuses the rows by slug / email.
"""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point

from accounts.models import RevelUser
from events import models as events_models
from events.compliance import BuyerContext
from events.compliance.enforcement import attendee_invoicing_for_sale, sale_nexus
from events.models.attendee_invoice import AttendeeInvoice
from geo.models import City

OWNER_EMAIL = "test.compliance@example.com"


class ComplianceOrgSpec(t.NamedTuple):
    """One seeded organization: slug, country, validated VAT ID, and a subdivision placed by its city."""

    slug: str
    country: str
    vat_id: str
    region: str = ""


# slug -> country. Empty country = "unknown"; US = non-EU (out of scope, default policy).
ORG_SPECS: t.Final[tuple[ComplianceOrgSpec, ...]] = (
    ComplianceOrgSpec("compliance-it", "IT", "IT12345678901"),
    ComplianceOrgSpec("compliance-at", "AT", "ATU12345678"),
    ComplianceOrgSpec("compliance-hr", "HR", "HR12345678901"),
    ComplianceOrgSpec("compliance-es", "ES", "ESB12345678"),
    # Basque Country (#1086): an ES org whose city is Bilbao falls under TicketBAI.
    ComplianceOrgSpec("compliance-es-pv", "ES", "ESB87654321", region="ES-PV"),
    # Navarre (#1086): Spain's 2027 block, worded for NaTicket instead of Verifactu.
    ComplianceOrgSpec("compliance-es-nc", "ES", "ESB11223344", region="ES-NC"),
    ComplianceOrgSpec("compliance-be", "BE", "BE0123456789"),
    ComplianceOrgSpec("compliance-pl", "PL", "PL1234567890"),
    ComplianceOrgSpec("compliance-dk", "DK", "DK12345678"),
    ComplianceOrgSpec("compliance-us", "US", ""),
    ComplianceOrgSpec("compliance-unknown", "", ""),
)


def _owner() -> RevelUser:
    owner = RevelUser.objects.filter(username=OWNER_EMAIL).first()
    if owner is None:
        owner = RevelUser.objects.create_user(
            username=OWNER_EMAIL,
            email=OWNER_EMAIL,
            password="password123",
            first_name="Compliance",
            last_name="Owner",
            email_verified=True,
        )
    return owner


def _bilbao() -> City:
    """Bilbao, Basque Country: the full city data has it; the e2e mini fixture may not."""
    city = City.objects.filter(ascii_name="Bilbao", iso2="ES", admin_name="Basque Country").first()
    if city is None:
        city, _ = City.objects.get_or_create(
            city_id=9724000001,  # not a worldcities id: never collides with real data
            defaults={
                "name": "Bilbao",
                "ascii_name": "Bilbao",
                "country": "Spain",
                "iso2": "ES",
                "iso3": "ESP",
                "admin_name": "Basque Country",
                "location": Point(-2.9236, 43.2569),
            },
        )
    return city


def _pamplona() -> City:
    """Pamplona, Navarre: the full city data has it; the e2e mini fixture may not."""
    city = City.objects.filter(ascii_name="Pamplona", iso2="ES", admin_name="Navarre").first()
    if city is None:
        city, _ = City.objects.get_or_create(
            city_id=9724000002,  # not a worldcities id: never collides with real data
            defaults={
                "name": "Pamplona",
                "ascii_name": "Pamplona",
                "country": "Spain",
                "iso2": "ES",
                "iso3": "ESP",
                "admin_name": "Navarre",
                "location": Point(-1.6432, 42.8125),
            },
        )
    return city


REGION_CITIES: t.Final[dict[str, t.Callable[[], City]]] = {"ES-PV": _bilbao, "ES-NC": _pamplona}


def _org(owner: RevelUser, spec: ComplianceOrgSpec) -> events_models.Organization:
    """A public, Stripe-connected (fake account), invoicing-ready organization in ``spec.country``."""
    city = {"city": REGION_CITIES[spec.region]()} if spec.region else {}
    org, _ = events_models.Organization.objects.update_or_create(
        slug=spec.slug,
        defaults={
            **city,
            "name": f"Compliance {spec.region or spec.country or 'Unknown'} Org",
            "owner": owner,
            "visibility": events_models.Organization.Visibility.PUBLIC,
            "vat_country_code": spec.country,
            "vat_id": spec.vat_id,
            "vat_id_validated": bool(spec.vat_id),
            "vat_rate": Decimal("20.00"),
            "billing_name": f"Compliance {spec.region or spec.country or 'Unknown'} Legal Entity",
            "billing_address": "Main Street 1",
            "billing_email": f"billing+{spec.slug}@example.com",
            "stripe_account_id": f"acct_e2e_{spec.slug.replace('-', '_')}",
            "stripe_charges_enabled": True,
            "stripe_details_submitted": True,
        },
    )
    return org


def _event(
    org: events_models.Organization, slug: str, name: str, start: datetime, **extra: t.Any
) -> events_models.Event:
    event, _ = events_models.Event.objects.update_or_create(
        organization=org,
        slug=slug,
        defaults={
            "name": name,
            "event_type": events_models.Event.EventType.PUBLIC,
            "visibility": events_models.Event.Visibility.PUBLIC,
            "status": events_models.Event.EventStatus.OPEN,
            "start": start,
            "end": start + timedelta(hours=4),
            "requires_ticket": True,
            "can_attend_without_login": True,
            "max_tickets_per_user": 5,
            **extra,
        },
    )
    return event


def _tier(event: events_models.Event, name: str, method: str, price: str, **extra: t.Any) -> events_models.TicketTier:
    tier, _ = events_models.TicketTier.objects.update_or_create(
        event=event,
        name=name,
        defaults={"payment_method": method, "price": Decimal(price), "currency": "EUR", **extra},
    )
    return tier


def _skipped_b2b_sale(
    buyer: RevelUser, tier: events_models.TicketTier, session_id: str, vat_id: str
) -> events_models.SkippedFiscalDocument:
    """A paid online sale to a business buyer whose attendee invoice the country policy skipped (#1091).

    Keyed on the fixed session: ``update_or_create`` re-attaches the SET_NULL foreign keys
    after a reset (#1083) and reopens a document a previous run resolved.
    """
    event = tier.event
    org = event.organization
    rate = "20.00"
    gross = tier.price
    net = (gross / Decimal("1.20")).quantize(Decimal("0.01"))
    vat = gross - net
    snapshot: dict[str, t.Any] = {
        "billing_name": f"E2E Business {vat_id[:2]}",
        "vat_id": vat_id,
        "vat_country_code": vat_id[:2],
        "vat_id_validated": True,
        "vat_id_status": "valid",
        "billing_address": "Business Street 1",
        "billing_email": f"e2e.business.{vat_id[:2].lower()}@example.com",
        "reverse_charge": False,
    }
    payment = events_models.Payment.objects.filter(stripe_session_id=session_id).first()
    if payment is None:
        ticket = events_models.Ticket.objects.create(
            event=event, tier=tier, user=buyer, status=events_models.Ticket.TicketStatus.ACTIVE, guest_name="E2E Buyer"
        )
        payment = events_models.Payment.objects.create(
            ticket=ticket,
            user=buyer,
            stripe_session_id=session_id,
            status=events_models.Payment.PaymentStatus.SUCCEEDED,
            amount=gross,
            net_amount=net,
            vat_amount=vat,
            vat_rate=Decimal(rate),
            platform_fee=Decimal("0.00"),
            currency="EUR",
            buyer_billing_snapshot=snapshot,
        )
    decision = attendee_invoicing_for_sale(sale_nexus(org, [event]), BuyerContext.from_billing_snapshot(snapshot))
    line = {
        "description": f"{event.name} — {tier.name} — E2E Buyer",
        "unit_price_gross": str(gross),
        "discount_amount": "0.00",
        "net_amount": str(net),
        "vat_amount": str(vat),
        "vat_rate": rate,
    }
    doc, _ = events_models.SkippedFiscalDocument.objects.update_or_create(
        stripe_session_id=session_id,
        kind=events_models.SkippedFiscalDocument.Kind.INVOICE,
        defaults={
            "reason_code": decision.code,
            "policy_country": decision.country,
            "reason": decision.reason,
            "organization": org,
            "event": event,
            "user": buyer,
            "buyer_name": snapshot["billing_name"],
            "buyer_email": snapshot["billing_email"],
            "buyer_vat_id": vat_id,
            "buyer_vat_country": vat_id[:2],
            "buyer_address": snapshot["billing_address"],
            "buyer_vat_id_status": "valid",
            "currency": "EUR",
            "total_gross": gross,
            "total_net": net,
            "total_vat": vat,
            "line_items": [line],
            "notified_at": None,
            "resolved_at": None,
            "resolved_by": None,
            "external_reference": "",
        },
    )
    doc.payments.set([payment])
    return doc


def create_compliance_fixtures(now: datetime) -> dict[str, events_models.Organization]:
    """Seed the compliance journeys' organizations, events, tiers and one stale draft invoice.

    Args:
        now: The bootstrap's reference time (events start a week later).

    Returns:
        The seeded organizations by slug.
    """
    method = events_models.TicketTier.PaymentMethod
    owner = _owner()
    orgs = {spec.slug: _org(owner, spec) for spec in ORG_SPECS}
    start = now + timedelta(days=7)

    # Italy: online payment blocked for events held there.
    it_org = orgs["compliance-it"]
    club = _event(it_org, "it-club-night", "IT Club Night", start)
    _tier(club, "Door", method.AT_THE_DOOR, "10.00")
    _tier(club, "Bank transfer", method.OFFLINE, "15.00", manual_payment_instructions="IBAN IT00 0000 0000")
    _tier(club, "Pay what you can (offline)", method.OFFLINE, "0.00", price_type="pwyc", pwyc_min=Decimal("5.00"))
    _tier(club, "Free entry", method.FREE, "0.00")
    _tier(club, "Card (legacy)", method.ONLINE, "20.00")  # predates the gate: banner + checkout 422
    _tier(club, "Card (paused)", method.ONLINE, "20.00", sales_paused=True)  # resume -> 422
    talk = _event(it_org, "it-online-talk", "IT Online Talk", start, is_virtual=True)
    _tier(talk, "Stream", method.ONLINE, "8.00")  # virtual: not reached by Italy's rule
    series = events_models.EventSeries.objects.update_or_create(
        organization=it_org, slug="it-season", defaults={"name": "IT Season"}
    )[0]
    series_pass = events_models.SeriesPass.objects.update_or_create(
        event_series=series,
        name="IT Season Pass",
        defaults={
            "price": Decimal("30.00"),
            "pro_rata_discount": Decimal("0.00"),
            "currency": "EUR",
            "payment_method": method.ONLINE,
        },
    )[0]
    for i in range(2):
        night = _event(
            it_org, f"it-season-{i}", f"IT Season Night {i + 1}", start + timedelta(days=i), event_series=series
        )
        events_models.SeriesPassTierLink.objects.get_or_create(
            series_pass=series_pass, event=night, defaults={"tier": _tier(night, "Season", method.ONLINE, "15.00")}
        )

    # Austria: an event held in Italy (territorial rule) and one at home (Registrierkasse notice).
    at_org = orgs["compliance-at"]
    _tier(
        _event(at_org, "at-gig-in-italy", "AT Gig in Milan", start, vat_country_code="IT"),
        "Door",
        method.AT_THE_DOOR,
        "12.00",
    )
    _tier(_event(at_org, "at-gig-vienna", "AT Gig in Vienna", start), "Door", method.AT_THE_DOOR, "12.00")

    # Denmark: sales-registration notice next to the door/offline setting.
    _tier(_event(orgs["compliance-dk"], "dk-disco-night", "DK Disco Night", start), "Door", method.AT_THE_DOOR, "9.00")

    # Poland: kasa fiskalna notice next to the ticket-sales settings; it covers online sales too (#1067).
    pl_card = _tier(
        _event(orgs["compliance-pl"], "pl-dance-night", "PL Dance Night", start), "Card", method.ONLINE, "15.00"
    )
    # Skipped B2B invoices (#1091): a Belgian buyer of a Belgian org (Peppol), a Dutch buyer of a Polish one (KSeF).
    be_card = _tier(
        _event(orgs["compliance-be"], "be-business-summit", "BE Business Summit", start),
        "Card",
        method.ONLINE,
        "120.00",
    )
    _skipped_b2b_sale(owner, be_card, "cs_e2e_compliance_be_skipped", "BE0123456789")
    _skipped_b2b_sale(owner, pl_card, "cs_e2e_compliance_pl_skipped", "NL123456789B01")

    # Croatia: a HYBRID draft that predates the gate and can no longer be issued. update_or_create, not
    # get_or_create: the invoice's FKs are SET_NULL, so a reset leaves an orphan to re-attach (#1083).
    hr_org = orgs["compliance-hr"]
    hr_event = _event(hr_org, "hr-concert", "HR Concert", start)
    AttendeeInvoice.objects.update_or_create(
        stripe_session_id="cs_e2e_compliance_hr_draft",
        defaults={
            "organization": hr_org,
            "user": owner,
            "event": hr_event,
            "invoice_number": "COMPLIANCEHR-2026-000001",
            "status": AttendeeInvoice.InvoiceStatus.DRAFT,
            "total_gross": Decimal("25.00"),
            "total_net": Decimal("20.00"),
            "total_vat": Decimal("5.00"),
            "vat_rate": Decimal("25.00"),
            "currency": "EUR",
            "line_items": [
                {
                    "description": "HR Concert — General",
                    "unit_price_gross": "25.00",
                    "discount_amount": "0.00",
                    "net_amount": "20.00",
                    "vat_amount": "5.00",
                    "vat_rate": "25.00",
                }
            ],
            "seller_name": hr_org.billing_name,
            "seller_vat_id": hr_org.vat_id,
            "seller_vat_country": "HR",
            "seller_email": hr_org.billing_email,
            "buyer_name": "E2E Buyer",
            "buyer_email": "e2e.buyer@example.com",
        },
    )
    return orgs
