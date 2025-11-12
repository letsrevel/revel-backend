"""Context schemas for notifications using TypedDict for type safety.

Each notification type has a corresponding context schema that defines
the structure of the context data. This ensures type safety and makes
it clear what data is required for each notification type.
"""

from typing import Any, NotRequired, TypedDict

from notifications.enums import NotificationType


class BaseNotificationContext(TypedDict):
    """Base context for all notifications."""

    # Frontend URL for deep linking
    frontend_url: NotRequired[str]


# ===== Ticket Contexts =====


class TicketCreatedContext(BaseNotificationContext):
    """Context for TICKET_CREATED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start: str  # ISO format
    event_location: str
    organization_id: str
    organization_name: str
    tier_name: str
    tier_price: str
    quantity: int
    total_price: str
    qr_code_url: NotRequired[str]


class TicketUpdatedContext(BaseNotificationContext):
    """Context for TICKET_UPDATED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    old_status: str
    new_status: str
    changed_by: NotRequired[str]
    reason: NotRequired[str]


class PaymentConfirmationContext(BaseNotificationContext):
    """Context for PAYMENT_CONFIRMATION notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    event_start: str
    payment_amount: str
    payment_method: str
    receipt_url: NotRequired[str]


class TicketRefundedContext(BaseNotificationContext):
    """Context for TICKET_REFUNDED notification."""

    ticket_id: str
    ticket_reference: str
    event_id: str
    event_name: str
    refund_amount: str
    refund_reason: NotRequired[str]


# ===== Event Contexts =====


class EventOpenContext(BaseNotificationContext):
    """Context for EVENT_OPEN notification."""

    event_id: str
    event_name: str
    event_description: str
    event_start: str
    event_end: str
    event_location: str
    event_image_url: NotRequired[str]
    organization_id: str
    organization_name: str
    rsvp_required: bool
    tickets_available: bool
    questionnaire_required: bool


class EventCreatedContext(BaseNotificationContext):
    """Context for EVENT_CREATED notification."""

    event_id: str
    event_name: str
    event_description: str
    event_start: str
    event_end: str
    event_location: str
    organization_id: str
    organization_name: str


class EventUpdatedContext(BaseNotificationContext):
    """Context for EVENT_UPDATED notification."""

    event_id: str
    event_name: str
    changed_fields: list[str]  # ["start", "location", etc.]
    old_values: dict[str, str]
    new_values: dict[str, str]


class EventReminderContext(BaseNotificationContext):
    """Context for EVENT_REMINDER notification."""

    event_id: str
    event_name: str
    event_start: str
    event_location: str
    days_until: int  # 14, 7, or 1
    rsvp_status: NotRequired[str]
    ticket_reference: NotRequired[str]


class EventCancelledContext(BaseNotificationContext):
    """Context for EVENT_CANCELLED notification."""

    event_id: str
    event_name: str
    event_start: str
    cancellation_reason: NotRequired[str]
    refund_available: bool


# ===== RSVP Contexts =====


class RSVPConfirmationContext(BaseNotificationContext):
    """Context for RSVP_CONFIRMATION notification."""

    rsvp_id: str
    event_id: str
    event_name: str
    event_start: str
    event_location: str
    response: str  # YES, NO, MAYBE
    plus_ones: int


class RSVPUpdatedContext(BaseNotificationContext):
    """Context for RSVP_UPDATED notification."""

    rsvp_id: str
    event_id: str
    event_name: str
    old_response: str
    new_response: str


# ===== Potluck Contexts =====


class PotluckItemContext(BaseNotificationContext):
    """Context for potluck notifications."""

    potluck_item_id: str
    item_name: str
    event_id: str
    event_name: str
    action: str  # "created", "updated", "claimed", "unclaimed"
    changed_by_username: NotRequired[str]
    assigned_to_username: NotRequired[str]


# ===== Questionnaire Contexts =====


class QuestionnaireSubmittedContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_SUBMITTED (to staff)."""

    submission_id: str
    questionnaire_name: str
    submitter_email: str
    submitter_name: str
    organization_id: str
    organization_name: str
    event_id: NotRequired[str]
    event_name: NotRequired[str]


class QuestionnaireEvaluationContext(BaseNotificationContext):
    """Context for QUESTIONNAIRE_EVALUATION_RESULT (to user)."""

    submission_id: str
    questionnaire_name: str
    evaluation_status: str  # ACCEPTED, REJECTED, PENDING_PAYMENT
    event_id: NotRequired[str]
    event_name: NotRequired[str]
    feedback: NotRequired[str]


