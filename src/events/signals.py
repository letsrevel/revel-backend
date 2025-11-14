# src/telegram/signals.py

import typing as t
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    DEFAULT_TICKET_TIER_NAME,
    Event,
    EventInvitation,
    EventRSVP,
    GeneralUserPreferences,
    PendingEventInvitation,
    PotluckItem,
    Ticket,
    TicketTier,
)
from events.service.notification_service import (
    get_eligible_users_for_event_notification,
)
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from events.tasks import build_attendee_visibility_flags
from notifications.enums import NotificationType
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def unclaim_user_potluck_items(event_id: UUID, user_id: UUID, notify: bool = True) -> int:
    """Unclaim all potluck items for a user at an event.

    This function is called when a user's participation status changes to a non-confirmed
    state (RSVP NO/MAYBE, ticket cancelled, or participation deleted). It removes the user
    as the assignee from all potluck items they had claimed for this event.

    Args:
        event_id: UUID of the event
        user_id: UUID of the user whose items should be unclaimed
        notify: Whether to send notifications about the unclaimed items (default: True)

    Returns:
        The number of items that were unclaimed
    """
    # Get items before unclaiming (for notification purposes)
    if notify:
        items = list(PotluckItem.objects.filter(event_id=event_id, assignee=user_id).select_related("event"))

    unclaimed_count = PotluckItem.objects.filter(event_id=event_id, assignee=user_id).update(assignee=None)

    if notify and unclaimed_count > 0:
        logger.info(
            "potluck_items_auto_unclaimed",
            count=unclaimed_count,
            user_id=str(user_id),
            event_id=str(event_id),
        )

        # Send notification for each unclaimed item
        # Schedule notification task to run after the current transaction commits
        def send_notifications() -> None:
            user = RevelUser.objects.get(pk=user_id)
            for item in items:
                event = item.event
                # Build frontend URL
                frontend_base_url = SiteSettings.get_solo().frontend_base_url
                frontend_url = f"{frontend_base_url}/events/{event.id}"

                # Get all users who should be notified
                eligible_users = get_eligible_users_for_event_notification(
                    event, NotificationType.POTLUCK_ITEM_UNCLAIMED
                )
                for recipient in eligible_users:
                    notification_requested.send(
                        sender=unclaim_user_potluck_items,
                        user=recipient,
                        notification_type=NotificationType.POTLUCK_ITEM_UNCLAIMED,
                        context={
                            "potluck_item_id": str(item.id),
                            "item_name": item.name,
                            "event_id": str(event.id),
                            "event_name": event.name,
                            "action": "unclaimed",
                            "changed_by_username": user.first_name or user.username,
                            "frontend_url": frontend_url,
                        },
                    )

        transaction.on_commit(send_notifications)

    return unclaimed_count


