"""Stripe Connect onboarding of an organization: the account, its onboarding link and its verification.

Split from ``stripe_service`` (#1473), which re-exports these names: callers and patches use
``stripe_service.<name>``.
"""

import typing as t

from pydantic import EmailStr

from common.models import SiteSettings
from common.service.stripe_connect_service import (
    create_account_link as _create_account_link,
)
from common.service.stripe_connect_service import (
    create_connect_account as _create_connect_account,
)
from common.service.stripe_connect_service import (
    sync_account_status,
)
from events.models import Event, Organization
from events.service.stripe_address_service import (
    apply_postal_address,
    postal_address_from_stripe,
)

if t.TYPE_CHECKING:
    from events.schema.organization import StripeReturnTo


def create_connect_account(organization: Organization, stripe_account_email: EmailStr) -> str:
    """Create a Stripe Connect Standard account for an organization."""
    return _create_connect_account(organization, stripe_account_email, account_type="standard")


def create_account_link(
    account_id: str, organization: Organization, return_to: "StripeReturnTo", event: Event | None = None
) -> str:
    """Create a one-time onboarding link for an organization's Stripe Connect account.

    ``return_to`` picks the admin page Stripe redirects back to; it is an allowlisted enum,
    so the destination is always one of our own pages. ``event`` is the organization's event
    whose editor ``return_to=event`` points at; the caller looks it up scoped to the organization.
    """
    from events.schema.organization import StripeReturnTo

    admin_base = f"{SiteSettings.get_solo().frontend_base_url}/org/{organization.slug}/admin"
    if return_to == StripeReturnTo.EVENT:
        if event is None:
            raise ValueError("return_to=event needs the event")
        page = f"{admin_base}/events/{event.id}/edit?tab=ticketing&"
    else:
        page = f"{admin_base}/{return_to.value}?"
    refresh_url = f"{page}stripe_refresh=true"
    return_url = f"{page}stripe_success=true"
    return _create_account_link(account_id, refresh_url, return_url)


def stripe_verify_account(organization: Organization) -> Organization:
    """Verify a Stripe Connect account.

    Also auto-fills billing_address and vat_country_code from Stripe account
    details if they are currently empty (fallback for orgs without a VAT ID).
    """
    account = sync_account_status(organization)
    if account is None:  # account not accessible: already flagged as not connected
        return organization

    # Organization-specific: auto-fill billing details from Stripe
    update_fields: list[str] = []

    if not organization.billing_address and account.get("company"):
        company = account["company"]
        address = company.get("address", {})
        parts = [
            address.get("line1", ""),
            address.get("line2", ""),
            address.get("postal_code", ""),
            address.get("city", ""),
            address.get("state", ""),
            address.get("country", ""),
        ]
        full_address = ", ".join(p for p in parts if p)
        if full_address:
            organization.billing_address = full_address
            update_fields.append("billing_address")

    update_fields += apply_postal_address(organization, postal_address_from_stripe(account))

    if not organization.vat_country_code and account.get("country"):
        organization.vat_country_code = account["country"]
        update_fields.append("vat_country_code")

    if update_fields:
        update_fields.append("updated_at")
        organization.save(update_fields=update_fields)

    return organization
