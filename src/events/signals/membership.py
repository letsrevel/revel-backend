"""Receivers for members, staff, blacklist entries and membership subscriptions."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from common.models import SiteSettings
from events.models import (
    Blacklist,
    MembershipPayment,
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationToken,
)
from events.service import (
    day_membership_service,
    member_register_service,
    permission_snapshot,
)
from events.service.blacklist_service import (
    apply_blacklist_consequences,
    check_user_hard_blacklisted,
)
from events.service.member_register_linking import notify_membership_granted
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from events.utils.frontend_urls import frontend_base_url_for
from events.utils.membership_access import is_awaiting_payment_status, is_banned_status, is_eligible_status
from events.utils.subscription_payment import is_never_paid_offline, is_never_paid_on_offline_plan
from notifications.enums import NotificationType
from notifications.signals import notification_requested

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.signals")


@receiver(post_save, sender=OrganizationMember)
def handle_membership_granted(
    sender: type[OrganizationMember], instance: OrganizationMember, created: bool, **kwargs: t.Any
) -> None:
    """Send notification when user is granted membership to an organization."""
    from events.suppression import is_membership_granted_suppressed

    if not created or instance.status == OrganizationMember.MembershipStatus.AWAITING_PAYMENT:
        # An awaiting member is granted nothing yet: activation on first payment sends the notification.
        return
    if is_membership_granted_suppressed():  # the verified-email auto-link sends MEMBER_RECORD_LINKED instead
        return

    transaction.on_commit(lambda: notify_membership_granted(instance))


@receiver(post_save, sender=OrganizationMember)
def recheck_blacklist_after_membership_commit(
    sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any
) -> None:
    """Apply the ban consequences when an account was blacklisted while its membership row was being written.

    Whoever grants a membership checks the blacklist first, but a concurrent entry may not have been
    visible to that check. Re-reading after this row's commit closes the window whatever the grant path
    (token claim, subscription, approval, reactivation). A banned row is skipped, so this never recurses, and the
    consequences are idempotent (terminal subscriptions are not cancelled twice).
    """
    if instance.status == OrganizationMember.MembershipStatus.BANNED:
        return
    organization_id, user = instance.organization_id, instance.user

    def _recheck() -> None:
        org = Organization.objects.get(pk=organization_id)
        if org.owner_id != user.pk and check_user_hard_blacklisted(user, org):
            apply_blacklist_consequences(user, org)

    transaction.on_commit(_recheck)


@receiver(post_save, sender=OrganizationMember)
def end_personal_invitation_on_membership(
    sender: type[OrganizationMember], instance: OrganizationMember, created: bool, **kwargs: t.Any
) -> None:
    """End the invitee's personal invitation once they hold a membership row, however it was created.

    Otherwise a later removal would make the old invitation link claimable again.
    """
    if not created:
        return
    now = timezone.now()
    OrganizationToken.objects.filter(
        organization_id=instance.organization_id, invitee_id=instance.user_id, expires_at__gt=now
    ).update(expires_at=now)


@receiver(post_delete, sender=OrganizationMember)
def handle_membership_removed(sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any) -> None:
    """Send notification when user is removed from an organization."""
    if instance.status == OrganizationMember.MembershipStatus.AWAITING_PAYMENT:
        # Never granted anything; cancelling their subscription already told them.
        return

    def send_removal_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.MEMBERSHIP_REMOVED,
            context={
                "organization_id": str(instance.organization_id),
                "organization_name": instance.organization.name,
                "role": "member",
                "action": "removed",
                "frontend_url": f"{frontend_base_url}/organizations",
            },
        )

        logger.info(
            "membership_removed_notification_sent",
            organization_id=str(instance.organization_id),
            user_id=str(instance.user_id),
        )

    transaction.on_commit(send_removal_notification)


@receiver(post_save, sender=OrganizationMember)
@receiver(post_delete, sender=OrganizationMember)
@receiver(post_save, sender=OrganizationStaff)
@receiver(post_delete, sender=OrganizationStaff)
def invalidate_permission_snapshot(
    sender: type[OrganizationMember | OrganizationStaff],
    instance: OrganizationMember | OrganizationStaff,
    **kwargs: t.Any,
) -> None:
    """Bust the user's cached my-permissions payload on any membership/staff write (#886).

    Membership writes are scattered across services, Stripe sync, admin and signals, so
    a receiver beats enumerating call sites. Note: ``bulk_create``/``bulk_update``
    bypass signals — no such call sites exist for these models today; if one is added,
    it must invalidate explicitly.
    """
    permission_snapshot.invalidate_my_permissions(instance.user_id)


@receiver(post_save, sender=OrganizationMember)
@receiver(post_delete, sender=OrganizationMember)
def rebuild_visibility_on_membership_change(
    sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any
) -> None:
    """Rebuild the persisted attendee visibility flags of the events this member takes part in.

    Member-scoped attendee privacy only counts active memberships, so any membership write (join,
    removal, status change) can change it. Scoped to the member's own events, so it stays cheap.
    """
    trigger_visibility_flags_for_user(instance.user_id)


@receiver(pre_save, sender=OrganizationMember)
def capture_member_old_tier(sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any) -> None:
    """Stash the pre-save tier id so post_save can detect tier changes."""
    if instance.pk:
        old_tier_id, old_status = OrganizationMember.objects.filter(pk=instance.pk).values_list(
            "tier_id", "status"
        ).first() or (None, None)
        instance._old_tier_id = old_tier_id  # type: ignore[attr-defined]
        instance._old_member_status = old_status  # type: ignore[attr-defined]


@receiver(post_save, sender=OrganizationMember)
def mirror_member_to_register(sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any) -> None:
    """Mirror access-row transitions into the membership register (#1318)."""
    member_register_service.mirror_member(instance)


@receiver(post_save, sender=OrganizationMember)
def revoke_day_memberships_on_ban(
    sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any
) -> None:
    """A banned person loses their upcoming day memberships (#1320)."""
    if kwargs.get("raw"):
        return
    if instance.status == OrganizationMember.MembershipStatus.BANNED:
        day_membership_service.revoke_upcoming_for_ban(instance.organization_id, instance.user_id)


@receiver(post_save, sender=OrganizationMember)
def handle_membership_tier_changed(
    sender: type[OrganizationMember], instance: OrganizationMember, created: bool, **kwargs: t.Any
) -> None:
    """Send a card re-download notification when a member's tier changes.

    The wallet card face shows the tier, and passes are write-once (Google saves
    with a known object id are no-ops — the tier is part of the object id), so a
    tier change requires the member to re-add the card.
    """
    if created:
        return
    old_tier_id = getattr(instance, "_old_tier_id", instance.tier_id)
    if old_tier_id == instance.tier_id:
        return
    if is_awaiting_payment_status(instance.status) or is_awaiting_payment_status(
        getattr(instance, "_old_member_status", None)
    ):
        return  # no card yet; on activation MEMBERSHIP_GRANTED is the notification
    from events.suppression import is_membership_granted_suppressed

    if is_membership_granted_suppressed():  # an auto-link reactivation: MEMBER_RECORD_LINKED is the one message
        return

    def send_card_updated_notification() -> None:
        frontend_base_url = frontend_base_url_for(instance.organization)
        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.MEMBERSHIP_CARD_UPDATED,
            context={
                "organization_id": str(instance.organization_id),
                "organization_name": instance.organization.name,
                "member_id": str(instance.id),
                "tier_name": instance.tier.name if instance.tier else "",
                "frontend_url": f"{frontend_base_url}/org/{instance.organization.slug}",
            },
        )

    transaction.on_commit(send_card_updated_notification)


