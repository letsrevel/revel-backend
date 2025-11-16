"""Context schemas for notifications using TypedDict for type safety.

Each notification type has a corresponding context schema that defines
the structure of the context data. This ensures type safety and makes
it clear what data is required for each notification type.
"""

import typing as t

from notifications.enums import NotificationType


class BaseNotificationContext(t.TypedDict, total=False):
    """Base context for all notifications.

    All notification contexts inherit from this and may include the frontend_url.
    Using total=False means all fields are optional by default unless marked as Required.
    """

    # Frontend URL for deep linking
    frontend_url: str


# ===== Ticket Contexts =====


class TicketCreatedContext(BaseNotificationContext):
    """Context for TICKET_CREATED notification."""

    ticket_id: t.Required[str]
    ticket_reference: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    event_start: t.Required[str]  # ISO format
    event_location: t.Required[str]
    organization_id: t.Required[str]
    organization_name: t.Required[str]
    tier_name: t.Required[str]
    tier_price: t.Required[str]
    quantity: t.Required[int]
    total_price: t.Required[str]
    qr_code_url: str  # Optional


class TicketUpdatedContext(BaseNotificationContext):
    """Context for TICKET_UPDATED notification."""

    ticket_id: t.Required[str]
    ticket_reference: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    old_status: t.Required[str]
    new_status: t.Required[str]
    changed_by: str  # Optional
    reason: str  # Optional


class PaymentConfirmationContext(BaseNotificationContext):
    """Context for PAYMENT_CONFIRMATION notification."""

    ticket_id: t.Required[str]
    ticket_reference: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    event_start: t.Required[str]
    payment_amount: t.Required[str]
    payment_method: t.Required[str]
    receipt_url: str  # Optional


class TicketRefundedContext(BaseNotificationContext):
    """Context for TICKET_REFUNDED notification."""

    ticket_id: t.Required[str]
    ticket_reference: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    refund_amount: t.Required[str]
    refund_reason: str  # Optional


# ===== Event Contexts =====


class EventOpenContext(BaseNotificationContext):
    """Context for EVENT_OPEN notification."""

    event_id: t.Required[str]
    event_name: t.Required[str]
    event_description: t.Required[str]
    event_start: t.Required[str]
    event_end: t.Required[str]
    event_location: t.Required[str]
    organization_id: t.Required[str]
    organization_name: t.Required[str]
    rsvp_required: t.Required[bool]
    tickets_available: t.Required[bool]
    questionnaire_required: t.Required[bool]
    event_image_url: str  # Optional


class EventUpdatedContext(BaseNotificationContext):
    """Context for EVENT_UPDATED notification."""

    event_id: t.Required[str]
    event_name: t.Required[str]
    changed_fields: t.Required[list[str]]  # ["start", "location", etc.]
    old_values: t.Required[dict[str, str]]
    new_values: t.Required[dict[str, str]]


class EventReminderContext(BaseNotificationContext):
    """Context for EVENT_REMINDER notification."""

    event_id: t.Required[str]
    event_name: t.Required[str]
    event_start: t.Required[str]
    event_location: t.Required[str]
    days_until: t.Required[int]  # 14, 7, or 1
    rsvp_status: str  # Optional
    ticket_reference: str  # Optional


class EventCancelledContext(BaseNotificationContext):
    """Context for EVENT_CANCELLED notification."""

    event_id: t.Required[str]
    event_name: t.Required[str]
    event_start: t.Required[str]
    refund_available: t.Required[bool]
    cancellation_reason: str  # Optional


# ===== RSVP Contexts =====


class RSVPConfirmationContext(BaseNotificationContext):
    """Context for RSVP_CONFIRMATION notification."""

    rsvp_id: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    event_start: t.Required[str]
    event_location: t.Required[str]
    response: t.Required[str]  # YES, NO, MAYBE
    plus_ones: t.Required[int]
    user_name: t.Required[str]  # Name of person who RSVP'd (for staff notifications)
    user_email: t.Required[str]  # Email of person who RSVP'd (for staff notifications)


