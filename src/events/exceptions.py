from django.core.exceptions import ValidationError as DjangoValidationError


class InvalidResourceStateError(DjangoValidationError):
    """Raised when an EventResource has an invalid combination of fields for its type."""


class AlreadyMemberError(Exception):
    """Raised when a user is already a member of an organization."""


class PendingMembershipRequestExistsError(Exception):
    """Raised when a pending membership request already exists for a user and organization."""


class TooManyItemsError(Exception):
    """Raised when too many PotluckItems are created."""


class OrganizationTokenStaffGrantForbidden(Exception):
    """Raised when a non-owner attempts to manage a staff-granting organization token."""


class OrganizationTokenGrantInvariantError(Exception):
    """Raised when an organization-token update would leave both grants disabled."""


class OrganizationTokenMembershipTierRequiredError(Exception):
    """Raised when an organization-token update would leave ``grants_membership=True`` with no ``membership_tier``."""


class RevenueReportCadenceOwnerOnlyError(Exception):
    """Raised when a non-owner attempts to change an organization's ``revenue_report_cadence``."""


class MembershipPolicyManageSubscriptionsOnlyError(Exception):
    """Raised when a staffer lacking ``manage_subscriptions`` attempts to change membership subscription policy."""


class TicketAlreadyCancelledError(Exception):
    """Raised when attempting to cancel/refund a ticket that is already in CANCELLED state."""


class StripeNotConnectedError(Exception):
    """Raised when an online-payment tier cannot be created/updated because the org has no Stripe Connect."""


class BillingInfoRequiredError(Exception):
    """Raised when an online-payment tier with platform fees lacks the organization's billing info."""


class InvalidStripeWebhookSignatureError(Exception):
    """Raised when no configured webhook secret verifies the Stripe-Signature header."""


class SessionTotalMismatchError(Exception):
    """Raised when a Stripe checkout session's total disagrees with ``sum(Payment.amount)``.

    A money invariant, not a warning: since a batch's Payment rows can carry
    different amounts (#739), "what Stripe charges" and "what our books record" are
    two independently-computed numbers. If they diverge, the books permanently
    disagree with the charge and the platform fee lands on the wrong total, so both
    the session-creation and the webhook-confirm path refuse to proceed.
    """


class DuplicateDiscountCodeError(Exception):
    """Raised when creating a discount code whose ``(organization, code)`` pair already exists."""


class InvalidPeriodError(Exception):
    """Raised when month and quarter period filters are combined."""


class SeriesPassCoverageError(Exception):
    """Raised when a series/event cannot carry a series pass (enable-time gate)."""


class SeriesPassNotPurchasableError(Exception):
    """Raised when a series pass cannot be purchased right now."""


class SeriesPassHasHoldersError(Exception):
    """Raised when deleting a SeriesPass or removing tier-link coverage would strand non-cancelled holders."""
