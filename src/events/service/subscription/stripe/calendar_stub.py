"""Invoice-line and calendar-stub helpers for syncing Stripe invoices onto membership payments.

A calendar plan's first invoice covers a *stub* (join -> next boundary): prorated, free (zero total), or absent.
This module reads an invoice's lines to find the billing period, recognises a stub first invoice, and resolves
the full per-period price the stub stood in for. It never touches the database; the one Stripe call
(:func:`stub_full_price`) must run before the member row lock. Imported by :mod:`.sync`, never the reverse.
"""

import typing as t
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import stripe
import structlog
from django.utils import timezone

from events.models import MembershipSubscriptionPlan
from events.service.subscription.stripe.payloads import epoch_to_dt, stripe_account_kwargs
from events.utils.currency import from_stripe_amount
from events.utils.subscription_periods import is_calendar_plan

logger = structlog.get_logger(__name__)


def line_price_id(line: dict[str, t.Any]) -> str:
    """Best-effort price id for an invoice line, across dahlia and legacy shapes."""
    pricing = line.get("pricing") or {}
    details = pricing.get("price_details") or {}
    if details.get("price"):
        return t.cast(str, details["price"])
    price = line.get("price") or {}
    return t.cast(str, price.get("id") or "")


def line_is_proration(line: dict[str, t.Any]) -> bool:
    """Whether an invoice line is a proration, across dahlia and legacy shapes.

    The pinned API version dropped the line's top-level ``proration`` flag: it
    now lives under ``parent.subscription_item_details`` (subscription lines) or
    ``parent.invoice_item_details`` (invoice-item lines). Reading only the legacy
    field makes *every* proration on a dahlia payload look like a recurring line.
    """
    parent = line.get("parent") or {}
    for details_key in ("subscription_item_details", "invoice_item_details"):
        details = parent.get(details_key) or {}
        if details.get("proration"):
            return True
    return bool(line.get("proration"))


def recurring_line_period(lines_data: list[dict[str, t.Any]], plan_price_id: str) -> dict[str, t.Any] | None:
    """Pick the billing period from the *recurring* line of an invoice.

    Stripe sorts invoice lines with "pending invoice items (including
    prorations)" first, so ``lines.data[0]`` is the proration line on any invoice
    carrying one — and a proration's period starts when it was calculated, not at
    the billing anchor. Writing that onto ``current_period_start`` corrupts the
    anchor ``_is_full_refund_of_current_period`` compares against, which silently
    suppresses the refund auto-cancel.

    Prefer the line whose price matches the plan, then the last non-proration
    line. Returns ``None`` when the invoice carries *only* prorations (a
    mid-cycle upgrade under ``always_invoice``): that invoice describes no
    billing period, so the caller must leave the subscription's anchor alone and
    let ``customer.subscription.updated`` supply the authoritative one.

    Args:
        lines_data: ``invoice["lines"]["data"]``.
        plan_price_id: The subscription plan's current Stripe price id.

    Returns:
        The chosen line's ``period`` mapping, or ``None`` when no recurring line
        is present.
    """
    if not lines_data:
        return None
    if plan_price_id:
        for line in lines_data:
            if line_price_id(line) == plan_price_id and not line_is_proration(line):
                return t.cast(dict[str, t.Any], line.get("period") or {})
    non_proration = [line for line in lines_data if not line_is_proration(line)]
    if non_proration:
        return t.cast(dict[str, t.Any], non_proration[-1].get("period") or {})
    return None


def first_proration_line(lines_data: list[dict[str, t.Any]]) -> dict[str, t.Any] | None:
    """The first proration line of an invoice, if any."""
    return next((line for line in lines_data if line_is_proration(line)), None)


