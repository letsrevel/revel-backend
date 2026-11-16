"""Helpers shared by guest checkout tests."""

import typing as t


def to_cart(tier_id: t.Any, payload: dict[str, t.Any]) -> dict[str, t.Any]:
    """Wrap a single-tier guest payload into a one-item cart for ``api:guest_multi_tier_checkout``.

    Per-tier fields move into the group (``price_per_ticket`` becomes ``pwyc_amount``);
    everything else (email, names, discount_code, billing_info, ...) stays at the top level.
    """
    group_keys = {
        "tickets": "tickets",
        "price_per_ticket": "pwyc_amount",
        "price_category_id": "price_category_id",
        "accessible_required": "accessible_required",
    }
    group: dict[str, t.Any] = {"tier_id": str(tier_id)}
    cart: dict[str, t.Any] = {}
    for key, value in payload.items():
        if key in group_keys:
            group[group_keys[key]] = value
        else:
            cart[key] = value
    cart["items"] = [group]
    return cart
