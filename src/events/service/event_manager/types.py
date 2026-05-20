"""Types and exceptions for the event eligibility system."""

import uuid

from pydantic import AwareDatetime, BaseModel

from .enums import NextStep


class EventUserEligibility(BaseModel):
    """Result of an eligibility check for a user on an event."""

    allowed: bool
    event_id: uuid.UUID
    reason: str | None = None  # we don't use the enum here because we want translation
    next_step: NextStep | None = None
    questionnaires_missing: list[uuid.UUID] | None = None
    questionnaires_pending_review: list[uuid.UUID] | None = None
    questionnaires_failed: list[uuid.UUID] | None = None
    retry_on: AwareDatetime | None = None
    missing_profile_fields: list[str] | None = None
    pending_offers_count: int | None = None
    next_batch_at: AwareDatetime | None = None
    waitlist_position: int | None = None
    active_offer_expires_at: AwareDatetime | None = None


class UserIsIneligibleError(Exception):
    """Exception raised when a user is not eligible for an event action."""

    def __init__(self, message: str, eligibility: EventUserEligibility) -> None:
        """Initialize the exception with eligibility details."""
        super().__init__(message)
        self.eligibility = eligibility
