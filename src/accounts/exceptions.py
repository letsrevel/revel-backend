"""Accounts-specific exceptions mapped to HTTP status codes.

The exception → status mapping lives in :mod:`accounts.exception_handlers`,
registered on the global API from :meth:`accounts.apps.AccountsConfig.ready`.
"""

import typing as t

if t.TYPE_CHECKING:  # pragma: no cover - typing only
    from accounts.service.referral_cleanup import ReferralForfeitureSummary


class ReferralPayoutInFlightError(Exception):
    """Raised when a user still has ``PENDING`` referral payouts.

    Deleting the user now would race the disbursement task (a Stripe transfer may
    already be in flight), so the deletion task retries with backoff instead.
    Stale ``PENDING`` rows are reclaimed after an hour by ``process_referral_payouts``.
    """


class ReferralForfeitureConfirmationRequiredError(Exception):
    """Raised when account deletion would forfeit unpaid referral earnings.

    Carries the assessed :class:`~accounts.service.referral_cleanup.ReferralForfeitureSummary`
    so the exception handler can render a machine-readable 409 payload for the
    frontend's consequence screen. The user re-submits with ``force=true``.
    """

    def __init__(self, summary: "ReferralForfeitureSummary") -> None:
        """Store the assessed forfeiture summary for the exception handler."""
        self.summary = summary
        super().__init__("Referral forfeiture confirmation required")


OIDCErrorCode = t.Literal["state", "provider", "denied", "unverified_email", "no_email", "banned", "inactive"]


class OIDCLoginError(Exception):
    """Raised inside the OIDC callback flow. Carries a short machine code, never PII.

    The callback controller turns it into a redirect to the frontend login page
    (``?error=oidc_<code>``); the handler mapping below is a safety net only.
    """

    def __init__(self, code: OIDCErrorCode) -> None:
        """Store the machine-readable error code."""
        super().__init__(code)
        self.code: OIDCErrorCode = code
