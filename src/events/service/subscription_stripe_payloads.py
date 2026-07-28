"""Payload-shape helpers for Stripe membership-subscription objects.

Split out of :mod:`subscription_stripe_service` (file-length budget). These
readers are pinned-API-version aware: API versions >= 2025-03-31.basil (we pin
dahlia) moved several fields — subscription periods onto items, the invoice's
subscription reference under ``parent.subscription_details``, and the invoice
payment intent into the ``payments`` list. Each reader tries the modern path
first and falls back to the legacy field for old fixtures / unpinned tooling.
"""

import typing as t
from datetime import datetime
from datetime import timezone as _utc

import stripe
import structlog
from django.conf import settings

from events.models import Organization

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service):
# this module makes its own outbound call (Invoice.retrieve) and must not rely
# on another module's import side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


def _stripe_account_kwargs(organization: Organization) -> dict[str, str]:
    """Return ``stripe_account=...`` kwargs for a Connect API call.

    When the organization happens to share the platform's own Stripe account,
    omit the kwarg entirely (mirrors :mod:`events.service.stripe_service`).
    """
    if organization.stripe_account_id and organization.stripe_account_id != settings.STRIPE_ACCOUNT:
        return {"stripe_account": organization.stripe_account_id}
    return {}


def _epoch_to_dt(epoch: int | None) -> datetime | None:
    """Convert a Stripe Unix timestamp to a tz-aware datetime."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=_utc.utc)


def _subscription_period_epochs(stripe_subscription: dict[str, t.Any]) -> tuple[int | None, int | None]:
    """Extract ``current_period_{start,end}`` from a Subscription payload.

    API versions >= 2025-03-31.basil (we pin dahlia) moved the period from the
    Subscription's top level onto each subscription item; single-item
    subscriptions (our only shape) carry it on ``items.data[0]``. Older
    payloads (tests, fixtures, any unpinned tooling) still have the top-level
    fields, so fall back to those.
    """
    items_data = (stripe_subscription.get("items") or {}).get("data") or []
    item = items_data[0] if items_data else {}
    start = item.get("current_period_start") or stripe_subscription.get("current_period_start")
    end = item.get("current_period_end") or stripe_subscription.get("current_period_end")
    return start, end


def _as_stripe_id(value: t.Any) -> str:
    """Normalize a possibly-expanded Stripe reference to its string id."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return t.cast(str, value.get("id") or "")
    return t.cast(str, getattr(value, "id", "") or "")


def _invoice_subscription_id(invoice: dict[str, t.Any]) -> str:
    """Extract the Subscription id from an Invoice payload.

    API versions >= 2025-03-31.basil (we pin dahlia) moved it from the
    top-level ``subscription`` field to
    ``parent.subscription_details.subscription``. Try the modern path first,
    then the legacy field (old fixtures / unpinned tooling).
    """
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    modern = _as_stripe_id(details.get("subscription"))
    if modern:
        return modern
    return _as_stripe_id(invoice.get("subscription"))


def _invoice_payment_intent_id(invoice: dict[str, t.Any], organization: Organization) -> str:
    """Extract the PaymentIntent id from an Invoice payload, fetching if needed.

    Pre-basil payloads carry ``invoice.payment_intent``. From 2025-03-31.basil
    an invoice can have multiple partial payments and the field moved to the
    ``payments`` list (``payments.data[].payment.payment_intent``), which
    webhook payloads do NOT embed — so fall back to an outbound
    ``stripe.Invoice.retrieve(..., expand=["payments"])``. Best-effort: the id
    feeds refund routing (charge.refunded → MembershipPayment matching) and
    audit, so an empty string is tolerated rather than failing the webhook.
    """
    legacy = _as_stripe_id(invoice.get("payment_intent"))
    if legacy:
        return legacy

    def _scan(payments_obj: t.Any) -> str:
        data = (payments_obj or {}).get("data") or []
        for entry in data:
            intent = _as_stripe_id((entry.get("payment") or {}).get("payment_intent"))
            if intent:
                return intent
        return ""

    found = _scan(invoice.get("payments"))
    if found:
        return found

    invoice_id = invoice.get("id")
    if not invoice_id:
        return ""
    try:
        retrieved = stripe.Invoice.retrieve(
            invoice_id,
            expand=["payments"],
            **_stripe_account_kwargs(organization),
        )
    except stripe.error.StripeError:
        logger.warning("subscription_invoice_payments_fetch_failed", stripe_invoice_id=invoice_id)
        return ""
    return _scan(dict(retrieved).get("payments"))
