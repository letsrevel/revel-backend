"""Event service module - re-exports from specialized modules.

This module serves as a facade, re-exporting functions from:
- calendar_utils: Date range calculations and distance ordering
- tokens: Event token creation and claiming
- invitations: Invitation request management
- dietary: Dietary summary aggregation
- pronouns: Pronoun distribution aggregation
- duplication: Event duplication
"""

from events.service.calendar_utils import calculate_calendar_date_range, order_by_distance
from events.service.dietary import get_event_dietary_summary
from events.service.duplication import duplicate_event
from events.service.invitations import (
    approve_invitation_request,
    create_invitation_request,
    reject_invitation_request,
)
from events.service.pronouns import get_event_pronoun_distribution
from events.service.tokens import claim_invitation, create_event_token, get_event_token

__all__ = [
    # calendar_utils
    "calculate_calendar_date_range",
    "order_by_distance",
    # tokens
    "create_event_token",
    "get_event_token",
    "claim_invitation",
    # invitations
    "create_invitation_request",
    "approve_invitation_request",
    "reject_invitation_request",
    # dietary
    "get_event_dietary_summary",
    # pronouns
    "get_event_pronoun_distribution",
    # duplication
    "duplicate_event",
]
