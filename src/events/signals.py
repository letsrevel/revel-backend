# src/events/signals.py

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    DEFAULT_TICKET_TIER_NAME,
    Blacklist,
    Event,
    EventInvitation,
    EventRSVP,
    GeneralUserPreferences,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PendingEventInvitation,
    Ticket,
    TicketTier,
)
from events.models.organization import MembershipTier
from events.service.blacklist_service import apply_blacklist_consequences, link_blacklist_entries_for_user
from events.service.follow_service import get_followers_for_new_event_notification
from events.service.potluck_service import unclaim_user_potluck_items
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from events.tasks import build_attendee_visibility_flags
from notifications.enums import NotificationType
from notifications.signals import notification_requested

__all__ = ["unclaim_user_potluck_items"]

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=Event)
def handle_event_save(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Handle event creation and updates."""
    # Create default ticket tier if needed
    if instance.requires_ticket and not TicketTier.objects.filter(event=instance).exists():
        TicketTier.objects.create(event=instance, name=DEFAULT_TICKET_TIER_NAME)


@receiver(post_save, sender=Organization)
def handle_organization_creation(
    sender: type[Organization], instance: Organization, created: bool, **kwargs: t.Any
) -> None:
    """Create default 'General membership' tier when organization is created."""
    if not created:
        return

    MembershipTier.objects.create(organization=instance, name="General membership")
    logger.info(
        "default_membership_tier_created",
        organization_id=str(instance.id),
        organization_name=instance.name,
    )


@receiver(post_save, sender=RevelUser)
def handle_user_creation(sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: t.Any) -> None:
    """Creates GeneralUserPreferences, links blacklist entries, and processes pending invitations."""
    if not created:
        return
    logger.info("revel_user_created", user_id=str(instance.id))
    GeneralUserPreferences.objects.create(user=instance)

    # Link any existing blacklist entries that match this user's identifiers
    if linked_count := link_blacklist_entries_for_user(instance):
        logger.info("blacklist_entries_linked", user_id=str(instance.id), count=linked_count)

    # Convert any pending invitations for this email to real invitations
    pending_invitations = PendingEventInvitation.objects.filter(email__iexact=instance.email)

    if pending_invitations.exists():
        logger.info(
            "converting_pending_invitations",
            user_id=str(instance.id),
            count=pending_invitations.count(),
        )

        with transaction.atomic():
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
                    waives_apply_deadline=pending.waives_apply_deadline,
                    custom_message=pending.custom_message,
                    tier=pending.tier,
                )
            pending_invitations.delete()


@receiver(post_save, sender=EventRSVP)
def handle_event_rsvp_save(sender: type[EventRSVP], instance: EventRSVP, created: bool, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after RSVP is changed or created.

    When a user's RSVP status changes to anything other than YES (i.e., NO or MAYBE),
    we automatically unclaim all potluck items they had previously claimed, since they
    are no longer confirmed to attend.
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

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
def handle_ticket_visibility_and_potluck(
    sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any
) -> None:
    """Trigger visibility task and unclaim potluck items when ticket status becomes CANCELLED.

    When a ticket's status changes to CANCELLED, we automatically unclaim all potluck items
    the user had claimed, since they are no longer confirmed to attend.

    Note: This is one of multiple post_save handlers for Ticket model:
    - events.signals.handle_ticket_visibility_and_potluck: Handles visibility flags + potluck (this handler)
    - notifications.signals.ticket.handle_ticket_notifications: Sends notifications
    - notifications.signals.waitlist.handle_ticket_waitlist_logic: Manages waitlist removal
    """
    build_attendee_visibility_flags.delay(str(instance.event_id))

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


@receiver(post_save, sender=OrganizationMember)
def handle_membership_granted(
    sender: type[OrganizationMember], instance: OrganizationMember, created: bool, **kwargs: t.Any
) -> None:
    """Send notification when user is granted membership to an organization."""
    if not created:
        return

    def send_membership_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        notification_requested.send(
            sender=sender,
            user=instance.user,
            notification_type=NotificationType.MEMBERSHIP_GRANTED,
            context={
                "organization_id": str(instance.organization_id),
                "organization_name": instance.organization.name,
                "role": "member",
                "action": "granted",
                "frontend_url": f"{frontend_base_url}/org/{instance.organization.slug}",
            },
        )

        logger.info(
            "membership_granted_notification_sent",
            organization_id=str(instance.organization_id),
            user_id=str(instance.user_id),
        )

    transaction.on_commit(send_membership_notification)


@receiver(post_delete, sender=OrganizationMember)
def handle_membership_removed(sender: type[OrganizationMember], instance: OrganizationMember, **kwargs: t.Any) -> None:
    """Send notification when user is removed from an organization."""

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


@receiver(post_save, sender=OrganizationStaff)
def handle_membership_promoted(
    sender: type[OrganizationStaff], instance: OrganizationStaff, created: bool, **kwargs: t.Any
) -> None:
    """Send notification when user is promoted to staff."""
    if not created:
        return

    def send_promotion_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

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


@receiver(pre_save, sender=Event)
def capture_event_old_status(sender: type[Event], instance: Event, **kwargs: t.Any) -> None:
    """Capture the old status value before save for change detection in post_save.

    This allows us to reliably detect when an event's status changes to OPEN,
    regardless of whether save() is called with or without update_fields.
    """
    if instance.pk:
        try:
            old_instance = Event.objects.only("status").get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except Event.DoesNotExist:
            # Event was deleted between check and fetch (race condition) - skip silently
            pass


def _should_notify_followers_for_event(event: Event, created: bool) -> bool:
    """Check if followers should be notified for this event status change.

    Returns True only when an event transitions to OPEN status (either on creation
    or via update). Prevents duplicate notifications.
    """
    if event.status != Event.EventStatus.OPEN:
        return False

    if created:
        return True

    # For existing events, check if status actually changed to OPEN
    old_status = getattr(event, "_old_status", None)
    return old_status is not None and old_status != Event.EventStatus.OPEN


def _get_event_location_string(event: Event) -> str:
    """Build a human-readable location string for an event."""
    location = event.address or ""
    if event.city:
        location = f"{location}, {event.city.name}" if location else event.city.name
    return location


@receiver(post_save, sender=Event)
def handle_event_opened_notify_followers(sender: type[Event], instance: Event, created: bool, **kwargs: t.Any) -> None:
    """Notify followers when an event becomes OPEN.

    Sends notifications to:
    - Organization followers who have notify_new_events enabled
    - Event series followers (if event belongs to a series) who have notify_new_events enabled

    Series followers are prioritized - if a user follows both the org and series,
    they receive the series notification only.
    """
    if not _should_notify_followers_for_event(instance, created):
        return

    organization = instance.organization
    if not organization:
        return

    def send_follower_notifications() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        event_series = instance.event_series
        event_location = _get_event_location_string(instance)

        for user, notification_type in get_followers_for_new_event_notification(organization, event_series):
            context: dict[str, t.Any] = {
                "organization_id": str(organization.id),
                "organization_name": organization.name,
                "event_id": str(instance.id),
                "event_name": instance.name,
                "event_description": instance.description or "",
                "event_start": instance.start.isoformat() if instance.start else "",
                "event_start_formatted": (instance.start.strftime("%B %d, %Y at %I:%M %p") if instance.start else ""),
                "event_location": event_location,
                "event_url": f"{frontend_base_url}/events/{instance.id}",
            }

            if notification_type == NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES and event_series:
                context["event_series_id"] = str(event_series.id)
                context["event_series_name"] = event_series.name

            notification_requested.send(
                sender=sender,
                user=user,
                notification_type=notification_type,
                context=context,
            )

        logger.info(
            "follower_notifications_sent_for_event",
            event_id=str(instance.id),
            organization_id=str(organization.id),
        )

    transaction.on_commit(send_follower_notifications)
