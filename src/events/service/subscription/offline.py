"""Offline-paid membership: first-payment activation.

An OFFLINE plan's subscription is *never-paid* until its first SUCCEEDED payment
(``MembershipPayment`` with ``status=SUCCEEDED``). That first payment is what
turns a held ``AWAITING_PAYMENT`` member into an ``ACTIVE`` one; nothing else
does (``sync_member_from_subscription`` deliberately leaves such members alone).
"""

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from events.service import member_register_service
from events.service.blacklist_service import check_user_hard_blacklisted
from events.service.member_register_linking import notify_membership_granted
from events.service.subscription.notifications import (
    _dispatch_renewal_succeeded,
    dispatch_membership_payment_requested,
)
from events.service.subscription.sales import ensure_plan_on_sale
from events.utils.membership_access import (
    eligible_memberships,
    is_awaiting_payment_status,
    is_eligible_status,
    is_payment_held_status,
    payment_held_memberships,
)
from events.utils.subscription_payment import is_never_paid_offline, is_never_paid_on_offline_plan

logger = structlog.get_logger(__name__)


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
    member = OrganizationMember.objects.filter(
        organization_id=subscription.organization_id, user_id=subscription.user_id
    ).first()
    # A member always exists for an offline join (held as AWAITING_PAYMENT); if one is somehow
    # missing there is nothing to activate, so only the application is settled.
    if member is not None and is_payment_held_status(member.status):
        was_awaiting = is_awaiting_payment_status(member.status)
        update_fields = []
        if was_awaiting:
            member.status = OrganizationMember.MembershipStatus.ACTIVE
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


@transaction.atomic
def open_offline_subscription(
    plan: MembershipSubscriptionPlan,
    user: RevelUser,
    *,
    application: OrganizationMembershipRequest | None = None,
    member_facing: bool = False,
) -> MembershipSubscription:
    """Open (or return) the PENDING subscription of a user joining an OFFLINE plan, and ask them to pay.

    Idempotent: a never-paid subscription the user already holds on this plan is returned as is
    (no second subscription, no second MEMBERSHIP_PAYMENT_REQUESTED). Otherwise
    :func:`create_subscription` creates it and holds the person as ``AWAITING_PAYMENT``.

    When ``application`` is given it is linked to the subscription and ends APPROVED. ``member_facing``
    (the subscriber is the caller) refuses a paused plan; staff paths bypass it.
    """
    # lazy: subscription.core imports this module.
    from events.service.subscription.core import create_subscription

    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.OFFLINE:
        raise ValueError("open_offline_subscription requires an OFFLINE plan")
    # Serialize concurrent opens per user (same lock link_record takes): the second caller finds the first's row.
    RevelUser.objects.select_for_update(no_key=True).get(pk=user.pk)
    subscription = (
        MembershipSubscription.objects.filter(user=user, organization_id=plan.tier.organization_id, plan=plan)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .first()
    )
    created = subscription is None or not is_never_paid_offline(subscription)
    if created:
        if member_facing:
            ensure_plan_on_sale(plan)
        subscription = create_subscription(plan, user)
    assert subscription is not None  # noqa: S101
    if application is not None:
        update_fields: list[str] = []
        if application.subscription_id is None:
            application.subscription = subscription
            update_fields.append("subscription")
        # Never re-point an application already linked elsewhere; approve it only when it is this subscription's.
        if (
            application.subscription_id == subscription.pk
            and application.status != OrganizationMembershipRequest.Status.APPROVED
        ):
            application.status = OrganizationMembershipRequest.Status.APPROVED
            update_fields.append("status")
        if update_fields:
            application.save(update_fields=[*update_fields, "updated_at"])
    if created:
        transaction.on_commit(lambda: dispatch_membership_payment_requested(subscription))
    return subscription


def _held_member(subscription: MembershipSubscription, *, include_awaiting: bool) -> OrganizationMember | None:
    """The subscriber's member row when it was held *for this subscription* (no other live subscription).

    AWAITING_PAYMENT is held by definition; ACTIVE only counts at the plan's tier (a legacy row: the
    member was created ACTIVE before the awaiting state existed), never an upgrading free member.
    """
    candidates = payment_held_memberships() if include_awaiting else eligible_memberships()
    member = (
        candidates.select_for_update(of=("self",))
        .filter(organization_id=subscription.organization_id, user_id=subscription.user_id)
        .first()
    )
    if member is None:
        return None
    if is_eligible_status(member.status) and member.tier_id != subscription.plan.tier_id:
        return None
    other_live = (
        MembershipSubscription.objects.filter(
            user_id=subscription.user_id, organization_id=subscription.organization_id
        )
        .exclude(pk=subscription.pk)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exists()
    )
    return None if other_live else member


@transaction.atomic
def close_unpaid_offline(subscription: MembershipSubscription) -> None:
    """Tidy up after a never-paid OFFLINE subscription was terminalised (call it *after* the status change).

    Cancels the application that opened it and the member held for it (``AWAITING_PAYMENT``, or a
    legacy ``ACTIVE`` member at the plan's tier), so the person can join again. A paid, ONLINE or
    FREE subscription, or one still live, is a no-op.
    """
    if not subscription.is_terminal or not is_never_paid_on_offline_plan(subscription):
        return
    subscription.originating_application.filter(
        status__in=(OrganizationMembershipRequest.Status.PENDING, OrganizationMembershipRequest.Status.APPROVED)
    ).update(status=OrganizationMembershipRequest.Status.CANCELLED, updated_at=timezone.now())
    member = _held_member(subscription, include_awaiting=True)
    if member is not None:
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
            member.save(update_fields=["status", "updated_at"])


@transaction.atomic
def revert_to_awaiting_payment(subscription: MembershipSubscription) -> None:
    """After a void left an OFFLINE subscription never-paid again, hold its ACTIVE member as AWAITING_PAYMENT."""
    if not is_never_paid_offline(subscription):
        return
    member = _held_member(subscription, include_awaiting=False)
    if member is not None:
        member.status = OrganizationMember.MembershipStatus.AWAITING_PAYMENT
        with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
            member.save(update_fields=["status", "updated_at"])
