"""Eligibility rules for editing and voiding manual membership payments.

Single source of truth shared by the service (which enforces the rules) and the
payment schemas (which expose ``can_edit`` / ``can_void``). List querysets
annotate :func:`latest_payment_id_subquery` so the flags cost no extra queries.
"""

import typing as t
import uuid

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils.translation import gettext_lazy as _

from events.exceptions import MembershipPaymentEditRefusalCode, MembershipPaymentEditRefusedError

if t.TYPE_CHECKING:
    from events.models import MembershipPayment

#: Attribute the list querysets annotate with the subscription's latest non-voided payment id.
LATEST_ID_ATTR = "latest_payment_id"

#: Deterministic "latest payment" ordering: newest period first, ties by creation then pk.
LATEST_PAYMENT_ORDERING: tuple[str, ...] = ("-period_start", "-created_at", "-pk")


def non_voided_payments(subscription_id: t.Any) -> "QuerySet[MembershipPayment]":
    """Return the subscription's non-voided payments, latest first."""
    from events.models import MembershipPayment

    return (
        MembershipPayment.objects.filter(subscription_id=subscription_id)
        .exclude(status=MembershipPayment.PaymentStatus.VOIDED)
        .order_by(*LATEST_PAYMENT_ORDERING)
    )


def latest_payment_id_subquery() -> Subquery:
    """Subquery yielding the latest non-voided payment id of the outer row's subscription."""
    return Subquery(non_voided_payments(OuterRef("subscription_id")).values("pk")[:1])


def with_latest_payment_id(qs: "QuerySet[MembershipPayment]") -> "QuerySet[MembershipPayment]":
    """Annotate ``qs`` so :func:`payment_edit_flags` needs no further query."""
    return qs.annotate(**{LATEST_ID_ATTR: latest_payment_id_subquery()})


def _latest_payment_id(payment: "MembershipPayment") -> uuid.UUID | None:
    """Return the annotated latest id, falling back to one query for un-annotated rows."""
    if hasattr(payment, LATEST_ID_ATTR):
        return t.cast("uuid.UUID | None", getattr(payment, LATEST_ID_ATTR))
    return non_voided_payments(payment.subscription_id).values_list("pk", flat=True).first()


def edit_refusal(payment: "MembershipPayment") -> MembershipPaymentEditRefusedError | None:
    """Return why ``payment``'s amount/notes cannot be edited, or ``None`` when they can."""
    from events.models import MembershipPayment, MembershipSubscriptionPlan

    code = MembershipPaymentEditRefusalCode
    if payment.status == MembershipPayment.PaymentStatus.VOIDED:
        return MembershipPaymentEditRefusedError(code.PAYMENT_VOIDED, str(_("This payment has been voided.")))
    if (
        payment.stripe_invoice_id
        or payment.stripe_payment_intent_id
        or payment.subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.OFFLINE
    ):
        return MembershipPaymentEditRefusedError(
            code.PAYMENT_ONLINE, str(_("Online payments are managed through Stripe and cannot be edited here."))
        )
    if payment.status == MembershipPayment.PaymentStatus.REFUNDED or payment.refund_amount is not None:
        return MembershipPaymentEditRefusedError(code.PAYMENT_REFUNDED, str(_("Refunded payments cannot be edited.")))
    return None


def period_edit_refusal(payment: "MembershipPayment") -> MembershipPaymentEditRefusedError | None:
    """Return why ``payment``'s status/date cannot change (or be voided), or ``None``.

    Stricter than :func:`edit_refusal`: the row must also be the subscription's
    latest non-voided payment on a subscription that is not terminal.
    """
    refusal = edit_refusal(payment)
    if refusal is not None:
        return refusal
    code = MembershipPaymentEditRefusalCode
    if payment.subscription.is_terminal:
        return MembershipPaymentEditRefusedError(
            code.SUBSCRIPTION_TERMINAL,
            str(_("The status and date of a payment on a cancelled or expired subscription cannot be changed.")),
        )
    if _latest_payment_id(payment) != payment.pk:
        return MembershipPaymentEditRefusedError(
            code.PAYMENT_NOT_LATEST,
            str(_("Only the subscription's latest payment can have its status or date changed, or be voided.")),
        )
    return None


def payment_edit_flags(payment: "MembershipPayment") -> tuple[bool, bool]:
    """Return ``(can_edit, can_void)`` for ``payment``."""
    return edit_refusal(payment) is None, period_edit_refusal(payment) is None
