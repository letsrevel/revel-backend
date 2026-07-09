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
from events.service.event_update_service import (
    SLUG_ALREADY_EXISTS_MESSAGE,
    SlugAlreadyExistsError,
    create_event,
    update_event,
    update_event_schedule,
    update_slug,
    update_status,
)
from events.service.invitations import (
    approve_invitation_request,
    create_invitation_request,
    reject_invitation_request,
)
from events.service.pronouns import get_event_pronoun_distribution
from events.service.tokens import (
    claim_invitation,
    create_event_token,
    get_event_token,
    get_token_rejection_reason,
    update_event_token,
)

__all__ = [
    "SLUG_ALREADY_EXISTS_MESSAGE",
    "SlugAlreadyExistsError",
    "approve_invitation_request",
    "calculate_calendar_date_range",
    "claim_invitation",
    "create_event",
    "create_event_token",
    "create_invitation_request",
    "duplicate_event",
    "get_event_dietary_summary",
    "get_event_pronoun_distribution",
    "get_event_token",
    "get_token_rejection_reason",
    "order_by_distance",
    "reject_invitation_request",
    "update_event",
    "update_event_schedule",
    "update_event_token",
    "update_slug",
    "update_status",
]
