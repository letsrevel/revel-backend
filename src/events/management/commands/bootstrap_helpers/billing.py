# src/events/management/commands/bootstrap_helpers/billing.py
"""VAT/billing setup and invoice generation for bootstrap process."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import structlog
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events import models as events_models
from events.service.invoice_service import (
    _render_invoice_pdf,
    generate_invoices_for_period,
)
from events.service.vat_service import calculate_platform_fee_vat, calculate_vat_inclusive, get_effective_vat_rate

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def configure_platform_billing() -> SiteSettings:
    """Populate SiteSettings with realistic platform billing info."""
    logger.info("Configuring platform billing settings...")

    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel Technologies S.r.l."
    site.platform_business_address = "Via della Conciliazione 44, 00193 Roma RM, Italy"
    site.platform_vat_id = "IT12345678903"
    site.platform_vat_country = "IT"
    site.platform_vat_rate = Decimal("22.00")
    site.platform_invoice_bcc_email = "accounting@letsrevel.io"
    site.save()

    logger.info("Platform billing configured", vat_id=site.platform_vat_id, vat_country=site.platform_vat_country)
    return site


def configure_organization_billing(state: BootstrapState) -> None:
    """Add realistic VAT/billing info to bootstrap organizations."""
    logger.info("Configuring organization billing info...")

    # Revel Events Collective — Austrian org with validated VAT ID (EU cross-border → reverse charge)
    org_alpha = state.orgs["alpha"]
    org_alpha.vat_id = "ATU12345678"
    org_alpha.vat_country_code = "AT"
    org_alpha.vat_rate = Decimal("20.00")  # Austrian standard VAT rate
    org_alpha.vat_id_validated = True
    org_alpha.vat_id_validated_at = timezone.now() - timedelta(days=15)
    org_alpha.vies_request_identifier = "WAPIAAAAXxx0TEST"
    org_alpha.billing_address = "Musterstraße 42\nAT-1010 Wien"
    org_alpha.billing_email = "billing@revelcollective.example.com"
    org_alpha.save()

    logger.info(
        "org_alpha_billing_configured",
        vat_id=org_alpha.vat_id,
        country=org_alpha.vat_country_code,
    )

    # Tech Innovators Network — German org with validated VAT ID (also EU cross-border)
    org_beta = state.orgs["beta"]
    org_beta.vat_id = "DE123456789"
    org_beta.vat_country_code = "DE"
    org_beta.vat_rate = Decimal("19.00")  # German standard VAT rate
    org_beta.vat_id_validated = True
    org_beta.vat_id_validated_at = timezone.now() - timedelta(days=10)
    org_beta.vies_request_identifier = "WAPIAAAAYdd3O5ab"
    org_beta.billing_address = "Friedrichstraße 123\n10117 Berlin, Germany"
    org_beta.billing_email = "billing@techinnovators.example.com"
    org_beta.save()

    logger.info(
        "org_beta_billing_configured",
        vat_id=org_beta.vat_id,
        country=org_beta.vat_country_code,
    )


def _create_bootstrap_payments(
    users: list[RevelUser],
    event: events_models.Event,
    tier: events_models.TicketTier,
    org: events_models.Organization,
    site: SiteSettings,
    effective_vat_rate: Decimal,
    first_of_previous: date,
    last_of_previous: date,
) -> int:
    """Create bootstrap payment records spread across the previous month."""
    payments_created = 0
    for i, user in enumerate(users):
        payment_day = min(first_of_previous.day + (i * 6) + 3, last_of_previous.day)
        payment_date = first_of_previous.replace(day=payment_day)

        ticket_vat = calculate_vat_inclusive(tier.price, effective_vat_rate)
        platform_fee_amount = (tier.price * Decimal("0.10")).quantize(Decimal("0.01"))
        fee_vat = calculate_platform_fee_vat(
            platform_fee_amount, org, site.platform_vat_country, site.platform_vat_rate
        )

        ticket = events_models.Ticket.objects.create(
            event=event,
            user=user,
            tier=tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
            guest_name=f"Bootstrap Guest {i + 1}",
        )

        payment = events_models.Payment(
            ticket=ticket,
            user=user,
            stripe_session_id=f"cs_bootstrap_{i + 1}_{first_of_previous.isoformat()}",
            status=events_models.Payment.PaymentStatus.SUCCEEDED,
            amount=tier.price,
            platform_fee=platform_fee_amount,
            currency=tier.currency,
            net_amount=ticket_vat.net_amount,
            vat_amount=ticket_vat.vat_amount,
            vat_rate=ticket_vat.vat_rate,
            platform_fee_net=fee_vat.fee_net,
            platform_fee_vat=fee_vat.fee_vat,
            platform_fee_vat_rate=fee_vat.fee_vat_rate,
            platform_fee_reverse_charge=fee_vat.reverse_charge,
        )
        payment.save()
        events_models.Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.make_aware(datetime(payment_date.year, payment_date.month, payment_date.day, 14, 30))
        )
        payments_created += 1
    return payments_created


def create_payments_and_invoice(state: BootstrapState) -> None:
    """Create realistic Payment records for last month and generate an invoice.

    Creates succeeded payments for the Revel Events Collective (org_alpha)
    for the previous month, then runs the invoice generation service to
    produce a real invoice with PDF.
    """
    logger.info("Creating payments and generating invoice...")

    site = SiteSettings.get_solo()
    org = state.orgs["alpha"]

    today = date.today()
    first_of_current = today.replace(day=1)
    last_of_previous = first_of_current - timedelta(days=1)
    first_of_previous = last_of_previous.replace(day=1)

    # Find a ticketed event from org_alpha with an ONLINE paid EUR tier
    # Prefer EUR since the platform is in Italy
    event = None
    tier = None
    for evt_key in ("seated_concert", "wellness_retreat", "summer_festival"):
        if evt_key not in state.events:
            continue
        evt = state.events[evt_key]
        if evt.organization_id != org.id:
            continue
        found_tier = (
            events_models.TicketTier.objects.filter(
                event=evt,
                payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
                price__gt=0,
                currency="EUR",
            )
            .exclude(price_type=events_models.TicketTier.PriceType.PWYC)
            .first()
        )
        if found_tier:
            event = evt
            tier = found_tier
            break

    if not event or not tier:
        logger.warning("no_suitable_tier_for_payments", org=org.slug)
        return

    effective_vat_rate = get_effective_vat_rate(tier.vat_rate, org.vat_rate)

    # Create 5 payments spread across last month
    resolved_users: list[RevelUser] = [
        state.users[k] for k in ("org_alpha_member", "multi_org_user", "attendee_1", "attendee_2") if k in state.users
    ]
    if not resolved_users:
        logger.warning("no_users_for_payments")
        return

    payments_created = _create_bootstrap_payments(
        users=resolved_users,
        event=event,
        tier=tier,
        org=org,
        site=site,
        effective_vat_rate=effective_vat_rate,
        first_of_previous=first_of_previous,
        last_of_previous=last_of_previous,
    )

    logger.info(
        "payments_created",
        count=payments_created,
        period=f"{first_of_previous.isoformat()} to {last_of_previous.isoformat()}",
        org=org.slug,
    )

    # Generate invoice for last month
    invoices = generate_invoices_for_period(first_of_previous, last_of_previous)

    for invoice in invoices:
        # If PDF wasn't generated (e.g. WeasyPrint not available), try rendering it
        if not invoice.pdf_file:
            try:
                pdf_bytes = _render_invoice_pdf(invoice)
                invoice.pdf_file.save(
                    f"{invoice.invoice_number}.pdf",
                    ContentFile(pdf_bytes),
                    save=True,
                )
                logger.info("invoice_pdf_generated", invoice_number=invoice.invoice_number)
            except Exception:
                logger.warning("invoice_pdf_generation_failed", invoice_number=invoice.invoice_number, exc_info=True)
        else:
            logger.info("invoice_already_has_pdf", invoice_number=invoice.invoice_number)

    logger.info("invoices_generated", count=len(invoices))