# ===== Invitation Contexts =====


class InvitationReceivedContext(BaseNotificationContext):
    """Context for INVITATION_RECEIVED notification."""

    invitation_id: str
    event_id: str
    event_name: str
    event_start: str
    invited_by_name: str
    personal_message: NotRequired[str]


class InvitationClaimedContext(BaseNotificationContext):
    """Context for INVITATION_CLAIMED notification (to organizers)."""

    invitation_id: str
    event_id: str
    event_name: str
    claimed_by_email: str
    claimed_by_name: str


class InvitationRevokedContext(BaseNotificationContext):
    """Context for INVITATION_REVOKED notification."""

    invitation_id: str
    event_id: str
    event_name: str
    revoked_by_name: NotRequired[str]


# ===== Membership Contexts =====


class MembershipContext(BaseNotificationContext):
    """Context for membership notifications."""

    organization_id: str
    organization_name: str
    role: str  # "member", "staff", "owner"
    action: str  # "granted", "promoted", "removed"
    actioned_by_name: NotRequired[str]


# ===== System Contexts =====


class MalwareDetectedContext(BaseNotificationContext):
    """Context for MALWARE_DETECTED notification."""

    file_name: str
    findings: dict[str, Any]
    quarantine_id: NotRequired[str]


class OrgAnnouncementContext(BaseNotificationContext):
    """Context for ORG_ANNOUNCEMENT notification."""

    organization_id: str
    organization_name: str
    announcement_title: str
    announcement_body: str
    posted_by_name: str


# Context type registry
NOTIFICATION_CONTEXT_SCHEMAS: dict[NotificationType, type[BaseNotificationContext]] = {
    NotificationType.TICKET_CREATED: TicketCreatedContext,
    NotificationType.TICKET_UPDATED: TicketUpdatedContext,
    NotificationType.TICKET_REFUNDED: TicketRefundedContext,
    NotificationType.PAYMENT_CONFIRMATION: PaymentConfirmationContext,
    NotificationType.EVENT_OPEN: EventOpenContext,
    NotificationType.EVENT_CREATED: EventCreatedContext,
    NotificationType.EVENT_UPDATED: EventUpdatedContext,
    NotificationType.EVENT_REMINDER: EventReminderContext,
    NotificationType.EVENT_CANCELLED: EventCancelledContext,
    NotificationType.RSVP_CONFIRMATION: RSVPConfirmationContext,
    NotificationType.RSVP_UPDATED: RSVPUpdatedContext,
    NotificationType.POTLUCK_ITEM_CREATED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_UPDATED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_CLAIMED: PotluckItemContext,
    NotificationType.POTLUCK_ITEM_UNCLAIMED: PotluckItemContext,
    NotificationType.QUESTIONNAIRE_SUBMITTED: QuestionnaireSubmittedContext,
    NotificationType.QUESTIONNAIRE_EVALUATION_RESULT: QuestionnaireEvaluationContext,
    NotificationType.INVITATION_RECEIVED: InvitationReceivedContext,
    NotificationType.INVITATION_CLAIMED: InvitationClaimedContext,
    NotificationType.INVITATION_REVOKED: InvitationRevokedContext,
    NotificationType.MEMBERSHIP_GRANTED: MembershipContext,
    NotificationType.MEMBERSHIP_PROMOTED: MembershipContext,
    NotificationType.MEMBERSHIP_REMOVED: MembershipContext,
    NotificationType.MEMBERSHIP_REQUEST_APPROVED: MembershipContext,
    NotificationType.MEMBERSHIP_REQUEST_REJECTED: MembershipContext,
    NotificationType.MALWARE_DETECTED: MalwareDetectedContext,
    NotificationType.ORG_ANNOUNCEMENT: OrgAnnouncementContext,
}


def validate_notification_context(notification_type: NotificationType, context: dict[str, Any]) -> None:
    """Validate that context matches expected schema for notification type.

    Args:
        notification_type: Type of notification
        context: Context dict to validate

    Raises:
        ValueError: If context is invalid or notification type has no schema
    """
    schema = NOTIFICATION_CONTEXT_SCHEMAS.get(notification_type)
    if not schema:
        raise ValueError(f"No schema defined for notification type: {notification_type}")

    # TypedDict validation happens at type-check time with mypy
    # At runtime, we do basic key validation for required fields
    required_keys: set[str] = getattr(schema, "__required_keys__", set())
    missing_keys = required_keys - context.keys()

    if missing_keys:
        raise ValueError(f"Missing required context keys for {notification_type}: {missing_keys}")
