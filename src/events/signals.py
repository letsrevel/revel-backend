# src/telegram/signals.py

import logging
import typing as t
from uuid import UUID

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
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from events.tasks import (
    build_attendee_visibility_flags,
    notify_potluck_item_update,
)

logger = logging.getLogger(__name__)


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
    unclaimed_count = PotluckItem.objects.filter(event_id=event_id, assignee=user_id).update(assignee=None)

    if notify and unclaimed_count > 0:
        logger.info(
            f"Auto-unclaimed {unclaimed_count} potluck item(s) for user {user_id} at event {event_id} "
            f"due to participation status change"
        )
        # Schedule notification task to run after the current transaction commits
        # This ensures the database changes are committed before we send notifications
        # Note: We pass a placeholder item ID since the notification system expects it,
        # but in the future this could be enhanced to support bulk notifications
        transaction.on_commit(
            lambda: notify_potluck_item_update.delay(
                potluck_item_id=str(event_id),  # Use event_id as placeholder for bulk unclaim
                action="auto_unclaimed",
                changed_by_user_id=str(user_id),
            )
        )

    return unclaimed_count


@receiver(post_save, sender=Event)
def handle_event_save(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Handle event creation and updates."""
    # Create default ticket tier if needed
    if instance.requires_ticket and not TicketTier.objects.filter(event=instance).exists():
        TicketTier.objects.create(event=instance, name=DEFAULT_TICKET_TIER_NAME)

    # Send notification when event status changes to OPEN
    # Note: For now, we'll rely on manual triggering or a separate management command
    # TODO: Implement proper status change detection using django-model-utils or custom tracking


@receiver(post_save, sender=RevelUser)
def handle_user_creation(sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: t.Any) -> None:
    """Creates GeneralUserPreferences and processes pending invitations when a new RevelUser is created."""
    if created:
        logger.info(f"New RevelUser created (ID: {instance.id}). Creating settings.")
        GeneralUserPreferences.objects.create(user=instance)

        # Convert any pending invitations for this email to real invitations
        pending_invitations = PendingEventInvitation.objects.filter(email__iexact=instance.email)

        if pending_invitations.exists():
            logger.info(f"Converting {pending_invitations.count()} pending invitations for {instance.email}")

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
    if instance.status in [EventRSVP.Status.NO, EventRSVP.Status.MAYBE]:
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
    if instance.status == Ticket.Status.CANCELLED:
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
    if created:
        logger.info(f"PotluckItem {instance.id} created for event {instance.event.name}, sending notifications")
        notify_potluck_item_update.delay(
            potluck_item_id=str(instance.id),
            action="created",
            changed_by_user_id=None,  # TODO: Track who created the item
        )
    else:
        # For updates, we need to check if assignment changed
        # This is a simplified approach - in a real implementation you'd track field changes
        action = "assigned" if instance.assignee else "unassigned"
        logger.info(f"PotluckItem {instance.id} updated ({action}) for event {instance.event.name}")
        notify_potluck_item_update.delay(
            potluck_item_id=str(instance.id),
            action=action,
            changed_by_user_id=None,  # TODO: Track who made the change
        )


@receiver(post_delete, sender=PotluckItem)
def handle_potluck_item_delete(sender: type[PotluckItem], instance: PotluckItem, **kwargs: t.Any) -> None:
    """Handle potluck item deletion."""
    logger.info(f"PotluckItem {instance.id} deleted for event {instance.event.name}, sending notifications")
    notify_potluck_item_update.delay(
        potluck_item_id=str(instance.id),
        action="deleted",
        changed_by_user_id=None,  # TODO: Track who deleted the item
    )


# Ticket notifications are now handled in the service layer, not signals


# ---- Questionnaire Notification Signal Handlers ----
# Note: Questionnaire submission notifications are now handled directly in the service layer
# to avoid signal complexity and ensure they only fire when appropriate


# Note: Questionnaire evaluation notifications are also now handled in the service layer
# when evaluations are created/updated to ensure proper timing and avoid duplicate signals
