"""Payment means snapshot for attendee invoices (EN 16931 BT-81/BT-82, BG-18; #1234).

Read once at invoice generation from the PaymentIntent's latest charge on the organizer's
connected Stripe account, then stored on the invoice.
"""

import typing as t

import stripe
import structlog
from django.conf import settings

from common.service.stripe_config import configure_stripe
from events.models.organization import Organization

logger = structlog.get_logger(__name__)

CARD_CODE = "48"
OTHER_CODE = "ZZZ"
_ONLINE_LABEL = "Paid online via Stripe"
_METHOD_NAMES = {
    "sepa_debit": "SEPA Direct Debit",
    "klarna": "Klarna",
    "bancontact": "Bancontact",
    "ideal": "iDEAL",
    "eps": "EPS",
    "giropay": "giropay",
    "sofort": "Sofort",
    "paypal": "PayPal",
    "link": "Link",
}


class PaymentMeans(t.NamedTuple):
    """Payment means code (BT-81), label (BT-82) and card last four digits (BG-18)."""

    code: str
    label: str
    card_last4: str


_FALLBACK = PaymentMeans(OTHER_CODE, _ONLINE_LABEL, "")


def payment_means_from_charge(charge: t.Mapping[str, t.Any] | None) -> PaymentMeans:
    """Map a Stripe charge to payment means; anything unreadable yields the generic fallback."""
    details = (charge or {}).get("payment_method_details") or {}
    method_type = details.get("type")
    if not method_type:
        return _FALLBACK
    if method_type == "card":
        return PaymentMeans(CARD_CODE, "Paid by card", str((details.get("card") or {}).get("last4") or ""))
    name = _METHOD_NAMES.get(method_type, str(method_type).replace("_", " "))
    return PaymentMeans(OTHER_CODE, f"{_ONLINE_LABEL} ({name})", "")


def fetch_payment_means(org: Organization, payment_intent_id: str) -> PaymentMeans:
    """Look up how a PaymentIntent was paid, falling back to the generic means on any Stripe failure."""
    if not payment_intent_id:
        return _FALLBACK
    configure_stripe()
    kwargs: dict[str, t.Any] = {"expand": ["latest_charge"]}
    if org.stripe_account_id and org.stripe_account_id != settings.STRIPE_ACCOUNT:
        kwargs["stripe_account"] = org.stripe_account_id
    try:
        pi = stripe.PaymentIntent.retrieve(payment_intent_id, **kwargs)
    except stripe.StripeError:
        # ponytail: informational field -- an invoice is never blocked on it.
        logger.warning("payment_means_lookup_failed", org_id=str(org.id))
        return _FALLBACK
    charge = pi.get("latest_charge")
    return payment_means_from_charge(charge if isinstance(charge, t.Mapping) else None)
