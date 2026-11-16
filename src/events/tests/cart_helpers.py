"""Cart payload helper shared by the authenticated and guest checkout tests."""

import typing as t


def to_cart(tier: t.Any, payload: dict[str, t.Any] | None = None, **group_fields: t.Any) -> dict[str, t.Any]:
    """Build a one-item cart for ``api:multi_tier_checkout`` / ``api:guest_multi_tier_checkout``.

    ``tier`` is a tier (or its id). Per-tier fields (``tickets``, ``pwyc_amount``,
    ``price_category_id``, ``accessible_required``; ``price_per_ticket`` is accepted as an
    alias of ``pwyc_amount``) go into the group, whether passed in ``payload`` or as keyword
    arguments. Everything else in ``payload`` (email, names, discount_code, ...) stays top level.
    """
    group_keys = {
        "tickets": "tickets",
        "pwyc_amount": "pwyc_amount",
        "price_per_ticket": "pwyc_amount",
        "price_category_id": "price_category_id",
        "accessible_required": "accessible_required",
    }
    group: dict[str, t.Any] = {"tier_id": str(getattr(tier, "pk", tier))}
    cart: dict[str, t.Any] = {}
    for key, value in {**(payload or {}), **group_fields}.items():
        if key in group_keys:
            group[group_keys[key]] = value
        else:
            cart[key] = value
    cart["items"] = [group]
    return cart