@receiver(post_save, sender=OrganizationStaff)
def handle_membership_promoted(
    sender: type[OrganizationStaff], instance: OrganizationStaff, created: bool, **kwargs: t.Any
) -> None:
    """Send notification when user is promoted to staff."""
    if not created:
        return

    def send_promotion_notification() -> None:
        frontend_base_url = frontend_base_url_for(instance.organization)

        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.MEMBERSHIP_PROMOTED,
            context={
                "organization_id": str(instance.organization_id),
                "organization_name": instance.organization.name,
                "role": "staff",
                "action": "promoted",
                "frontend_url": f"{frontend_base_url}/org/{instance.organization.slug}",
            },
        )

        logger.info(
            "membership_promoted_notification_sent",
            organization_id=str(instance.organization_id),
            user_id=str(instance.user_id),
        )

    transaction.on_commit(send_promotion_notification)


@receiver(post_save, sender=Blacklist)
def handle_blacklist_user_linked(sender: type[Blacklist], instance: Blacklist, created: bool, **kwargs: t.Any) -> None:
    """Handle consequences when a user is linked to a blacklist entry.

    When a blacklist entry has a user FK set on creation, we apply
    blacklist consequences:
    1. Remove them from OrganizationStaff (if they are staff)
    2. Set their OrganizationMember status to BANNED (or create one with BANNED)

    Note: Auto-linking via `link_blacklist_entries_for_user` handles its own
    consequences since .update() doesn't trigger signals.
    """
    # Only act on creation with a user FK set
    # (Updates via link_blacklist_entries_for_user handle their own consequences)
    if not created or instance.user is None:
        return

    apply_blacklist_consequences(instance.user, instance.organization)


