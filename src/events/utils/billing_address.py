"""Pure helper reading a buyer's structured postal address from a billing snapshot."""

from common.postal_address import PostalAddress
from events.compliance.registry import resolve_country
from events.models.attendee_invoice import BuyerBillingSnapshot


def snapshot_buyer_postal_address(snapshot: BuyerBillingSnapshot) -> PostalAddress:
    """Build the buyer's :class:`PostalAddress` from a ``Payment.buyer_billing_snapshot``.

    Snapshots written before the structured fields existed read as blank. A blank country
    falls back to the VAT country / VAT ID prefix (``EL`` normalizes to ``GR``).

    Args:
        snapshot: The buyer billing snapshot.

    Returns:
        The buyer's postal address; fields are empty strings when unknown.
    """
    country = snapshot.get("billing_country_code", "") or resolve_country(
        snapshot.get("vat_country_code"), snapshot.get("vat_id"), None
    )
    return PostalAddress(
        line1=snapshot.get("billing_address_line1", ""),
        line2=snapshot.get("billing_address_line2", ""),
        postal_code=snapshot.get("billing_postal_code", ""),
        city=snapshot.get("billing_city", ""),
        subdivision=snapshot.get("billing_subdivision", ""),
        country_code=country,
    )
