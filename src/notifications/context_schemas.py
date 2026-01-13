"""Context schemas for notifications using TypedDict for type safety.

Each notification type has a corresponding context schema that defines
the structure of the context data. This ensures type safety and makes
it clear what data is required for each notification type.
"""

import typing as t

from notifications.enums import NotificationType


class BaseNotificationContext(t.TypedDict):
    """Base context for all notifications."""

    # Frontend URL for deep linking
    frontend_url: t.NotRequired[str]


# ===== Ticket Contexts =====


class TicketCreatedContext(BaseNotificationContext):
    """Context for TICKET_CREATED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start: str  # ISO format
    event_start_formatted: str  # User-friendly format
    event_location: str
    event_url: str
    organization_id: str
    organization_name: str
    tier_name: str
    tier_price: str
    ticket_status: str
    quantity: int
    total_price: str
    qr_code_url: t.NotRequired[str]
    manual_payment_instructions: t.NotRequired[str]
    payment_method: str


class TicketUpdatedContext(BaseNotificationContext):
    """Context for TICKET_UPDATED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start_formatted: str
    event_location: str
    event_url: str
    tier_name: str
    ticket_status: str
    old_status: str
    new_status: str
    changed_by: t.NotRequired[str]
    reason: t.NotRequired[str]


class TicketCancelledContext(BaseNotificationContext):
    """Context for TICKET_CANCELLED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start_formatted: str
    event_location: str
    event_url: str
    organization_id: str
    organization_name: str
    tier_name: str
    ticket_status: str
    old_status: str
    new_status: str
    # Additional fields for staff notifications
    ticket_holder_name: t.NotRequired[str]
    ticket_holder_email: t.NotRequired[str]


class PaymentConfirmationContext(BaseNotificationContext):
    """Context for PAYMENT_CONFIRMATION notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start: str
    payment_amount: str
    payment_method: str
    receipt_url: t.NotRequired[str]


class TicketRefundedContext(BaseNotificationContext):
    """Context for TICKET_REFUNDED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    refund_amount: str
    refund_reason: t.NotRequired[str]


class TicketCheckedInContext(BaseNotificationContext):
    """Context for TICKET_CHECKED_IN notification."""

    ticket_id: str
    event_id: str
    event_name: str
    event_start_formatted: str
    event_location: str
    event_url: str
    checked_in_at: str


# ===== Event Contexts =====


class EventOpenContext(BaseNotificationContext):
    """Context for EVENT_OPEN notification."""

    event_id: str
    event_name: str
    event_description: str
    event_start: str
    event_end: str
    event_location: str
    event_image_url: t.NotRequired[str]
    organization_id: str
    organization_name: str
    rsvp_required: bool
    tickets_available: bool
    questionnaire_required: bool


class EventUpdatedContext(BaseNotificationContext):
    """Context for EVENT_UPDATED notification."""

    event_id: str
    event_name: str
    event_start_formatted: str
    event_end_formatted: t.NotRequired[str]
    event_location: str
    event_url: str
    changed_fields: list[str]  # ["start", "location", etc.]
    old_values: dict[str, str]
    new_values: dict[str, str]
    changes_summary: t.NotRequired[str]
    update_message: t.NotRequired[str]


class EventReminderContext(BaseNotificationContext):
    """Context for EVENT_REMINDER notification."""

    event_id: str
    event_name: str
    event_start: str  # ISO format
    event_start_formatted: str  # User-friendly format
    event_end_formatted: t.NotRequired[str]  # User-friendly format
    event_location: str
    event_url: str
    days_until: int  # 14, 7, or 1
    reminder_message: t.NotRequired[str]
    # Ticket info (if user has a ticket)
    ticket_id: t.NotRequired[str]
    tier_name: t.NotRequired[str]
    # RSVP info (if user has RSVP)
    rsvp_status: t.NotRequired[str]


class EventCancelledContext(BaseNotificationContext):
    """Context for EVENT_CANCELLED notification."""

    event_id: str
    event_name: str
    event_start: str
    event_start_formatted: str
    event_location: str
    cancellation_reason: t.NotRequired[str]
    refund_available: bool
    refund_info: t.NotRequired[str]
    alternative_event_url: t.NotRequired[str]


# ===== RSVP Contexts =====


class RSVPConfirmationContext(BaseNotificationContext):
    """Context for RSVP_CONFIRMATION notification."""

    rsvp_id: str
    event_id: str
    event_name: str
    event_start: str
    event_location: str
    response: str  # YES, NO, MAYBE
    user_name: str  # Name of person who RSVP'd (for staff notifications)
    user_email: str  # Email of person who RSVP'd (for staff notifications)


class RSVPUpdatedContext(BaseNotificationContext):
    """Context for RSVP_UPDATED notification."""

    rsvp_id: str
    event_id: str
    event_name: str
    old_response: str
    new_response: str
    user_name: str  # Name of person who updated RSVP (for staff notifications)
    user_email: str  # Email of person who updated RSVP (for staff notifications)


class RSVPCancelledContext(BaseNotificationContext):
    """Context for RSVP_CANCELLED notification."""

    event_id: str
    event_name: str
    user_name: str


# ===== Potluck Contexts =====


class PotluckItemContext(BaseNotificationContext):
    """Context for potluck notifications."""

    potluck_item_id: str
    item_name: str
    event_id: str
    event_name: str
    action: str  # "created", "updated", "claimed", "unclaimed"
    item_type: t.NotRequired[str]
    quantity: t.NotRequired[str]
    note: t.NotRequired[str]
    actor_name: t.NotRequired[str]  # Who performed the action (created/claimed)
    is_organizer: t.NotRequired[bool]  # Whether recipient is org staff/owner


# ===== Questionnaire Contexts =====


class QuestionnaireSubmittedContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_SUBMITTED (to staff)."""

    submission_id: str
    questionnaire_name: str
    submitter_email: str
    submitter_name: str
    organization_id: str
    organization_name: str
    submission_url: str  # URL to view the submission in admin
    event_id: t.NotRequired[str]
    event_name: t.NotRequired[str]


class QuestionnaireEvaluationContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_EVALUATION_RESULT (to user)."""

    submission_id: str
    questionnaire_name: str
    evaluation_status: str  # APPROVED, REJECTED
    organization_name: str
    evaluation_score: t.NotRequired[str]
    evaluation_comments: t.NotRequired[str]
    event_id: t.NotRequired[str]
    event_name: t.NotRequired[str]
    event_url: t.NotRequired[str]  # URL to the event page (if questionnaire is event-linked)


# ===== Invitation Contexts =====


class InvitationReceivedContext(BaseNotificationContext):
    """Context for INVITATION_RECEIVED notification."""

    invitation_id: str
    event_id: str
    event_name: str
    event_description: str
    event_start: str
    event_end: str
    event_location: str
    organization_id: str
    organization_name: str
    personal_message: t.NotRequired[str]
    rsvp_required: bool
    tickets_required: bool


class InvitationRequestCreatedContext(BaseNotificationContext):
    """Context for INVITATION_REQUEST_CREATED notification (to organizers)."""

    request_id: str
    event_id: str
    event_name: str
    requester_email: str
    requester_name: t.NotRequired[str]
    request_message: t.NotRequired[str]


# ===== Membership Contexts =====


class MembershipContext(BaseNotificationContext):
    """Context for membership notifications."""

    organization_id: str
    organization_name: str
    role: str  # "member", "staff", "owner"
    action: str  # "granted", "promoted", "removed"
    actioned_by_name: t.NotRequired[str]


class MembershipRequestCreatedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_REQUEST_CREATED notification (to organizers)."""

    request_id: str
    organization_id: str
    organization_name: str
    requester_id: str
    requester_name: str
    requester_email: str
    request_message: t.NotRequired[str]


# ===== System Contexts =====


class OrgAnnouncementContext(BaseNotificationContext):
    """Context for ORG_ANNOUNCEMENT notification."""

    organization_id: str
    organization_name: str
    announcement_title: str
    announcement_body: str
    posted_by_name: str


# ===== Waitlist Contexts =====


class WaitlistSpotAvailableContext(BaseNotificationContext):
    """Context for WAITLIST_SPOT_AVAILABLE notification."""

    event_id: str
    event_name: str
    event_start: str  # ISO format
    event_start_formatted: str  # User-friendly format
    event_location: str
    event_url: str
    organization_id: str
    organization_name: str
    spots_available: int


# ===== Whitelist Contexts =====


