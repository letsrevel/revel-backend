"""Enums for the notification system."""

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class NotificationType(TextChoices):
    """All notification types in the system.

    Note: Account-specific transactional emails (signup, password reset, etc.)
    are handled separately in the accounts app and are NOT part of this system.
    """

    # Ticket notifications (holder-facing; staff get the STAFF_TICKET_* types below)
    TICKET_CREATED = "ticket_created", _("Ticket created")
    TICKET_UPDATED = "ticket_updated", _("Ticket updated")
    TICKET_CANCELLED = "ticket_cancelled", _("Ticket cancelled")
    TICKET_REFUNDED = "ticket_refunded", _("Ticket refunded")
    TICKET_CHECKED_IN = "ticket_checked_in", _("Ticket checked in")
    PAYMENT_CONFIRMATION = "payment_confirmation", _("Payment confirmation")
    # Inbound Stripe refund Revel refused to auto-match to a ticket (staff only)
    REFUND_UNMATCHED = "refund_unmatched", _("Unmatched refund")
    # Org staff/owner alerts about ticket sales, cancellations and refunds (#1127). Separate
    # types from the holder receipts so mandatory delivery never overrides staff preferences.
    STAFF_TICKET_CREATED = "staff_ticket_created", _("Ticket sold")
    STAFF_TICKET_CANCELLED = "staff_ticket_cancelled", _("Ticket cancelled by attendee")
    STAFF_TICKET_REFUNDED = "staff_ticket_refunded", _("Ticket refunded to attendee")
    # A card dispute was opened or closed on an organizer payment (evidence deadlines are time-boxed)
    STAFF_PAYMENT_DISPUTED = "staff_payment_disputed", _("Payment disputed")

    # Event notifications
    EVENT_OPEN = "event_open", _("Event open")
    EVENT_UPDATED = "event_updated", _("Event updated")
    EVENT_CANCELLED = "event_cancelled", _("Event cancelled")
    EVENT_REMINDER = "event_reminder", _("Event reminder")  # Placeholder - requires Celery periodic task
    # Bulk cancel-and-refund sweep on a cancelled event finished (staff only)
    EVENT_REFUND_SUMMARY = "event_refund_summary", _("Event refund summary")

    # RSVP notifications
    RSVP_CONFIRMATION = "rsvp_confirmation", _("RSVP confirmation")
    RSVP_UPDATED = "rsvp_updated", _("RSVP updated")
    RSVP_CANCELLED = "rsvp_cancelled", _("RSVP cancelled")

    # Potluck notifications (need 1 version for recipients, one for owners/staff)
    POTLUCK_ITEM_CREATED = "potluck_item_created", _("Potluck item added")
    POTLUCK_ITEM_CREATED_AND_CLAIMED = (
        "potluck_item_created_and_claimed",
        _("Potluck item added and claimed"),
    )  # Atomic create+claim
    POTLUCK_ITEM_UPDATED = "potluck_item_updated", _("Potluck item updated")
    POTLUCK_ITEM_CLAIMED = "potluck_item_claimed", _("Potluck item claimed")
    POTLUCK_ITEM_UNCLAIMED = "potluck_item_unclaimed", _("Potluck item unclaimed")
    POTLUCK_ITEM_DELETED = "potluck_item_deleted", _("Potluck item removed")

    # Questionnaire notifications
    QUESTIONNAIRE_SUBMITTED = (
        "questionnaire_submitted",
        _("Questionnaire submitted"),
    )  # (need 1 version for recipients, one for owners/staff)
    QUESTIONNAIRE_EVALUATION_RESULT = (
        "questionnaire_evaluation_result",
        _("Questionnaire result"),
    )  # (only needed for users)

    # Invitation notifications
    INVITATION_RECEIVED = "invitation_received", _("Invitation received")
    INVITATION_REQUEST_CREATED = (
        "invitation_request_created",
        _("Invitation request"),
    )  # User requests invitation to event

    # Membership notifications
    MEMBERSHIP_GRANTED = "membership_granted", _("Membership granted")
    MEMBERSHIP_PROMOTED = "membership_promoted", _("Membership promoted")
    MEMBERSHIP_REMOVED = "membership_removed", _("Membership removed")
    MEMBERSHIP_CARD_UPDATED = (
        "membership_card_updated",
        _("Membership card updated"),
    )  # Tier changed - wallet card needs re-adding
    MEMBERSHIP_REQUEST_CREATED = (
        "membership_request_created",
        _("Membership request"),
    )  # User requests to join organization
    MEMBERSHIP_REQUEST_APPROVED = "membership_request_approved", _("Membership request approved")
    MEMBERSHIP_REQUEST_REJECTED = "membership_request_rejected", _("Membership request rejected")
    # Staff heads-up: a register entry was linked to an account by verified-email match (#1319)
    STAFF_MEMBER_RECORD_AUTO_LINKED = "staff_member_record_auto_linked", _("Member record linked")

    # Organization notifications
    ORG_ANNOUNCEMENT = "org_announcement", _("Organization announcement")
    ORG_CONTACT_MESSAGE_RECEIVED = (
        "org_contact_message_received",
        _("Contact message received"),
    )  # Notify org admins of contact form submissions
    ORG_SETUP_NUDGE = (
        "org_setup_nudge",
        _("Organization setup reminder"),
    )  # Revel nudges a stalled org's owner (private profile, forgotten draft, ...)
    # White-label custom domain went live / failed: told to the organization owner (#1148)
    ORG_CUSTOM_DOMAIN_ACTIVE = "org_custom_domain_active", _("Custom domain active")
    ORG_CUSTOM_DOMAIN_FAILED = "org_custom_domain_failed", _("Custom domain failed")
    # Daily digest of invoices/credit notes a country policy made Revel skip (#1073, staff only)
    FISCAL_DOCUMENT_SKIPPED = "fiscal_document_skipped", _("Skipped fiscal documents")

    # Waitlist notifications
    WAITLIST_SPOT_AVAILABLE = "waitlist_spot_available", _("Waitlist spot available")

    # Whitelist notifications (blacklist verification flow)
    WHITELIST_REQUEST_CREATED = (
        "whitelist_request_created",
        _("Whitelist request"),
    )  # User requests whitelist - notify org admins
    WHITELIST_REQUEST_APPROVED = (
        "whitelist_request_approved",
        _("Whitelist request approved"),
    )  # Notify user when approved
    WHITELIST_REQUEST_REJECTED = (
        "whitelist_request_rejected",
        _("Whitelist request rejected"),
    )  # Notify user when rejected

    # Account notifications
    ACCOUNT_BANNED = "account_banned", _("Account banned")

    # System notifications
    SYSTEM_ANNOUNCEMENT = (
        "system_announcement",
        _("Platform announcement"),
    )  # Platform-wide announcements (privacy, ToC, etc.)

    # Follow notifications
    ORGANIZATION_FOLLOWED = (
        "organization_followed",
        _("New organization follower"),
    )  # Notify org admins when someone follows
    EVENT_SERIES_FOLLOWED = (
        "event_series_followed",
        _("New series follower"),
    )  # Notify org admins when someone follows a series
    NEW_EVENT_FROM_FOLLOWED_ORG = (
        "new_event_from_followed_org",
        _("New event from an organization you follow"),
    )  # Notify followers of new event
    NEW_EVENT_FROM_FOLLOWED_SERIES = (
        "new_event_from_followed_series",
        _("New event in a series you follow"),
    )  # Notify followers of new event in series

    # Series notifications
    SERIES_EVENTS_GENERATED = (
        "series_events_generated",
        _("Series events generated"),
    )  # Digest: N events generated for a series

    # Series pass notifications
    SERIES_PASS_PURCHASED = (
        "series_pass_purchased",
        _("Series pass purchased"),
    )  # Notify the holder when a pass activates
    SERIES_PASS_EXTENDED = (
        "series_pass_extended",
        _("Series pass extended"),
    )  # Notify holder when their pass gains newly-covered events
    SERIES_PASS_CANCELLED = (
        "series_pass_cancelled",
        _("Series pass cancelled"),
    )  # Notify the holder when a pass is cancelled
    # Org staff/owner alerts about series pass purchases/cancellations (#1127)
    STAFF_SERIES_PASS_PURCHASED = "staff_series_pass_purchased", _("Series pass sold")
    STAFF_SERIES_PASS_CANCELLED = "staff_series_pass_cancelled", _("Series pass cancelled by holder")

    # Subscription notifications
    SUBSCRIPTION_RENEWAL_SUCCEEDED = "subscription_renewal_succeeded", _("Subscription renewed")
    SUBSCRIPTION_PAYMENT_FAILED = "subscription_payment_failed", _("Subscription payment failed")
    SUBSCRIPTION_EXPIRED = "subscription_expired", _("Subscription expired")
    SUBSCRIPTION_CANCELLATION_CONFIRMED = (
        "subscription_cancellation_confirmed",
        _("Subscription cancellation confirmed"),
    )
    SUBSCRIPTION_RENEWAL_REMINDER = "subscription_renewal_reminder", _("Subscription renewal reminder")
    SUBSCRIPTION_PRICE_MIGRATION_NOTICE = "subscription_price_migration_notice", _("Subscription price change")
    SUBSCRIPTION_REVIVAL_CHECKOUT = (
        "subscription_revival_checkout",
        _("Subscription revival link"),
    )  # Staff revived: member gets the checkout link


