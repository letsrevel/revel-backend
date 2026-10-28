"""Structured billing address helpers for Stripe Connect accounts."""

import typing as t

from common.postal_address import PostalAddress
from events.models import Organization

STRUCTURED_ADDRESS_FIELDS: tuple[str, ...] = (
    "billing_address_line1",
    "billing_address_line2",
    "billing_postal_code",
    "billing_city",
    "billing_subdivision",
    "billing_country_code",
)


def postal_address_from_stripe(account: t.Mapping[str, t.Any]) -> PostalAddress:
    """Extract a structured address from a Stripe account: ``company`` first, then ``individual``.

    Missing or ``None`` objects and keys count as empty; the country is uppercased.
    """
    for key in ("company", "individual"):
        address = (account.get(key) or {}).get("address") or {}
        if any(address.get(k) for k in ("line1", "postal_code", "city", "country")):
            return PostalAddress(
                line1=address.get("line1") or "",
                line2=address.get("line2") or "",
                postal_code=address.get("postal_code") or "",
                city=address.get("city") or "",
                subdivision=address.get("state") or "",
                country_code=(address.get("country") or "").upper(),
            )
    return PostalAddress()


def apply_postal_address(org: Organization, address: PostalAddress) -> list[str]:
    """Set the structured ``billing_*`` fields from ``address`` when the org has no line 1 yet.

    Returns:
        The changed field names (for ``update_fields``); empty when nothing was applied.
    """
    if org.billing_address_line1 or address.is_empty:
        return []
    values = {
        "billing_address_line1": address.line1,
        "billing_address_line2": address.line2,
        "billing_postal_code": address.postal_code,
        "billing_city": address.city,
        "billing_subdivision": address.subdivision,
        "billing_country_code": address.country_code,
    }
    for field, value in values.items():
        setattr(org, field, value)
    return list(values)