@receiver(post_save, sender=Event)
def handle_event_save(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Handle event creation and updates."""
    # Create default ticket tier if needed
    if instance.requires_ticket and not TicketTier.objects.filter(event=instance).exists():
        TicketTier.objects.create(event=instance, name=DEFAULT_TICKET_TIER_NAME)

    # Send notification when event is created as OPEN or when status field is explicitly updated
    # This is triggered either:
    # 1. When an event is created with status=OPEN
    # 2. When status is explicitly in update_fields (updated via admin or API)
    # Note: For more robust change detection, django-model-utils FieldTracker could be used
    if instance.status == Event.EventStatus.OPEN:
        update_fields = kwargs.get("update_fields")

        # Trigger notification if:
        # - Created as OPEN
        # - Status field was explicitly updated (indicates intentional status change)
        if created or (update_fields and "status" in update_fields):
            from events.service.event_notification_service import notify_event_opened

            transaction.on_commit(lambda: notify_event_opened(instance))


@receiver(post_save, sender=RevelUser)
def handle_user_creation(sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: t.Any) -> None:
    """Creates GeneralUserPreferences and processes pending invitations when a new RevelUser is created."""
    if created:
        logger.info("revel_user_created", user_id=str(instance.id))
        GeneralUserPreferences.objects.create(user=instance)

        # Convert any pending invitations for this email to real invitations
        pending_invitations = PendingEventInvitation.objects.filter(email__iexact=instance.email)

        if pending_invitations.exists():
            logger.info(
                "converting_pending_invitations",
                user_id=str(instance.id),
                count=pending_invitations.count(),
            )

            for pending in pending_invitations:
                # Create EventInvitation from PendingEventInvitation
                EventInvitation.objects.create(
                    event=pending.event,
                    user=instance,
                    waives_questionnaire=pending.waives_questionnaire,
                    waives_purchase=pending.waives_purchase,
                    overrides_max_attendees=pending.overrides_max_attendees,
                    waives_membership_required=pending.waives_membership_required,
                    waives_rsvp_deadline=pending.waives_rsvp_deadline,
                    custom_message=pending.custom_message,
                    tier=pending.tier,
                )

            # Delete the pending invitations
            pending_invitations.delete()


def _build_rsvp_context(rsvp: EventRSVP) -> dict[str, t.Any]:
    """Build notification context for RSVP."""
    return {
        "rsvp_id": str(rsvp.id),
        "event_id": str(rsvp.event.id),
        "event_name": rsvp.event.name,
        "event_start": rsvp.event.start.isoformat(),
        "event_location": rsvp.event.address or (rsvp.event.city.name if rsvp.event.city else ""),
        "response": rsvp.status,
        "plus_ones": 0,  # EventRSVP doesn't have plus_ones field yet
        "user_name": rsvp.user.get_display_name(),
        "user_email": rsvp.user.email,
    }


def _notify_staff_about_rsvp(rsvp: EventRSVP, notification_type: str, context: dict[str, t.Any]) -> None:
    """Notify staff/owners about RSVP event."""
    from events.service.notification_service import get_organization_staff_and_owners

    staff_and_owners = get_organization_staff_and_owners(rsvp.event.organization_id)
    for recipient in staff_and_owners:
        notification_requested.send(
            sender=EventRSVP,
            user=recipient,
            notification_type=notification_type,
            context=context,
        )


def _send_rsvp_confirmation_notifications(rsvp: EventRSVP) -> None:
    """Send notifications when RSVP is created."""
    context = _build_rsvp_context(rsvp)
    _notify_staff_about_rsvp(rsvp, NotificationType.RSVP_CONFIRMATION, context)


def _send_rsvp_updated_notifications(rsvp: EventRSVP) -> None:
    """Send notifications when RSVP is updated."""
    # Check if old status was captured in pre_save
    if not hasattr(rsvp, "_old_status"):
        return  # No status change

    old_status = rsvp._old_status

    # Skip if old and new status are the same
    if old_status == rsvp.status:
        return

    context = {
        "rsvp_id": str(rsvp.id),
        "event_id": str(rsvp.event.id),
        "event_name": rsvp.event.name,
        "old_response": old_status,
        "new_response": rsvp.status,
        "user_name": rsvp.user.get_display_name(),
        "user_email": rsvp.user.email,
    }

    _notify_staff_about_rsvp(rsvp, NotificationType.RSVP_UPDATED, context)


@receiver(pre_save, sender=EventRSVP)
def capture_rsvp_old_status(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save."""
    if instance.pk:
        try:
            old_instance = EventRSVP.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except EventRSVP.DoesNotExist:
            pass


@receiver(post_save, sender=EventRSVP)
def handle_event_rsvp_save(sender: type[EventRSVP], instance: EventRSVP, created: bool, **kwargs: t.Any) -> None:
    """Trigger visibility task, unclaim potluck items, and send notifications after RSVP is changed or created.

    When a user's RSVP status changes to anything other than YES (i.e., NO or MAYBE),
    we automatically unclaim all potluck items they had previously claimed, since they
    are no longer confirmed to attend.

    Sends notifications to:
    - Organization staff and owners (NOT the user who RSVPed)
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

    if instance.status in [EventRSVP.RsvpStatus.NO, EventRSVP.RsvpStatus.MAYBE]:
        unclaim_user_potluck_items(instance.event_id, instance.user_id)

    def send_notifications() -> None:
        if created:
            _send_rsvp_confirmation_notifications(instance)
        else:
            _send_rsvp_updated_notifications(instance)

    transaction.on_commit(send_notifications)


@receiver(post_delete, sender=EventRSVP)
def handle_event_rsvp_delete(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Trigger visibility task, unclaim potluck items, and send notifications after RSVP is deleted.

    When a user deletes their RSVP entirely, we unclaim all potluck items they had claimed.

    Sends notifications to:
    - Organization staff and owners (the user already knows they cancelled)
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))
    # Unclaim items when RSVP is deleted entirely
    unclaim_user_potluck_items(instance.event_id, instance.user_id)

    # Send notifications after transaction commits
    def send_notifications() -> None:
        from events.service.notification_service import get_organization_staff_and_owners

        event = instance.event
        user = instance.user

        notification_type = NotificationType.RSVP_CANCELLED
        context = {
            "event_id": str(event.id),
            "event_name": event.name,
            "user_name": user.first_name or user.username,
        }

        # Notify organization staff and owners only (user already knows they cancelled)
        staff_and_owners = get_organization_staff_and_owners(event.organization_id)
        for recipient in staff_and_owners:
            notification_requested.send(
                sender=sender,
                user=recipient,
                notification_type=notification_type,
                context=context,
            )

    transaction.on_commit(send_notifications)


@receiver(post_delete, sender=Ticket)
def handle_ticket_delete(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after Ticket is deleted.

    When a user's ticket is deleted entirely, we unclaim all potluck items they had claimed.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))
    # Unclaim items when ticket is deleted
    unclaim_user_potluck_items(instance.event_id, instance.user_id)


@receiver(post_save, sender=EventInvitation)
def handle_invitation_save(
    sender: type[EventInvitation], instance: EventInvitation, created: bool, **kwargs: t.Any
) -> None:
    """Trigger visibility task and send notifications after invitation is created."""
    build_attendee_visibility_flags.delay(str(instance.event_id))
    if not created:
        return

    # Send INVITATION_RECEIVED notification to the invited user
    def send_invitation_notification() -> None:
        event = instance.event
        # Get the user who created the invitation (assume it's org staff/owner)
        invited_by_name = "Someone"  # Default if we can't determine

        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.INVITATION_RECEIVED,
            context={
                "invitation_id": str(instance.id),
                "event_id": str(event.id),
                "event_name": event.name,
                "event_start": event.start.isoformat() if event.start else "",
                "invited_by_name": invited_by_name,
                "personal_message": instance.custom_message or "",
            },
        )

    transaction.on_commit(send_invitation_notification)


@receiver(post_delete, sender=EventInvitation)
def handle_invitation_delete(sender: type[EventInvitation], instance: EventInvitation, **kwargs: t.Any) -> None:
    """Trigger visibility task after invitation is deleted."""
    build_attendee_visibility_flags.delay(str(instance.event_id))


@receiver(post_save, sender=GeneralUserPreferences)
def handle_default_user_pref_save(
    sender: type[GeneralUserPreferences], instance: GeneralUserPreferences, **kwargs: t.Any
) -> None:
    """Trigger visibility task after user preferences is changed or created."""
    # Iterate over all future events the user is attending
    trigger_visibility_flags_for_user(instance.user_id)


# ---- New Notification Signal Handlers ----


@receiver(post_save, sender=PotluckItem)
def handle_potluck_item_save(sender: type[PotluckItem], instance: PotluckItem, created: bool, **kwargs: t.Any) -> None:
    """Handle potluck item creation and updates."""
    event = instance.event

    if created:
        action = "created"
        notification_type = NotificationType.POTLUCK_ITEM_CREATED
        logger.info("potluck_item_created", potluck_item_id=str(instance.id), event_id=str(event.id))
    else:
        # For updates, determine action based on assignee
        if instance.assignee:
            action = "claimed"
            notification_type = NotificationType.POTLUCK_ITEM_CLAIMED
        else:
            action = "unclaimed"
            notification_type = NotificationType.POTLUCK_ITEM_UNCLAIMED
        logger.info("potluck_item_updated", potluck_item_id=str(instance.id), event_id=str(event.id), action=action)

    # Get all eligible users for notification
    def send_notifications() -> None:
        # Build frontend URL
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        frontend_url = f"{frontend_base_url}/events/{event.id}"

        eligible_users = get_eligible_users_for_event_notification(event, notification_type)

        for user in eligible_users:
            context = {
                "potluck_item_id": str(instance.id),
                "item_name": instance.name,
                "event_id": str(event.id),
                "event_name": event.name,
                "action": action,
                "frontend_url": frontend_url,
            }

            # Add assignee username if claimed
            if action == "claimed" and instance.assignee:
                context["assigned_to_username"] = instance.assignee.first_name or instance.assignee.username

            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=notification_type,
                context=context,
            )

    transaction.on_commit(send_notifications)


@receiver(post_delete, sender=PotluckItem)
def handle_potluck_item_delete(sender: type[PotluckItem], instance: PotluckItem, **kwargs: t.Any) -> None:
    """Handle potluck item deletion."""
    logger.info("potluck_item_deleted", potluck_item_id=str(instance.id), event_id=str(instance.event_id))
    event = instance.event

    # Get all eligible users for notification
    def send_notifications() -> None:
        eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_ITEM_UPDATED)

        for user in eligible_users:
            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=NotificationType.POTLUCK_ITEM_UPDATED,
                context={
                    "potluck_item_id": str(instance.id),
                    "item_name": instance.name,
                    "event_id": str(event.id),
                    "event_name": event.name,
                    "action": "deleted",
                },
            )

    transaction.on_commit(send_notifications)


# ---- Ticket Notification Signal Handlers ----


def _get_ticket_action_for_payment_method(payment_method: str) -> str | None:
    """Get action string for ticket creation based on payment method."""
    action_map = {
        TicketTier.PaymentMethod.FREE: "free_ticket_created",
        TicketTier.PaymentMethod.OFFLINE: "offline_payment_pending",
        TicketTier.PaymentMethod.AT_THE_DOOR: "at_door_payment_pending",
    }
    return t.cast(str | None, action_map.get(payment_method))  # type: ignore[call-overload]


def _build_ticket_created_context(ticket: Ticket) -> dict[str, t.Any]:
    """Build notification context for TICKET_CREATED."""
    event = ticket.event
    event_location = event.address or (event.city.name if event.city else "")

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start": event.start.isoformat() if event.start else "",
        "event_location": event_location,
        "organization_id": str(event.organization_id),
        "organization_name": event.organization.name,
        "tier_name": ticket.tier.name,
        "tier_price": str(ticket.tier.price),
        "quantity": 1,  # Single ticket per notification
        "total_price": str(ticket.tier.price),
        "frontend_url": frontend_url,
    }


def _build_ticket_updated_context(ticket: Ticket, old_status: str) -> dict[str, t.Any]:
    """Build notification context for TICKET_UPDATED."""
    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(ticket.event.id),
        "event_name": ticket.event.name,
        "old_status": old_status,
        "new_status": ticket.status,
    }


def _build_ticket_refunded_context(ticket: Ticket, refund_amount: str | None = None) -> dict[str, t.Any]:
    """Build notification context for TICKET_REFUNDED."""
    return {
        "ticket_id": str(ticket.id),
        "ticket_reference": str(ticket.id),
        "event_id": str(ticket.event.id),
        "event_name": ticket.event.name,
        "refund_amount": refund_amount or str(ticket.tier.price),
    }


def _send_ticket_created_notifications(ticket: Ticket) -> None:
    """Send notifications for newly created ticket."""
    from events.service.notification_service import get_organization_staff_and_owners

    action = _get_ticket_action_for_payment_method(ticket.tier.payment_method)
    if not action:
        return  # Online payment - handled by payment service

    context = _build_ticket_created_context(ticket)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_CREATED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in list(staff_and_owners):
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_CREATED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_CREATED,
                context=staff_context,
            )


def _send_ticket_activated_notification(ticket: Ticket, old_status: str) -> None:
    """Send notification when ticket is activated."""
    context = _build_ticket_updated_context(ticket, old_status)

    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_UPDATED,
        context=context,
    )


def _send_ticket_cancelled_notifications(ticket: Ticket, old_status: str) -> None:
    """Send notifications when ticket is cancelled."""
    from events.service.notification_service import get_organization_staff_and_owners

    context = _build_ticket_updated_context(ticket, old_status)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_UPDATED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in list(staff_and_owners):
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_UPDATED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_UPDATED,
                context=staff_context,
            )


def _send_ticket_refunded_notifications(ticket: Ticket) -> None:
    """Send notifications when ticket is refunded."""
    from events.service.notification_service import get_organization_staff_and_owners

    # Add refund amount if available
    refund_amount_value = getattr(ticket, "_refund_amount", None)
    context = _build_ticket_refunded_context(ticket, refund_amount_value)

    # Notify ticket holder
    notification_requested.send(
        sender=Ticket,
        user=ticket.user,
        notification_type=NotificationType.TICKET_REFUNDED,
        context=context,
    )

    # Notify staff/owners with additional context
    staff_context = {
        **context,
        "ticket_holder_name": ticket.user.get_display_name(),
        "ticket_holder_email": ticket.user.email,
    }
    staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
    for staff_user in list(staff_and_owners):
        prefs = getattr(staff_user, "notification_preferences", None)
        if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_REFUNDED):
            notification_requested.send(
                sender=Ticket,
                user=staff_user,
                notification_type=NotificationType.TICKET_REFUNDED,
                context=staff_context,
            )


def _handle_ticket_status_change(ticket: Ticket, old_status: str | None) -> None:
    """Handle notifications for ticket status changes."""
    if not old_status or old_status == ticket.status:
        return

    if ticket.status == Ticket.TicketStatus.ACTIVE:
        _send_ticket_activated_notification(ticket, old_status)

        # If payment was just completed (PENDING→ACTIVE), notify staff/owners
        if old_status == Ticket.TicketStatus.PENDING:
            from events.service.notification_service import get_organization_staff_and_owners

            context = _build_ticket_created_context(ticket)
            staff_context = {
                **context,
                "ticket_holder_name": ticket.user.get_display_name(),
                "ticket_holder_email": ticket.user.email,
            }
            staff_and_owners = get_organization_staff_and_owners(ticket.event.organization_id)
            for staff_user in list(staff_and_owners):
                prefs = getattr(staff_user, "notification_preferences", None)
                if prefs and prefs.is_notification_type_enabled(NotificationType.TICKET_CREATED):
                    notification_requested.send(
                        sender=Ticket,
                        user=staff_user,
                        notification_type=NotificationType.TICKET_CREATED,
                        context=staff_context,
                    )
    elif ticket.status == Ticket.TicketStatus.CANCELLED:
        # Check if this is a refund-related cancellation
        if getattr(ticket, "_is_refund", False):
            _send_ticket_refunded_notifications(ticket)
        else:
            _send_ticket_cancelled_notifications(ticket, old_status)


@receiver(pre_save, sender=Ticket)
def capture_ticket_old_status(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save."""
    if instance.pk:
        try:
            old_instance = Ticket.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Ticket.DoesNotExist:
            pass


@receiver(post_save, sender=Ticket)
def handle_ticket_save_and_notifications(
    sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any
) -> None:
    """Trigger visibility task, unclaim potluck items, and send notifications for ticket lifecycle."""
    build_attendee_visibility_flags.delay(str(instance.event_id))

    if instance.status == Ticket.TicketStatus.CANCELLED:
        unclaim_user_potluck_items(instance.event_id, instance.user_id)

    def send_notifications() -> None:
        if created:
            _send_ticket_created_notifications(instance)
        else:
            # Check if old status was captured in pre_save
            if hasattr(instance, "_old_status"):
                _handle_ticket_status_change(instance, instance._old_status)

    transaction.on_commit(send_notifications)


# Note: TICKET_REFUNDED notifications are handled by Payment model signals
# in the stripe service when a refund is processed, not by Ticket model