# Transactional notification types are time-/money-sensitive (a ticket sale, a payment,
# a cancellation or a refund): they bypass the digest cadence (#506). This includes the staff
# ticket alerts, which are NOT mandatory (see MANDATORY_TYPES) but still shouldn't wait for a digest.
# The custom-domain owner mails are the same: opt-out-able, but the owner is waiting on them (#1148).
TRANSACTIONAL_TYPES: frozenset[str] = frozenset(
    {
        NotificationType.PAYMENT_CONFIRMATION,
        NotificationType.TICKET_CREATED,
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        NotificationType.STAFF_TICKET_CREATED,
        NotificationType.STAFF_TICKET_CANCELLED,
        NotificationType.STAFF_TICKET_REFUNDED,
        NotificationType.STAFF_PAYMENT_DISPUTED,
        NotificationType.ORG_CUSTOM_DOMAIN_ACTIVE,
        NotificationType.ORG_CUSTOM_DOMAIN_FAILED,
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
# Binds the person the transaction belongs to; the staff alert types are never in here.
# Defined explicitly (not derived from TRANSACTIONAL_TYPES, which holds opt-out-able staff types).
# SYSTEM_ANNOUNCEMENT is for ToS / privacy / platform-wide notices ONLY — never promotional content.
MANDATORY_TYPES: frozenset[str] = frozenset(
    {
        NotificationType.PAYMENT_CONFIRMATION,
        NotificationType.TICKET_CREATED,
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        NotificationType.SUBSCRIPTION_EXPIRED,
        NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
        NotificationType.ACCOUNT_BANNED,
        NotificationType.SYSTEM_ANNOUNCEMENT,
    }
)

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

# Attendee-facing transactional mail that ALSO goes out as the org sender, but only for white-label
# tenants (active custom domain + ``custom_domain_enabled``, #1155). Everyone else keeps the platform
# sender. Unlike ORG_SENDER_TYPES, unsubscribe headers and footer links stay on the platform.
# Only types whose recipient is the attendee, holder or subscriber belong here. Staff-facing types
# (STAFF_*, RSVP_* which go only to org staff/owners, MEMBERSHIP_REQUEST_CREATED, ...) are deliberately
# absent, as is Revel's own billing mail.
TENANT_ORG_SENDER_TYPES: frozenset[str] = frozenset(
    {
        NotificationType.TICKET_CREATED,
        NotificationType.TICKET_UPDATED,
        NotificationType.TICKET_CANCELLED,
        NotificationType.TICKET_REFUNDED,
        NotificationType.TICKET_CHECKED_IN,
        NotificationType.PAYMENT_CONFIRMATION,
        NotificationType.QUESTIONNAIRE_EVALUATION_RESULT,
        NotificationType.MEMBERSHIP_GRANTED,
        NotificationType.MEMBERSHIP_PROMOTED,
        NotificationType.MEMBERSHIP_REMOVED,
        NotificationType.MEMBERSHIP_CARD_UPDATED,
        NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        NotificationType.MEMBERSHIP_REQUEST_REJECTED,
        NotificationType.WAITLIST_SPOT_AVAILABLE,
        # Series pass mail to the holder (STAFF_SERIES_PASS_* are org-facing and stay on the platform sender).
        NotificationType.SERIES_PASS_PURCHASED,
        NotificationType.SERIES_PASS_EXTENDED,
        NotificationType.SERIES_PASS_CANCELLED,
        # Membership subscription mail to the subscriber. Revel bills the member on the org's behalf,
        # so none of these is platform billing of the organization.
        NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        NotificationType.SUBSCRIPTION_EXPIRED,
        NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        NotificationType.SUBSCRIPTION_RENEWAL_REMINDER,
        NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
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
