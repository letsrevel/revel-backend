"""Structured billing address helpers for Stripe Connect accounts."""

import typing as t

import structlog
from django.db import models

from common.postal_address import PostalAddress
from events.models import Organization

logger = structlog.get_logger(__name__)


def postal_address_from_stripe(account: t.Mapping[str, t.Any]) -> PostalAddress:
    """Extract a structured address from a Stripe account: ``company`` first, then ``individual``.

    Missing or ``None`` objects and keys count as empty; values are stripped and the country uppercased.
    The first source with a street line wins, so a country-only ``company`` address does not shadow a
    full ``individual`` one; without any street line, the first non-empty source is returned.
    """
    candidates: list[PostalAddress] = []
    for key in ("company", "individual"):
        address = (account.get(key) or {}).get("address") or {}
        clean = {
            k: (address.get(k) or "").strip() for k in ("line1", "line2", "postal_code", "city", "state", "country")
        }
        if any(clean[k] for k in ("line1", "postal_code", "city", "country")):
            candidates.append(
                PostalAddress(
                    line1=clean["line1"],
                    line2=clean["line2"],
                    postal_code=clean["postal_code"],
                    city=clean["city"],
                    subdivision=clean["state"],
                    country_code=clean["country"].upper(),
                )
            )
    return next((c for c in candidates if c.line1), candidates[0] if candidates else PostalAddress())


def apply_postal_address(org: Organization, address: PostalAddress) -> list[str]:
    """Set the structured ``billing_*`` fields from ``address`` when the org has none yet.

    Nothing is applied when the org already has any structured value (hand-entered data wins) or
    when a value exceeds its column length: a truncated address would be wrong data, so the
    structured fill is skipped entirely.

    Returns:
        The changed field names (for ``update_fields``); empty when nothing was applied.
    """
    if address.is_empty or not org.billing_postal_address().is_empty:
        return []
    values = address.to_fields("billing_")
    for field, value in values.items():
        max_length = t.cast("models.Field[t.Any, t.Any]", Organization._meta.get_field(field)).max_length
        if max_length is not None and len(value) > max_length:
            logger.warning("stripe_address_value_too_long", org_id=str(org.id))
            return []
    for field, value in values.items():
        setattr(org, field, value)
    return list(values)
