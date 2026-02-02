"""Enums for the notification system."""

from django.db.models import TextChoices


class NotificationType(TextChoices):
    """All notification types in the system.

    Note: Account-specific transactional emails (signup, password reset, etc.)
    are handled separately in the accounts app and are NOT part of this system.
    """

    # Ticket notifications (need 1 version for recipients, one for owners/staff)
    TICKET_CREATED = "ticket_created"
    TICKET_UPDATED = "ticket_updated"
    TICKET_CANCELLED = "ticket_cancelled"
    TICKET_REFUNDED = "ticket_refunded"
    TICKET_CHECKED_IN = "ticket_checked_in"
    PAYMENT_CONFIRMATION = "payment_confirmation"

    # Event notifications
    EVENT_OPEN = "event_open"
    EVENT_UPDATED = "event_updated"
    EVENT_CANCELLED = "event_cancelled"
    EVENT_REMINDER = "event_reminder"  # Placeholder - requires Celery periodic task

    # RSVP notifications
    RSVP_CONFIRMATION = "rsvp_confirmation"
    RSVP_UPDATED = "rsvp_updated"
    RSVP_CANCELLED = "rsvp_cancelled"

    # Potluck notifications (need 1 version for recipients, one for owners/staff)
    POTLUCK_ITEM_CREATED = "potluck_item_created"
    POTLUCK_ITEM_CREATED_AND_CLAIMED = "potluck_item_created_and_claimed"  # Atomic create+claim
    POTLUCK_ITEM_UPDATED = "potluck_item_updated"
    POTLUCK_ITEM_CLAIMED = "potluck_item_claimed"
    POTLUCK_ITEM_UNCLAIMED = "potluck_item_unclaimed"
    POTLUCK_ITEM_DELETED = "potluck_item_deleted"

    # Questionnaire notifications
    QUESTIONNAIRE_SUBMITTED = "questionnaire_submitted"  # (need 1 version for recipients, one for owners/staff)
    QUESTIONNAIRE_EVALUATION_RESULT = "questionnaire_evaluation_result"  # (only needed for users)

    # Invitation notifications
    INVITATION_RECEIVED = "invitation_received"
    INVITATION_REQUEST_CREATED = "invitation_request_created"  # User requests invitation to event

    # Membership notifications
    MEMBERSHIP_GRANTED = "membership_granted"
    MEMBERSHIP_PROMOTED = "membership_promoted"
    MEMBERSHIP_REMOVED = "membership_removed"
    MEMBERSHIP_REQUEST_CREATED = "membership_request_created"  # User requests to join organization
    MEMBERSHIP_REQUEST_APPROVED = "membership_request_approved"
    MEMBERSHIP_REQUEST_REJECTED = "membership_request_rejected"

    # Organization notifications
    ORG_ANNOUNCEMENT = "org_announcement"  # Placeholder - requires API endpoint

    # Waitlist notifications
    WAITLIST_SPOT_AVAILABLE = "waitlist_spot_available"

    # Whitelist notifications (blacklist verification flow)
    WHITELIST_REQUEST_CREATED = "whitelist_request_created"  # User requests whitelist - notify org admins
    WHITELIST_REQUEST_APPROVED = "whitelist_request_approved"  # Notify user when approved
    WHITELIST_REQUEST_REJECTED = "whitelist_request_rejected"  # Notify user when rejected

    # Follow notifications
    ORGANIZATION_FOLLOWED = "organization_followed"  # Notify org admins when someone follows
    EVENT_SERIES_FOLLOWED = "event_series_followed"  # Notify org admins when someone follows a series
    NEW_EVENT_FROM_FOLLOWED_ORG = "new_event_from_followed_org"  # Notify followers of new event
    NEW_EVENT_FROM_FOLLOWED_SERIES = "new_event_from_followed_series"  # Notify followers of new event in series


class DeliveryChannel(TextChoices):
    """Notification delivery channels."""

    IN_APP = "in_app"
    EMAIL = "email"
    TELEGRAM = "telegram"


class DeliveryStatus(TextChoices):
    """Status of notification delivery."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
