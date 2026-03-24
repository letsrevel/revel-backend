"""Referral payout statement generation service.

Generates self-billing invoices (Gutschrift) for B2B referrers or payout
statements for B2C/individual referrers, with correct Austrian VAT treatment.
"""

from decimal import Decimal

import structlog
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from accounts.models import ReferralPayout, ReferralPayoutStatement, UserBillingProfile
from common.models import SiteSettings
from common.service.invoice_utils import get_next_sequential_number, render_pdf
from common.service.vat_utils import B2BFeeVATBreakdown, calculate_b2b_fee_vat

logger = structlog.get_logger(__name__)

DOCUMENT_NUMBER_PREFIX = "RVL-RP-"


def _get_next_statement_number(year: int) -> str:
    """Generate the next sequential statement number.

    Must be called inside ``transaction.atomic()``.
    """
    return get_next_sequential_number(ReferralPayoutStatement, DOCUMENT_NUMBER_PREFIX, year, "document_number")


def _determine_vat_treatment(
    payout: ReferralPayout,
    billing_profile: UserBillingProfile,
    site: SiteSettings,
) -> tuple[ReferralPayoutStatement.DocumentType, B2BFeeVATBreakdown]:
    """Determine document type and VAT breakdown for a payout.

    B2B (validated VAT ID) → self-billing invoice with VAT math.
    B2C (no validated VAT ID) → payout statement, no VAT.
    """
    is_b2b = bool(billing_profile.vat_id and billing_profile.vat_id_validated)

    if is_b2b:
        vat_breakdown = calculate_b2b_fee_vat(
            net_fee=payout.payout_amount,
            entity=billing_profile,
            platform_vat_country=site.platform_vat_country,
            platform_vat_rate=site.platform_vat_rate,
        )
        return ReferralPayoutStatement.DocumentType.SELF_BILLING_INVOICE, vat_breakdown

    # B2C / individual: no VAT applicable
    return ReferralPayoutStatement.DocumentType.PAYOUT_STATEMENT, B2BFeeVATBreakdown(
        fee_gross=payout.payout_amount,
        fee_net=payout.payout_amount,
        fee_vat=Decimal("0.00"),
        fee_vat_rate=Decimal("0.00"),
        reverse_charge=False,
    )


def generate_payout_statement(payout: ReferralPayout) -> ReferralPayoutStatement:
    """Generate a payout statement (or self-billing invoice) for a single payout.

    Idempotent: returns the existing statement if one already exists for this payout.
    Determines B2B vs B2C from the referrer's billing profile, calculates VAT,
    renders a PDF, and persists the statement.

    Args:
        payout: The ``ReferralPayout`` to create a statement for.

    Returns:
        The created (or existing) ``ReferralPayoutStatement``.

    Raises:
        UserBillingProfile.DoesNotExist: If the referrer has no billing profile.
    """
    # Idempotency: return existing statement if already generated
    existing = ReferralPayoutStatement.objects.filter(payout=payout).first()
    if existing:
        return existing

    referrer = payout.referral.referrer
    billing_profile = referrer.billing_profile
    site = SiteSettings.get_solo()
    now = timezone.now()
    year = payout.period_start.year

    doc_type, vat = _determine_vat_treatment(payout, billing_profile, site)
    is_b2b = doc_type == ReferralPayoutStatement.DocumentType.SELF_BILLING_INVOICE

    with transaction.atomic():
        document_number = _get_next_statement_number(year)
        statement = ReferralPayoutStatement.objects.create(
            payout=payout,
            document_type=doc_type,
            document_number=document_number,
            # Fee breakdown
            amount_gross=vat.fee_gross,
            amount_net=vat.fee_net,
            amount_vat=vat.fee_vat,
            vat_rate=vat.fee_vat_rate,
            currency=payout.currency,
            reverse_charge=vat.reverse_charge,
            # Referrer snapshot
            referrer_name=billing_profile.billing_name or referrer.get_display_name(),
            referrer_address=billing_profile.billing_address,
            referrer_vat_id=billing_profile.vat_id,
            referrer_country=billing_profile.vat_country_code,
            # Platform snapshot
            platform_business_name=site.platform_business_name,
            platform_business_address=site.platform_business_address,
            platform_vat_id=site.platform_vat_id,
            # Delivery
            issued_at=now,
        )

    # Generate PDF outside transaction (WeasyPrint is slow)
    document_title = "GUTSCHRIFT" if is_b2b else "PAYOUT STATEMENT"
    pdf_bytes = render_pdf(
        "invoices/referral_payout_statement.html",
        {
            "document_title": document_title,
            "document_number": document_number,
            "issued_date": now.strftime("%Y-%m-%d"),
            "period_start": payout.period_start.isoformat(),
            "period_end": payout.period_end.isoformat(),
            "period_label": payout.period_start.strftime("%B %Y"),
            "currency": payout.currency,
            # Platform
            "platform_business_name": site.platform_business_name,
            "platform_business_address": site.platform_business_address,
            "platform_vat_id": site.platform_vat_id,
            # Referrer
            "referrer_name": statement.referrer_name,
            "referrer_address": statement.referrer_address,
            "referrer_vat_id": statement.referrer_vat_id,
            # Amounts
            "amount_net": vat.fee_net,
            "amount_vat": vat.fee_vat,
            "amount_gross": vat.fee_gross,
            "vat_rate": vat.fee_vat_rate,
            "reverse_charge": vat.reverse_charge,
            "is_b2b": is_b2b,
        },
    )
    statement.pdf_file.save(f"{document_number}.pdf", ContentFile(pdf_bytes), save=True)

    logger.info(
        "payout_statement_generated",
        document_number=document_number,
        document_type=doc_type,
        referrer_id=str(referrer.id),
        amount_gross=str(vat.fee_gross),
        currency=payout.currency,
    )
    return statement
