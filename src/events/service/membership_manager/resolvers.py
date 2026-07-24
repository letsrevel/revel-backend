"""Resolve policy values that may be defined at the tier level or fall back to the org."""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from events.models import MembershipTier, Organization, OrganizationQuestionnaire


def resolve_membership_questionnaire(
    organization: "Organization",
    tier: "MembershipTier | None",
) -> "OrganizationQuestionnaire | None":
    """Return the applicable membership questionnaire.

    Tier-level override wins over org default. NULL on both → no questionnaire required.
    """
    if tier is not None and tier.membership_questionnaire_id:
        return tier.membership_questionnaire
    return organization.default_membership_questionnaire


def resolve_requires_membership_approval(
    organization: "Organization",
    tier: "MembershipTier | None",
) -> bool:
    """Return whether manual staff approval is required for this (org, tier).

    Tier-level override (when not NULL) wins; otherwise the org default applies.
    """
    if tier is not None and tier.requires_membership_approval is not None:
        return tier.requires_membership_approval
    return organization.default_requires_membership_approval
