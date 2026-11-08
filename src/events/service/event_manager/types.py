"""Types and exceptions for the event eligibility system."""

import uuid

from pydantic import AwareDatetime, BaseModel

from .enums import NextStep, ReasonCode


class SeriesPassQuestionnaireEventSchema(BaseModel):
    """A missing questionnaire and the event whose questionnaire endpoint accepts its submission."""

    questionnaire_id: uuid.UUID
    event_id: uuid.UUID


class EventUserEligibility(BaseModel):
    """Result of an eligibility check for a user on an event."""

    allowed: bool
    event_id: uuid.UUID
    reason: str | None = None  # human-readable, translated
    reason_code: ReasonCode | None = None  # stable machine-readable identifier
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
    # Event-scoped per-user ticket budget, mirroring UserEventStatus.event_remaining so
    # both shapes of GET /my-status carry it (#901). Only the my-status path populates it;
    # everywhere else an eligibility result is returned it stays None.
    event_remaining: int | None = None
    # Series-pass checkout only (#1175): one entry per missing questionnaire, naming the event whose
    # questionnaire endpoint accepts it (same shape as the pass quote). ``event_id`` stays the first group's.
    questionnaire_events: list[SeriesPassQuestionnaireEventSchema] | None = None


class UserIsIneligibleError(Exception):
    """Exception raised when a user is not eligible for an event action."""

    def __init__(self, message: str, eligibility: EventUserEligibility) -> None:
        """Initialize the exception with eligibility details."""
        super().__init__(message)
        self.eligibility = eligibility
