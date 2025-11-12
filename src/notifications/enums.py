"""Enums for the notification system."""

from django.db.models import TextChoices


class NotificationType(TextChoices):
    """All notification types in the system.

    Note: Account-specific transactional emails (signup, password reset, etc.)
    are handled separately in the accounts app and are NOT part of this system.
    """

    # Ticket notifications
    TICKET_CREATED = "ticket_created"
    TICKET_UPDATED = "ticket_updated"
    TICKET_CANCELLED = "ticket_cancelled"
    TICKET_REFUNDED = "ticket_refunded"
    PAYMENT_CONFIRMATION = "payment_confirmation"

    # Event notifications
    EVENT_OPEN = "event_open"
    EVENT_CREATED = "event_created"
    EVENT_UPDATED = "event_updated"
    EVENT_CANCELLED = "event_cancelled"
    EVENT_REMINDER = "event_reminder"

    # RSVP notifications
    RSVP_CONFIRMATION = "rsvp_confirmation"
    RSVP_UPDATED = "rsvp_updated"
    RSVP_CANCELLED = "rsvp_cancelled"

    # Potluck notifications
    POTLUCK_ITEM_CREATED = "potluck_item_created"
    POTLUCK_ITEM_UPDATED = "potluck_item_updated"
    POTLUCK_ITEM_CLAIMED = "potluck_item_claimed"
    POTLUCK_ITEM_UNCLAIMED = "potluck_item_unclaimed"

    # Questionnaire notifications
    QUESTIONNAIRE_SUBMITTED = "questionnaire_submitted"
    QUESTIONNAIRE_EVALUATION_RESULT = "questionnaire_evaluation_result"

    # Invitation notifications
    INVITATION_RECEIVED = "invitation_received"
    INVITATION_CLAIMED = "invitation_claimed"
    INVITATION_REVOKED = "invitation_revoked"

    # Membership notifications
    MEMBERSHIP_GRANTED = "membership_granted"
    MEMBERSHIP_PROMOTED = "membership_promoted"
    MEMBERSHIP_REMOVED = "membership_removed"
    MEMBERSHIP_REQUEST_APPROVED = "membership_request_approved"
    MEMBERSHIP_REQUEST_REJECTED = "membership_request_rejected"

    # Organization notifications
    ORG_ANNOUNCEMENT = "org_announcement"

    # System notifications
    MALWARE_DETECTED = "malware_detected"


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