# Map subscription status -> the OrganizationMember status it implies.
_SUBSCRIPTION_TO_MEMBER_STATUS: dict[str, str] = {
    MembershipSubscription.SubscriptionStatus.PENDING.value: OrganizationMember.MembershipStatus.ACTIVE.value,
    MembershipSubscription.SubscriptionStatus.ACTIVE.value: OrganizationMember.MembershipStatus.ACTIVE.value,
    MembershipSubscription.SubscriptionStatus.PAST_DUE.value: OrganizationMember.MembershipStatus.ACTIVE.value,
    MembershipSubscription.SubscriptionStatus.PAUSED.value: OrganizationMember.MembershipStatus.PAUSED.value,
    MembershipSubscription.SubscriptionStatus.EXPIRED.value: OrganizationMember.MembershipStatus.CANCELLED.value,
    MembershipSubscription.SubscriptionStatus.CANCELLED.value: OrganizationMember.MembershipStatus.CANCELLED.value,
}


def _has_paid_current_period(subscription: MembershipSubscription) -> bool:
    """Whether money was ever collected for the period a PAST_DUE row is honoring.

    PAST_DUE maps to an ACTIVE member so Stripe's dunning grace keeps a *paying*
    member's access while a renewal retries. A **first** invoice that fails —
    or stalls on SCA, which ``invoice.payment_action_required`` deliberately
    routes through the failure branch, and which async methods (SEPA) can do
    days after the checkout completed — moves the row PENDING → PAST_DUE
    (``subscription.stripe.sync._apply_invoice_outcome``). Without this check
    that grace would grant, or upgrade a free member to, the paid tier with
    zero money collected. A revival checkout is the same story on a reused row.

    ``current_period_end is None`` alone is not a sound discriminator: a
    ``customer.subscription.*`` sync mirrors Stripe's period onto a still
    ``incomplete`` (locally PENDING) row, so an unpaid row can carry one. What
    separates dunning from a never-paid row is a SUCCEEDED payment whose period
    runs up to the start of the period being honored — renewal periods are
    contiguous, while a revival's (or a first invoice's) period starts after a
    gap, with no payment reaching it.
    """
    if subscription.current_period_start is None:
        return False
    return MembershipPayment.objects.filter(
        subscription_id=subscription.pk,
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_end__gte=subscription.current_period_start,
    ).exists()


