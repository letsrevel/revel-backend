"""Offline-paid membership: first-payment activation.

An OFFLINE plan's subscription is *never-paid* until its first SUCCEEDED payment
(``MembershipPayment`` with ``status=SUCCEEDED``). That first payment is what
turns a held ``AWAITING_PAYMENT`` member into an ``ACTIVE`` one; nothing else
does (``sync_member_from_subscription`` deliberately leaves such members alone).
"""

import structlog
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import (
    MembershipPayment,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
)
from events.service import member_register_service
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.member_register_linking import notify_membership_granted
from events.service.subscription.notifications import _dispatch_renewal_succeeded

logger = structlog.get_logger(__name__)


def is_never_paid(subscription: MembershipSubscription) -> bool:
    """True when the subscription has no SUCCEEDED payment yet."""
    return not MembershipPayment.objects.filter(
        subscription=subscription, status=MembershipPayment.PaymentStatus.SUCCEEDED
    ).exists()


def is_never_paid_offline(subscription: MembershipSubscription) -> bool:
    """True for a PENDING, never-paid subscription on an OFFLINE plan (the one predicate for "first payment")."""
    return (
        subscription.status == MembershipSubscription.SubscriptionStatus.PENDING
        and subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE
        and is_never_paid(subscription)
    )


def refuse_blacklisted_first_payment(subscription: MembershipSubscription) -> None:
    """Refuse (400) a first offline payment for a hard-blacklisted user, before any row is written."""
    if is_never_paid_offline(subscription) and check_user_hard_blacklisted(
        subscription.user, subscription.organization
    ):
        raise HttpError(400, str(_("This user is blacklisted from the organization.")))


@transaction.atomic
def activate_on_first_payment(subscription: MembershipSubscription, *, send_receipt: bool = True) -> None:
    """Apply the effects of the first SUCCEEDED payment on an OFFLINE subscription.

    Call it only when :func:`is_never_paid` was true *before* the payment landed, and before the
    subscription itself is saved (that save fires ``sync_member_from_subscription``).

    - ``AWAITING_PAYMENT`` member becomes ``ACTIVE`` at the plan's tier and gets MEMBERSHIP_GRANTED.
    - ``ACTIVE`` member (upgrading, or a legacy row): tier follows the plan; a receipt is sent
      (``send_receipt``) since the welcome already happened.
    - ``PAUSED`` / ``BANNED`` members are left alone, silently.

    Finally the originating application, if any, settles.
    """
    # lazy: subscription.stripe.sync -> ... -> subscription.lifecycle imports core, which imports this module.
    from events.service.subscription.stripe.sync import settle_originating_application

    plan = subscription.plan
    Status = OrganizationMember.MembershipStatus
    member = OrganizationMember.objects.filter(
        organization_id=subscription.organization_id, user_id=subscription.user_id
    ).first()
    # A member always exists for an offline join (held as AWAITING_PAYMENT); if one is somehow
    # missing there is nothing to activate, so only the application is settled.
    if member is not None and member.status in (Status.AWAITING_PAYMENT, Status.ACTIVE):
        was_awaiting = member.status == Status.AWAITING_PAYMENT
        update_fields = []
        if was_awaiting:
            member.status = Status.ACTIVE
            update_fields.append("status")
        if member.tier_id != plan.tier_id:
            member.tier_id = plan.tier_id
            update_fields.append("tier")
        if update_fields:
            with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
                member.save(update_fields=[*update_fields, "updated_at"])
        if was_awaiting:
            # The post_save receiver only fires on create, so this is the single MEMBERSHIP_GRANTED.
            transaction.on_commit(lambda: notify_membership_granted(member))
        elif send_receipt:
            payment = (
                MembershipPayment.objects.filter(
                    subscription=subscription, status=MembershipPayment.PaymentStatus.SUCCEEDED
                )
                .order_by("-created_at")
                .first()
            )
            _dispatch_renewal_succeeded(
                subscription,
                amount=payment.amount if payment else None,
                currency=payment.currency if payment else "",
            )
    settle_originating_application(subscription)
