"""Event eligibility and management package.

This package provides the eligibility checking system and event management
for RSVP and ticket operations.
"""

from .enums import NextStep, ReasonCode, Reasons
from .manager import EventManager
from .service import EligibilityService
from .types import EventUserEligibility, UserIsIneligibleError

__all__ = [
    "NextStep",
    "ReasonCode",
    "Reasons",
    "EventUserEligibility",
    "UserIsIneligibleError",
    "EligibilityService",
    "EventManager",
]