class WhitelistRequestCreatedContext(BaseNotificationContext):
    """Context for WHITELIST_REQUEST_CREATED notification (to organizers)."""

    request_id: str
    organization_id: str
    organization_name: str
    requester_id: str
    requester_name: str
    requester_email: str
    request_message: t.NotRequired[str]
    matched_entries_count: int


class WhitelistRequestApprovedContext(BaseNotificationContext):
    """Context for WHITELIST_REQUEST_APPROVED notification (to user)."""

    organization_id: str
    organization_name: str


class WhitelistRequestRejectedContext(BaseNotificationContext):
    """Context for WHITELIST_REQUEST_REJECTED notification (to user)."""

    organization_id: str
    organization_name: str


# Context type registry
NOTIFICATION_CONTEXT_SCHEMAS: dict[NotificationType, type[BaseNotificationContext]] = {
    NotificationType.TICKET_CREATED: TicketCreatedContext,
    NotificationType.TICKET_UPDATED: TicketUpdatedContext,
    NotificationType.TICKET_CANCELLED: TicketCancelledContext,
    NotificationType.TICKET_REFUNDED: TicketRefundedContext,
    NotificationType.TICKET_CHECKED_IN: TicketCheckedInContext,
    NotificationType.PAYMENT_CONFIRMATION: PaymentConfirmationContext,
    NotificationType.EVENT_OPEN: EventOpenContext,
    NotificationType.EVENT_UPDATED: EventUpdatedContext,
    NotificationType.EVENT_REMINDER: EventReminderContext,
    NotificationType.EVENT_CANCELLED: EventCancelledContext,
    NotificationType.RSVP_CONFIRMATION: RSVPConfirmationContext,
    NotificationType.RSVP_UPDATED: RSVPUpdatedContext,
    NotificationType.RSVP_CANCELLED: RSVPCancelledContext,
    NotificationType.POTLUCK_ITEM_CREATED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_UPDATED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_CLAIMED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_UNCLAIMED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_DELETED: PotluckItemContext,
    NotificationType.QUESTIONNAIRE_SUBMITTED: QuestionnaireSubmittedContext,
    NotificationType.QUESTIONNAIRE_EVALUATION_RESULT: QuestionnaireEvaluationContext,
    NotificationType.INVITATION_RECEIVED: InvitationReceivedContext,
    NotificationType.INVITATION_REQUEST_CREATED: InvitationRequestCreatedContext,
    NotificationType.MEMBERSHIP_GRANTED: MembershipContext,
    NotificationType.MEMBERSHIP_PROMOTED: MembershipContext,
    NotificationType.MEMBERSHIP_REMOVED: MembershipContext,
    NotificationType.MEMBERSHIP_REQUEST_CREATED: MembershipRequestCreatedContext,
    NotificationType.MEMBERSHIP_REQUEST_APPROVED: MembershipContext,
    NotificationType.MEMBERSHIP_REQUEST_REJECTED: MembershipContext,
    NotificationType.ORG_ANNOUNCEMENT: OrgAnnouncementContext,
    NotificationType.WAITLIST_SPOT_AVAILABLE: WaitlistSpotAvailableContext,
    NotificationType.WHITELIST_REQUEST_CREATED: WhitelistRequestCreatedContext,
    NotificationType.WHITELIST_REQUEST_APPROVED: WhitelistRequestApprovedContext,
    NotificationType.WHITELIST_REQUEST_REJECTED: WhitelistRequestRejectedContext,
}


def validate_notification_context(notification_type: NotificationType, context: dict[str, t.Any]) -> None:
    """Validate that context matches expected schema for notification type.

    Args:
        notification_type: Type of notification
        context: Context dict to validate

    Raises:
        ValueError: If context is invalid or notification type has no schema
    """
    schema = NOTIFICATION_CONTEXT_SCHEMAS.get(notification_type)
    if schema is None:
        raise ValueError(f"No schema defined for notification type: {notification_type}")

    # TypedDict validation happens at type-check time with mypy
    # At runtime, we do basic key validation for required fields
    required_keys: set[str] = getattr(schema, "__required_keys__", set())
    missing_keys = required_keys - context.keys()

    if missing_keys:
        raise ValueError(f"Missing required context keys for {notification_type}: {missing_keys}")
