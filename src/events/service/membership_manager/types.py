"""Types and exceptions for the membership eligibility system."""

import datetime
import uuid

from pydantic import BaseModel

from .enums import MembershipNextStep, ReasonCode


class MembershipEligibility(BaseModel):
    """Result of a membership-eligibility check for a (user, organization[, tier[, plan]]) tuple."""

    allowed: bool
    organization_id: uuid.UUID
    tier_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    reason: str | None = None
    reason_code: ReasonCode | None = None  # stable machine-readable identifier
    next_step: MembershipNextStep | None = None
    questionnaire_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    retry_on: datetime.datetime | None = None


class MembershipApplicationIneligibleError(Exception):
    """Raised when an action requires the user to be eligible and they aren't."""

    def __init__(self, message: str, eligibility: MembershipEligibility) -> None:
        """Initialize with a human-readable message and the failing eligibility result."""
        super().__init__(message)
        self.eligibility = eligibility