def stub_line(lines_data: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    """The line standing for a calendar stub: the first proration line, else the first line (or ``{}``)."""
    return first_proration_line(lines_data) or (lines_data[0] if lines_data else {})


def is_calendar_first_stub(
    invoice: dict[str, t.Any], plan: MembershipSubscriptionPlan, lines_data: list[dict[str, t.Any]]
) -> bool:
    """Whether ``invoice`` is a calendar plan's first invoice for a stub period.

    That is a ``subscription_create`` invoice carrying a proration line (the prorated stub) or whose
    ``total`` and ``subtotal`` are both zero (the free stub: ``proration_behavior="none"``). A paid-nothing
    invoice is not enough on its own: a customer credit balance or coupon also leaves ``amount_paid`` 0 on a
    full period, which must not be stamped as a stub. A first invoice that is neither is a full period joined
    exactly on a boundary.
    """
    if invoice.get("billing_reason") != "subscription_create" or not is_calendar_plan(plan):
        return False
    return first_proration_line(lines_data) is not None or (invoice.get("total") == 0 and invoice.get("subtotal") == 0)


class StubPrice(t.NamedTuple):
    """The full per-period price a stub stood in for, and the line Price it was resolved from.

    ``plan_id`` is set when it came from ``plan.price`` (the line is on the plan's own Price), so a
    plan swap between the unlocked read and the row lock invalidates it.
    """

    price_id: str
    amount: Decimal
    plan_id: UUID | None = None


def stub_full_price(
    lines_data: list[dict[str, t.Any]],
    plan: MembershipSubscriptionPlan,
    organization: t.Any,
    currency_code: str,
    *,
    subscription_id: str,
    invoice_id: str = "",
) -> StubPrice:
    """The full per-period price a calendar stub stood in for, resolved from the stub line's Stripe Price id.

    The line's ``unit_amount_decimal`` is not trusted (it can be 0 or prorated on stub/zero lines), and
    ``plan.price`` is mutable. A line on the plan's own Price is ``plan.price``; any other Price (a
    grandfathered or since-replaced one) is retrieved on the organization's account. Makes a Stripe call:
    run it before the member row lock.

    Best effort: it never raises, because a member who paid must never be stranded over a ledger anchor.
    A line with no Price id, a Stripe error, or a Price without an integer ``unit_amount`` (tiered or
    metered) falls back to ``plan.price`` (almost always right, and the stub amount, possibly 0, never is)
    and logs at WARNING.
    """
    price_id = line_price_id(stub_line(lines_data))
    ids = {"subscription_id": subscription_id, "stripe_invoice_id": invoice_id, "stripe_price_id": price_id}
    fallback = StubPrice(price_id, plan.price, plan.pk)
    if not price_id:
        logger.warning("subscription_stub_line_without_price_using_plan_price", **ids)
        return fallback
    if price_id == plan.stripe_price_id:
        return fallback
    try:
        price = stripe.Price.retrieve(price_id, **stripe_account_kwargs(organization))
    except stripe.error.StripeError:
        logger.warning("subscription_stub_price_unreadable_using_plan_price", exc_info=True, **ids)
        return fallback
    unit_amount = price.get("unit_amount")
    if not isinstance(unit_amount, int):
        logger.warning("subscription_stub_price_unusable_using_plan_price", **ids)
        return fallback
    return StubPrice(price_id, from_stripe_amount(unit_amount, currency_code))


def usable_stub_price(
    stub_price: StubPrice | None,
    lines_data: list[dict[str, t.Any]],
    plan: MembershipSubscriptionPlan,
    *,
    still_stub: bool,
    subscription_id: str,
    invoice_id: str,
) -> Decimal | None:
    """The full per-period price to stamp on a calendar stub, decided under the row lock.

    ``still_stub`` is the locked re-check of the stub decision: not a stub -> nothing to stamp. A stub
    whose pre-lock price is missing or no longer matches the line or the (locked) plan falls back to
    ``plan.price`` with a WARNING, like an unreadable Price: the plan price is almost always right and
    the paid stub amount (possibly 0) never is.
    """
    if not still_stub:
        return None
    if (
        stub_price is None
        or line_price_id(stub_line(lines_data)) != stub_price.price_id
        or (stub_price.plan_id is not None and stub_price.plan_id != plan.pk)
    ):
        logger.warning(
            "subscription_stub_price_dropped_under_lock_using_plan_price",
            subscription_id=subscription_id,
            stripe_invoice_id=invoice_id,
            stripe_price_id=stub_price.price_id if stub_price else None,
        )
        return plan.price
    return stub_price.amount


def invoice_period(
    lines_data: list[dict[str, t.Any]],
    plan: MembershipSubscriptionPlan,
    stub_first_invoice: bool,
) -> tuple[datetime, datetime, dict[str, t.Any] | None]:
    """``(period_start, period_end, recurring_period)`` for an invoice's ledger row.

    ``recurring_period`` is the billing period the *subscription* may adopt, or ``None`` for an invoice
    describing none (a proration-only upgrade). A calendar plan's first invoice may carry only the
    prorated stub line, whose period is the subscription item's own (join -> boundary); without
    taking it, the row would stay NULL whenever ``customer.subscription.*`` was dropped before the
    checkout linked it.
    """
    recurring_period = recurring_line_period(lines_data, plan.stripe_price_id)
    if recurring_period is None and stub_first_invoice:
        stub_period = stub_line(lines_data).get("period") or {}
        if stub_period.get("start") and stub_period.get("end"):
            recurring_period = stub_period
    # The payment row records what this invoice actually covered — for a
    # proration-only invoice that is the proration window, which is correct.
    ledger_period = (
        recurring_period
        if recurring_period is not None
        else ((lines_data[0].get("period") or {}) if lines_data else {})
    )
    return (
        epoch_to_dt(ledger_period.get("start")) or timezone.now(),
        epoch_to_dt(ledger_period.get("end")) or timezone.now(),
        recurring_period,
    )
