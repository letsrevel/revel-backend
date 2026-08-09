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


class InvalidZoneSelectionError(Exception):
    """Raised when the requested best-available zone (price category) is not selectable on this tier.

    Carries a buyer-facing message naming the tier's sellable zones — see
    :func:`events.service.seating.pick.resolve_requested_zone`, the single
    authority for the rule.
    """


class SeriesPassHasHoldersError(Exception):
    """Raised when deleting a SeriesPass or removing tier-link coverage would strand non-cancelled holders."""


class MembershipTierInUseError(Exception):
    """Raised when deleting a MembershipTier would drop protected membership applications or subscriptions."""


class SubscriptionActivationPendingError(Exception):
    """Raised when a subscribe attempt hits a PENDING row whose Checkout Session was already paid.

    Distinct from a plain duplicate-subscription refusal: the money is already
    taken and only the activation webhooks are outstanding, so the frontend
    must show a "confirming your subscription" state rather than an error. The
    handler renders a machine-readable ``code`` for exactly that reason — see
    :class:`events.schema.SubscriptionActivationPendingSchema`.
    """


class StripeRefundFailed(Exception):
    """Raised when a Stripe refund attempt fails after all internal retries.

    Attributes:
        detail: Human-readable description of the Stripe error.
    """

    def __init__(self, detail: str) -> None:
        """Initialize with the Stripe error detail string."""
        super().__init__(detail)
        self.detail = detail


class RefundInsufficientBalanceError(Exception):
    """Raised when Stripe declines a refund because the connected account balance is too low."""


class NothingToRefundError(Exception):
    """Raised when a refund is requested on a payment with no refundable amount remaining."""


class EventRefundsStartedError(Exception):
    """Raised on an un-cancel attempt after the bulk refund sweep already started."""
