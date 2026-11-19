"""Context schemas for membership notifications (re-exported by ``notifications.context_schemas``)."""

import typing as t

from notifications.context_base import BaseNotificationContext


class MembershipContext(BaseNotificationContext):
    """Context for membership notifications."""

    organization_id: str
    organization_name: str
    role: str  # "member", "staff", "owner"
    action: str  # "granted", "promoted", "removed"
    actioned_by_name: t.NotRequired[str]


class MembershipCardUpdatedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_CARD_UPDATED notification (tier changed)."""

    organization_id: str
    organization_name: str
    member_id: str
    tier_name: str


class MembershipRequestCreatedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_REQUEST_CREATED notification (to organizers)."""

    request_id: str
    organization_id: str
    organization_name: str
    requester_id: str
    requester_name: str
    requester_email: str
    request_message: t.NotRequired[str]
    tier_name: t.NotRequired[str | None]
    plan_name: t.NotRequired[str | None]
    plan_payment_method: t.NotRequired[str | None]


class MembershipOfflineJoinedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_OFFLINE_JOINED notification (to org staff: a direct offline join awaits payment)."""

    subscription_id: str
    organization_id: str
    organization_name: str
    member_id: str
    member_name: str
    member_email: str
    tier_name: str
    plan_name: str


class StaffMemberRecordAutoLinkedContext(BaseNotificationContext):
    """Context for STAFF_MEMBER_RECORD_AUTO_LINKED notification (to org staff/owners)."""

    organization_id: str
    organization_name: str
    record_id: str
    record_full_name: str
    member_display_name: str
    member_email: str


class MembershipRequestApprovedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_REQUEST_APPROVED notification (to the applicant).

    A request decision is not a role change, so it does not carry the ``role``/``action``
    keys of ``MembershipContext`` — only the organization the applicant was admitted to.
    """

    organization_id: str
    organization_name: str


class MembershipRequestRejectedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_REQUEST_REJECTED notification (to the applicant)."""

    organization_id: str
    organization_name: str


class MembershipPaymentRequestedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_PAYMENT_REQUESTED notification (offline plan: pay the organization directly)."""

    organization_id: str
    organization_name: str
    organization_slug: str
    organization_contact_url: str
    plan_name: str
    tier_name: str
    amount: str
    period_label: str
    payment_instructions: str  # "" when neither plan nor organization defines any
    memberships_url: str
    application_accepted: t.NotRequired[bool]  # lead with "<org> accepted your application."
    payment_window_days: t.NotRequired[
        int
    ]  # APPLICATION_PAYMENT_WINDOW_DAYS; absent on older stored rows (templates default to 30)


class MembershipInvitationReceivedContext(BaseNotificationContext):
    """Context for MEMBERSHIP_INVITATION_RECEIVED notification (to the invited account)."""

    organization_id: str
    organization_name: str
    tier_name: str
    expires_at: str  # display string in the organization's timezone (format_organization_datetime)
    accept_url: str
