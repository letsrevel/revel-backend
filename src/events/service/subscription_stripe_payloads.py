"""Payload-shape helpers for Stripe membership-subscription objects.

Split out of :mod:`subscription_stripe_service` (file-length budget). These
readers are pinned-API-version aware: API versions >= 2025-03-31.basil (we pin
dahlia) moved several fields — subscription periods onto items, the invoice's
subscription reference under ``parent.subscription_details``, and the invoice
payment intent into the ``payments`` list. Each reader tries the modern path
first and falls back to the legacy field for old fixtures / unpinned tooling.
"""

import re
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
# Same reasoning for the HTTP timeout (see stripe_service): don't rely on
# another module's import to configure stripe.default_http_client.
stripe.default_http_client = stripe.RequestsClient(  # type: ignore[attr-defined]
    timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS
)


def _stripe_account_kwargs(organization: Organization) -> dict[str, str]:
    """Return ``stripe_account=...`` kwargs for a Connect API call.

    When the organization happens to share the platform's own Stripe account,
    omit the kwarg entirely (mirrors :mod:`events.service.stripe_service`).
    """
    if organization.stripe_account_id and organization.stripe_account_id != settings.STRIPE_ACCOUNT:
        return {"stripe_account": organization.stripe_account_id}
    return {}


# Stripe has no error code for "this subscription is already canceled" — only
# ``resource_missing`` (unknown id) is machine-readable — so the canceled
# variant is matched on its message ("This subscription has been canceled.",
# "A canceled subscription can only update its cancellation_details.", …).
_ALREADY_CANCELED_RE = re.compile(r"cancell?ed subscription|subscription[^.]{0,40}cancell?ed", re.IGNORECASE)


def _is_subscription_gone(exc: stripe.error.InvalidRequestError) -> bool:
    """Return True when ``exc`` means the Stripe Subscription is already gone.

    Stripe raises :class:`InvalidRequestError` for many unrelated reasons — most
    dangerously when the subscription is *schedule-managed* (a downgrade
    schedule is still running), where it refuses ``cancel_at_period_end`` and
    ``pause_collection``. Treating that as "already canceled" would record a
    cancellation locally that Stripe never accepted, and the member would keep
    being billed — hence the explicit schedule guard below. Only a missing
    resource or an explicitly canceled subscription means the caller's desired
    end state already holds; anything else must stay a hard failure.
    """
    if exc.code == "resource_missing":
        return True
    message = str(exc) or ""
    if "schedule" in message.lower():
        return False
    return bool(_ALREADY_CANCELED_RE.search(message))


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


class InvoicePaymentDetails(t.NamedTuple):
    """PaymentIntent id and collected application fee resolved from an Invoice."""

    payment_intent_id: str
    application_fee_minor: int | None
    """Minor-unit application fee; ``None`` when it could not be resolved."""


def _scan_payment_entries(payments_obj: t.Any, invoice_fee: int | None) -> InvoicePaymentDetails | None:
    """Pick the first resolvable PaymentIntent out of an invoice's ``payments`` list.

    ``invoice_fee``, when known (legacy readable field), overrides the
    per-intent fee; otherwise the fee is only known when the intent is an
    expanded PaymentIntent object.
    """
    data = (payments_obj or {}).get("data") or []
    for entry in data:
        intent = (entry.get("payment") or {}).get("payment_intent")
        intent_id = _as_stripe_id(intent)
        if not intent_id:
            continue
        if invoice_fee is not None:
            return InvoicePaymentDetails(intent_id, invoice_fee)
        if isinstance(intent, dict):  # expanded PaymentIntent
            return InvoicePaymentDetails(intent_id, int(intent.get("application_fee_amount") or 0))
        return InvoicePaymentDetails(intent_id, None)
    return None


def _invoice_payment_details(
    invoice: dict[str, t.Any],
    organization: Organization,
    *,
    need_fee: bool = False,
) -> InvoicePaymentDetails:
    """Resolve the PaymentIntent id (and application fee) from an Invoice payload.

    Pre-basil payloads carry ``invoice.payment_intent`` and a readable
    ``invoice.application_fee_amount``. From 2025-03-31.basil an invoice can
    have multiple partial payments and both moved behind the ``payments`` list:
    the intent at ``payments.data[].payment.payment_intent`` and the collected
    fee *only* on that PaymentIntent — and webhook payloads do NOT embed
    ``payments``, so fall back to an outbound ``stripe.Invoice.retrieve`` that
    expands down to the PaymentIntent. Best-effort: the id feeds refund routing
    (charge.refunded → MembershipPayment matching) and audit, the fee feeds the
    VAT ledger and referral payouts, so an empty id / ``None`` fee is tolerated
    rather than failing the webhook.

    Args:
        invoice: The Stripe ``invoice.*`` payload (``data.object`` or a retrieve).
        organization: The owning org, for the Connect ``stripe_account`` header.
        need_fee: When ``True`` (a paid invoice), an embedded-but-unexpanded
            intent reference is not enough — the fee has to be read off the
            expanded PaymentIntent, so the outbound fallback still fires.
    """
    raw_invoice_fee = invoice.get("application_fee_amount")
    invoice_fee = int(raw_invoice_fee) if raw_invoice_fee is not None else None
    if invoice_fee is not None:
        need_fee = False  # the legacy readable field is authoritative
    legacy_intent = _as_stripe_id(invoice.get("payment_intent"))
    if legacy_intent:
        # Pre-basil shape: the invoice-level fee field travels with it
        # (absent/None simply means no fee was collected).
        return InvoicePaymentDetails(legacy_intent, invoice_fee or 0)

    found = _scan_payment_entries(invoice.get("payments"), invoice_fee)
    if found is not None and not (need_fee and found.application_fee_minor is None):
        return found

    unresolved = found or InvoicePaymentDetails("", invoice_fee)
    invoice_id = invoice.get("id")
    if not invoice_id:
        return unresolved
    try:
        retrieved = stripe.Invoice.retrieve(
            invoice_id,
            expand=["payments.data.payment.payment_intent"],
            **_stripe_account_kwargs(organization),
        )
    except stripe.error.StripeError:
        logger.warning("subscription_invoice_payments_fetch_failed", stripe_invoice_id=invoice_id)
        return unresolved
    return _scan_payment_entries(dict(retrieved).get("payments"), invoice_fee) or unresolved