@receiver(pre_save, sender=MembershipSubscription)
def capture_subscription_old_status(
    sender: type[MembershipSubscription],
    instance: MembershipSubscription,
    **kwargs: t.Any,
) -> None:
    """Stamp the committed status on the instance for :func:`sync_member_from_subscription`.

    Same pre_save capture pattern as :func:`capture_event_old_status`, but the
    attribute is stamped on *every* save because the post_save consumer needs to
    distinguish "was PAUSED" from "was already ACTIVE", not merely "changed".
    The UUID pk is populated before the INSERT, so a create is identified by the
    lookup finding no committed row — leaving ``_old_status`` ``None``.
    """
    instance._old_status = (  # type: ignore[attr-defined]
        MembershipSubscription.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _subscription_cannot_move(member: OrganizationMember, target_status: str) -> bool:
    """BANNED members never move; AWAITING_PAYMENT ones leave only via first-payment activation."""
    return is_banned_status(member.status) or (
        is_awaiting_payment_status(member.status) and is_eligible_status(target_status)
    )


def _never_paid_offline_handled(instance: MembershipSubscription) -> bool:
    """True when a never-paid OFFLINE subscription must not drive its member through the generic sync.

    PENDING ones never move the member. A terminal one would take a free member's existing membership with it,
    so :func:`close_unpaid_offline` releases exactly the member held for it instead, but only on the transition
    into a terminal status (``_old_status``, stamped by :func:`capture_subscription_old_status`): a later re-save
    of the old row must not cancel a member staff set up by hand since.
    """
    if instance.is_terminal and is_never_paid_on_offline_plan(instance):
        if getattr(instance, "_old_status", None) not in MembershipSubscription.TERMINAL_STATUSES:
            from events.service.subscription.offline import close_unpaid_offline  # lazy: services import models

            close_unpaid_offline(instance)
        return True
    return is_never_paid_offline(instance)


@receiver(post_save, sender=MembershipSubscription)
def sync_member_from_subscription(
    sender: type[MembershipSubscription],
    instance: MembershipSubscription,
    created: bool,
    **kwargs: t.Any,
) -> None:
    """Sync ``OrganizationMember.status`` and ``tier`` from the subscription.

    Rules:
    - Never creates an :class:`OrganizationMember`. Creation lives in
      :func:`events.service.subscription.core.create_subscription` for the
      FREE and OFFLINE flows (OFFLINE holds a new or CANCELLED person as
      AWAITING_PAYMENT, activated by ``offline.activate_on_first_payment``;
      any other existing member is left unchanged), and in
      :func:`events.service.subscription.stripe.sync._ensure_active_member`
      for the ONLINE flow (gated on Stripe's first paid invoice / ``active``
      status, so members don't get tier benefits before paying).
    - Leaves ``BANNED`` members untouched.
    - Leaves a ``PAUSED`` member paused unless the subscription is itself
      transitioning *out of* PAUSED (the staff resume paths). A staff-imposed
      pause is mirrored onto the subscription, so any other save of a
      still-ACTIVE row — a webhook echo sync, an uncancel, an ``invoice.paid``
      on a row that was never PAUSED — used to silently lift the suspension.
    - Subscription tier wins: ``member.tier`` is set to ``plan.tier`` whenever
      they differ.
    - Never moves a member out of ``AWAITING_PAYMENT`` (only
      :func:`events.service.subscription.offline.activate_on_first_payment` does) and
      never moves status or tier while an OFFLINE subscription is still PENDING and never-paid
      (legacy ACTIVE members are not downgraded). Consequently ``staff_change_plan`` on a
      never-paid offline row moves the member's tier only at first payment.
    - Skips syncing when a newer non-terminal subscription exists for the
      same (user, org) — older terminal rows must not clobber the effective
      subscription when, e.g., admin re-saves a historical entry after the
      user has resubscribed.
    - For ONLINE plans in PENDING state — or in PAST_DUE without a paid
      period behind them — returns early so a subscription doesn't grant
      ACTIVE membership before Stripe collects the first invoice.
    """
    target_status = _SUBSCRIPTION_TO_MEMBER_STATUS.get(instance.status)
    if target_status is None:
        return

    # ONLINE plans gate ACTIVE membership on the first successful Stripe
    # payment. PENDING is the "awaiting first invoice" state — leave the
    # member alone until the webhook flips us to ACTIVE. A PAST_DUE row that
    # never collected a period is the same state wearing a dunning hat (first
    # invoice failed / stalled on SCA, revival checkout never settled), so it
    # gets the same treatment — see :func:`_has_paid_current_period`.
    if instance.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value and (
        instance.status == MembershipSubscription.SubscriptionStatus.PENDING.value
        or (
            instance.status == MembershipSubscription.SubscriptionStatus.PAST_DUE.value
            and not _has_paid_current_period(instance)
        )
    ):
        return

    if _never_paid_offline_handled(instance):
        return

    # If a newer non-terminal subscription exists, it owns the member state.
    newer_active_exists = (
        MembershipSubscription.objects.filter(
            organization_id=instance.organization_id,
            user_id=instance.user_id,
            created_at__gt=instance.created_at,
        )
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exists()
    )
    if newer_active_exists:
        return

    member = OrganizationMember.objects.filter(
        organization_id=instance.organization_id,
        user_id=instance.user_id,
    ).first()
    if member is None or _subscription_cannot_move(member, target_status):
        return

    # A staff pause is only lifted by the resume paths — OFFLINE
    # ``resume_subscription`` and ``resume_online_subscription`` both flip the
    # row PAUSED → ACTIVE, and so does the ``customer.subscription.updated``
    # echo that clears ``pause_collection``. Everything else that saves an
    # already-ACTIVE row must leave the member suspended (tier still follows
    # the plan). ``update_member(status=ACTIVE)`` is unaffected: it saves the
    # member row directly and never routes through this signal.
    if (
        member.status == OrganizationMember.MembershipStatus.PAUSED
        and target_status == OrganizationMember.MembershipStatus.ACTIVE
        and getattr(instance, "_old_status", None) != MembershipSubscription.SubscriptionStatus.PAUSED.value
    ):
        target_status = member.status

    target_tier_id = instance.plan.tier_id
    update_fields = []
    if member.status != target_status:
        member.status = target_status
        update_fields.append("status")
    if member.tier_id != target_tier_id:
        member.tier_id = target_tier_id
        update_fields.append("tier")

    if not update_fields:
        return

    update_fields.append("updated_at")
    with member_register_service.record_source(MembershipRecord.Source.SUBSCRIPTION):
        member.save(update_fields=update_fields)

    logger.info(
        "membership_subscription_synced_member",
        subscription_id=str(instance.id),
        member_id=str(member.id),
        status=member.status,
        tier_id=str(member.tier_id) if member.tier_id else None,
    )
