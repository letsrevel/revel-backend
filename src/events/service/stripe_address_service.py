"""Structured billing address helpers for Stripe Connect accounts."""

import typing as t

from common.postal_address import PostalAddress
from events.models import Organization


def postal_address_from_stripe(account: t.Mapping[str, t.Any]) -> PostalAddress:
    """Extract a structured address from a Stripe account: ``company`` first, then ``individual``.

    Missing or ``None`` objects and keys count as empty; values are stripped and the country uppercased.
    """
    for key in ("company", "individual"):
        address = (account.get(key) or {}).get("address") or {}
        clean = {
            k: (address.get(k) or "").strip() for k in ("line1", "line2", "postal_code", "city", "state", "country")
        }
        if any(clean[k] for k in ("line1", "postal_code", "city", "country")):
            return PostalAddress(
                line1=clean["line1"],
                line2=clean["line2"],
                postal_code=clean["postal_code"],
                city=clean["city"],
                subdivision=clean["state"],
                country_code=clean["country"].upper(),
            )
    return PostalAddress()


def apply_postal_address(org: Organization, address: PostalAddress) -> list[str]:
    """Set the structured ``billing_*`` fields from ``address`` when the org has no line 1 yet.

    Returns:
        The changed field names (for ``update_fields``); empty when nothing was applied.
    """
    if org.billing_address_line1 or address.is_empty:
        return []
    values = address.to_fields("billing_")
    for field, value in values.items():
        setattr(org, field, value)
    return list(values)
