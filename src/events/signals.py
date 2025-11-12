# src/telegram/signals.py

import typing as t
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from accounts.models import RevelUser
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
    UserEventPreferences,
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


@receiver(post_save, sender=EventRSVP)
def handle_event_rsvp_save(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after RSVP is changed or created.

    When a user's RSVP status changes to anything other than YES (i.e., NO or MAYBE),
    we automatically unclaim all potluck items they had previously claimed, since they
    are no longer confirmed to attend.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

    # Unclaim potluck items if RSVP is not a definite YES
    if instance.status in [EventRSVP.RsvpStatus.NO, EventRSVP.RsvpStatus.MAYBE]:
        unclaim_user_potluck_items(instance.event_id, instance.user_id)


@receiver(post_delete, sender=EventRSVP)
def handle_event_rsvp_delete(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after RSVP is deleted.

    When a user deletes their RSVP entirely, we unclaim all potluck items they had claimed.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))
    # Unclaim items when RSVP is deleted entirely
    unclaim_user_potluck_items(instance.event_id, instance.user_id)


@receiver(post_save, sender=Ticket)
def handle_ticket_save(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after Ticket is changed or created.

    When a user's ticket status changes to CANCELLED, we automatically unclaim all potluck
    items they had previously claimed, since they are no longer attending.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

    # Unclaim potluck items if ticket is cancelled
    if instance.status == Ticket.TicketStatus.CANCELLED:
        unclaim_user_potluck_items(instance.event_id, instance.user_id)


@receiver(post_delete, sender=Ticket)
def handle_ticket_delete(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after Ticket is deleted.

    When a user's ticket is deleted entirely, we unclaim all potluck items they had claimed.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))
    # Unclaim items when ticket is deleted
    unclaim_user_potluck_items(instance.event_id, instance.user_id)


@receiver(post_save, sender=EventInvitation)
def handle_invitation_save(sender: type[EventInvitation], instance: EventInvitation, **kwargs: t.Any) -> None:
    """Trigger visibility task after invitation is changed or created."""
    build_attendee_visibility_flags.delay(str(instance.event_id))


@receiver(post_delete, sender=EventInvitation)
def handle_invitation_delete(sender: type[EventInvitation], instance: EventInvitation, **kwargs: t.Any) -> None:
    """Trigger visibility task after invitation is deleted."""
    build_attendee_visibility_flags.delay(str(instance.event_id))


@receiver(post_save, sender=UserEventPreferences)
def handle_event_user_pref_save(
    sender: type[UserEventPreferences], instance: UserEventPreferences, **kwargs: t.Any
) -> None:
    """Trigger visibility task after user preferences is changed or created."""
    build_attendee_visibility_flags.delay(str(instance.event_id))


@receiver(post_delete, sender=UserEventPreferences)
def handle_event_user_pref_delete(
    sender: type[UserEventPreferences], instance: UserEventPreferences, **kwargs: t.Any
) -> None:
    """Trigger visibility task after user preferences is deleted."""
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
        eligible_users = get_eligible_users_for_event_notification(event, notification_type)

        for user in eligible_users:
            context = {
                "potluck_item_id": str(instance.id),
                "item_name": instance.name,
                "event_id": str(event.id),
                "event_name": event.name,
                "action": action,
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


# Ticket notifications are now handled in the service layer, not signals


# ---- Questionnaire Notification Signal Handlers ----
# Note: Questionnaire submission notifications are now handled directly in the service layer
# to avoid signal complexity and ensure they only fire when appropriate


# Note: Questionnaire evaluation notifications are also now handled in the service layer
# when evaluations are created/updated to ensure proper timing and avoid duplicate signals
