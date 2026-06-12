"""Membership eligibility and application pipeline.

Parallels events.service.event_manager: composable gates that determine whether
a user can join an organization (free or paid) at a target tier, returning a
structured MembershipEligibility with reason/next_step/supporting IDs.
"""

from .enums import MembershipNextStep, Reasons
from .resolvers import resolve_membership_questionnaire, resolve_requires_membership_approval
from .service import MembershipEligibilityService, advance_application, apply_for_membership
from .types import MembershipApplicationIneligibleError, MembershipEligibility

__all__ = [
    "MembershipApplicationIneligibleError",
    "MembershipEligibility",
    "MembershipEligibilityService",
    "MembershipNextStep",
    "Reasons",
    "advance_application",
    "apply_for_membership",
    "resolve_membership_questionnaire",
    "resolve_requires_membership_approval",
]