class RSVPUpdatedContext(BaseNotificationContext):
    """Context for RSVP_UPDATED notification."""

    rsvp_id: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    old_response: t.Required[str]
    new_response: t.Required[str]
    user_name: t.Required[str]  # Name of person who updated RSVP (for staff notifications)
    user_email: t.Required[str]  # Email of person who updated RSVP (for staff notifications)


class RSVPCancelledContext(BaseNotificationContext):
    """Context for RSVP_CANCELLED notification."""

    event_id: t.Required[str]
    event_name: t.Required[str]
    user_name: t.Required[str]


# ===== Potluck Contexts =====


class PotluckItemContext(BaseNotificationContext):
    """Context for potluck notifications."""

    potluck_item_id: t.Required[str]
    item_name: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    action: t.Required[str]  # "created", "updated", "claimed", "unclaimed"
    item_type: str  # Optional
    quantity: str  # Optional
    note: str  # Optional
    actor_name: str  # Optional - Who performed the action (created/claimed)
    is_organizer: bool  # Optional - Whether recipient is org staff/owner


# ===== Questionnaire Contexts =====


class QuestionnaireSubmittedContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_SUBMITTED (to staff)."""

    submission_id: t.Required[str]
    questionnaire_name: t.Required[str]
    submitter_email: t.Required[str]
    submitter_name: t.Required[str]
    organization_id: t.Required[str]
    organization_name: t.Required[str]
    event_id: str  # Optional
    event_name: str  # Optional


class QuestionnaireEvaluationContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_EVALUATION_RESULT (to user)."""

    submission_id: t.Required[str]
    questionnaire_name: t.Required[str]
    evaluation_status: t.Required[str]  # ACCEPTED, REJECTED, PENDING_PAYMENT
    organization_name: t.Required[str]
    evaluation_score: str  # Optional
    evaluation_comments: str  # Optional
    event_id: str  # Optional
    event_name: str  # Optional


# ===== Invitation Contexts =====


class InvitationReceivedContext(BaseNotificationContext):
    """Context for INVITATION_RECEIVED notification."""

    invitation_id: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    event_description: t.Required[str]
    event_start: t.Required[str]
    event_end: t.Required[str]
    event_location: t.Required[str]
    organization_id: t.Required[str]
    organization_name: t.Required[str]
    rsvp_required: t.Required[bool]
    tickets_required: t.Required[bool]
    personal_message: str  # Optional


class InvitationRequestCreatedContext(BaseNotificationContext):
    """Context for INVITATION_REQUEST_CREATED notification (to organizers)."""

    request_id: t.Required[str]
    event_id: t.Required[str]
    event_name: t.Required[str]
    requester_email: t.Required[str]
    requester_name: str  # Optional
    request_message: str  # Optional


# ===== Membership Contexts =====


class MembershipContext(BaseNotificationContext):
    """Context for membership notifications."""

    organization_id: t.Required[str]
    organization_name: t.Required[str]
    role: t.Required[str]  # "member", "staff", "owner"
    action: t.Required[str]  # "granted", "promoted", "removed"
    actioned_by_name: str  # Optional


class MembershipRequestCreatedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_REQUEST_CREATED notification (to organizers)."""

    request_id: t.Required[str]
    organization_id: t.Required[str]
    organization_name: t.Required[str]
    requester_id: t.Required[str]
    requester_name: t.Required[str]
    requester_email: t.Required[str]
    request_message: str  # Optional


# ===== System Contexts =====


class OrgAnnouncementContext(BaseNotificationContext):
    """Context for ORG_ANNOUNCEMENT notification."""

    organization_id: t.Required[str]
    organization_name: t.Required[str]
    announcement_title: t.Required[str]
    announcement_body: t.Required[str]
    posted_by_name: t.Required[str]


# Context type registry
NOTIFICATION_CONTEXT_SCHEMAS: dict[NotificationType, type[BaseNotificationContext]] = {
    NotificationType.TICKET_CREATED: TicketCreatedContext,
    NotificationType.TICKET_UPDATED: TicketUpdatedContext,
    NotificationType.TICKET_REFUNDED: TicketRefundedContext,
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
