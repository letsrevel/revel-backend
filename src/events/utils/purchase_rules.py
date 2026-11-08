"""Comparing ticket purchase rules (``TicketTier.PurchasableBy``) by who they admit (#1332).

Who each rule admits. Buyers: M = eligible members, I = invitees.

- ``public``              -> everyone
- ``members``             -> M
- ``invited``             -> I
- ``invited_and_members`` -> M or I (union, see ``tier_eligibility._check_purchasable_by``)
- ``non_members``         -> not M

A tier is *stricter* than a pass when some buyer the pass admits is not admitted by the tier.
``excluded_membership_tiers`` is deliberately NOT a stricter signal: on a ``non_members`` tier it
only narrows who is *excluded* (members of those tiers are kept out, see
``tier_eligibility.is_member_excluded``), so the tier still admits a superset of a ``non_members`` pass.

Values are plain strings mirroring ``TicketTier.PurchasableBy`` to keep this module free of model imports.
"""

# Allowed tier rules per pass rule (the pass admits a subset of the tier).
_ALLOWED_TIER_RULES: dict[str, frozenset[str]] = {
    "public": frozenset({"public"}),
    "members": frozenset({"public", "members", "invited_and_members"}),
    "invited": frozenset({"public", "invited", "invited_and_members"}),
    "invited_and_members": frozenset({"public", "invited_and_members"}),
    "non_members": frozenset({"public", "non_members"}),
}


def tier_rule_stricter_than_pass(tier_rule: str, pass_rule: str, *, tier_restricted_to_membership_tiers: bool) -> bool:
    """Return True when the tier admits fewer buyers than the pass, so the pass cannot cover it."""
    # A SeriesPass has no membership-tier restriction, so it can never carry an equivalent rule.
    if tier_restricted_to_membership_tiers:
        return True
    # Unknown pass rule fails closed.
    return tier_rule not in _ALLOWED_TIER_RULES.get(pass_rule, frozenset())
