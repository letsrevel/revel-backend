"""Generic Stripe Connect helpers shared across bounded contexts.

Functions here operate on any model that inherits ``StripeConnectMixin``
(e.g. ``Organization``, ``RevelUser``).  Domain-specific logic (billing
autofill, URL construction) stays in each app's own service module.
"""

import typing as t

import stripe
import structlog
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from pydantic import EmailStr

from common.models import StripeConnectMixin

logger = structlog.get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def get_account_details(account_id: str) -> stripe.Account:
    """Retrieve details for a connected Stripe account."""
    return t.cast(stripe.Account, stripe.Account.retrieve(account_id))


def create_connect_account(
    connectable: StripeConnectMixin,
    stripe_account_email: EmailStr,
    account_type: str = "standard",
) -> str:
    """Create a Stripe Connect account and persist the ID/email on *connectable*.

    Args:
        connectable: Any model with the ``StripeConnectMixin`` fields.
        stripe_account_email: Email address for the Stripe account.
        account_type: Stripe account type (``"standard"`` or ``"express"``).

    Returns:
        The new Stripe account ID.
    """
    account = stripe.Account.create(type=account_type, email=stripe_account_email)
    connectable.stripe_account_id = account.id
    connectable.stripe_account_email = stripe_account_email
    connectable.save(update_fields=["stripe_account_id", "stripe_account_email"])
    return t.cast(str, account.id)


def create_account_link(account_id: str, refresh_url: str, return_url: str) -> str:
    """Create a one-time onboarding link for a Stripe Connect account.

    Args:
        account_id: The Stripe account ID to onboard.
        refresh_url: URL Stripe redirects to if the link expires.
        return_url: URL Stripe redirects to after onboarding completes.

    Returns:
        The onboarding URL.
    """
    account_link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return t.cast(str, account_link.url)


def sync_account_status(connectable: StripeConnectMixin) -> None:
    """Fetch the latest status from Stripe and update *connectable* in place.

    Only updates ``stripe_charges_enabled`` and ``stripe_details_submitted``.
    Callers may perform additional domain-specific updates afterwards.

    Raises:
        HttpError 400: If the connectable has no ``stripe_account_id``.
    """
    if connectable.stripe_account_id is None:
        raise HttpError(400, str(_("You must connect your Stripe account first.")))
    account = get_account_details(connectable.stripe_account_id)
    connectable.stripe_charges_enabled = account.charges_enabled
    connectable.stripe_details_submitted = account.details_submitted
    update_fields = ["stripe_charges_enabled", "stripe_details_submitted"]
    if hasattr(connectable, "updated_at"):
        update_fields.append("updated_at")
    connectable.save(update_fields=update_fields)
