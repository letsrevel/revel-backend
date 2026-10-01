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
    # Inbound Stripe refund Revel refused to auto-match to a ticket (staff only)
    REFUND_UNMATCHED = "refund_unmatched"

    # Event notifications
    EVENT_OPEN = "event_open"
    EVENT_UPDATED = "event_updated"
    EVENT_CANCELLED = "event_cancelled"
    EVENT_REMINDER = "event_reminder"  # Placeholder - requires Celery periodic task
    # Bulk cancel-and-refund sweep on a cancelled event finished (staff only)
    EVENT_REFUND_SUMMARY = "event_refund_summary"

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
    MEMBERSHIP_CARD_UPDATED = "membership_card_updated"  # Tier changed - wallet card needs re-adding
    MEMBERSHIP_REQUEST_CREATED = "membership_request_created"  # User requests to join organization
    MEMBERSHIP_REQUEST_APPROVED = "membership_request_approved"
    MEMBERSHIP_REQUEST_REJECTED = "membership_request_rejected"

    # Organization notifications
    ORG_ANNOUNCEMENT = "org_announcement"
    ORG_CONTACT_MESSAGE_RECEIVED = "org_contact_message_received"  # Notify org admins of contact form submissions
    ORG_SETUP_NUDGE = "org_setup_nudge"  # Revel nudges a stalled org's owner (private profile, forgotten draft, ...)

    # Waitlist notifications
    WAITLIST_SPOT_AVAILABLE = "waitlist_spot_available"

    # Whitelist notifications (blacklist verification flow)
    WHITELIST_REQUEST_CREATED = "whitelist_request_created"  # User requests whitelist - notify org admins
    WHITELIST_REQUEST_APPROVED = "whitelist_request_approved"  # Notify user when approved
    WHITELIST_REQUEST_REJECTED = "whitelist_request_rejected"  # Notify user when rejected

    # Account notifications
    ACCOUNT_BANNED = "account_banned"

    # System notifications
    SYSTEM_ANNOUNCEMENT = "system_announcement"  # Platform-wide announcements (privacy, ToC, etc.)

    # Follow notifications
    ORGANIZATION_FOLLOWED = "organization_followed"  # Notify org admins when someone follows
    EVENT_SERIES_FOLLOWED = "event_series_followed"  # Notify org admins when someone follows a series
    NEW_EVENT_FROM_FOLLOWED_ORG = "new_event_from_followed_org"  # Notify followers of new event
    NEW_EVENT_FROM_FOLLOWED_SERIES = "new_event_from_followed_series"  # Notify followers of new event in series

    # Series notifications
    SERIES_EVENTS_GENERATED = "series_events_generated"  # Digest: N events generated for a series

    # Series pass notifications
    SERIES_PASS_PURCHASED = "series_pass_purchased"  # Notify holder + org staff/owners when a pass activates
    SERIES_PASS_EXTENDED = "series_pass_extended"  # Notify holder when their pass gains newly-covered events
    SERIES_PASS_CANCELLED = "series_pass_cancelled"  # Notify holder + org staff/owners when a pass is cancelled
    # Subscription notifications
    SUBSCRIPTION_RENEWAL_SUCCEEDED = "subscription_renewal_succeeded"
    SUBSCRIPTION_PAYMENT_FAILED = "subscription_payment_failed"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    SUBSCRIPTION_CANCELLATION_CONFIRMED = "subscription_cancellation_confirmed"
    SUBSCRIPTION_RENEWAL_REMINDER = "subscription_renewal_reminder"
    SUBSCRIPTION_PRICE_MIGRATION_NOTICE = "subscription_price_migration_notice"
    SUBSCRIPTION_REVIVAL_CHECKOUT = "subscription_revival_checkout"  # Staff revived: member gets the checkout link


# Transactional notification types are time-/money-sensitive (a ticket sale, a payment,
# a cancellation or a refund): they bypass the digest cadence (#506). Via MANDATORY_TYPES
# below they also bypass silence and per-type disables and always include in-app + email.
TRANSACTIONAL_TYPES: frozenset[str] = frozenset(
    {
        NotificationType.PAYMENT_CONFIRMATION,
        NotificationType.TICKET_CREATED,
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        # Subscription money/lifecycle events must not wait for a digest sweep:
        # a failed renewal or expiry needs immediate action, a revival checkout
        # link is time-boxed, and renewal/cancellation confirmations are
        # receipts. (RENEWAL_REMINDER and PRICE_MIGRATION_NOTICE are advance
        # notices with days of slack — they stay on the digest cadence.)
        NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        NotificationType.SUBSCRIPTION_EXPIRED,
        NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
    }
)

# Types users can't opt out of: bypass silence_all, the email switch, per-type disables, and the digest.
# SYSTEM_ANNOUNCEMENT is for ToS / privacy / platform-wide notices ONLY — never promotional content.
MANDATORY_TYPES: frozenset[str] = TRANSACTIONAL_TYPES | {
    NotificationType.ACCOUNT_BANNED,
    NotificationType.SYSTEM_ANNOUNCEMENT,
}

# Revel-written, opt-out-able mail that isn't org-sent but should still carry one-click
# List-Unsubscribe (bulk-sender rules for promotional-ish mail).
PLATFORM_LIST_UNSUBSCRIBE_TYPES: frozenset[str] = frozenset({NotificationType.ORG_SETUP_NUDGE})

# Org-written AND org-pushed mail: sent as "<Org> via Revel" <slug@ORG_EMAIL_DOMAIN>, with List-Unsubscribe.
ORG_SENDER_TYPES: frozenset[str] = frozenset(
    {
        NotificationType.ORG_ANNOUNCEMENT,
        NotificationType.INVITATION_RECEIVED,
        NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
        NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
        NotificationType.EVENT_OPEN,
        NotificationType.EVENT_UPDATED,
        NotificationType.EVENT_CANCELLED,
        NotificationType.EVENT_REMINDER,
    }
)


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
