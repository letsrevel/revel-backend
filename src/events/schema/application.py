"""Schemas for the membership application flow."""

import typing as t

from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, Field

from events.models import OrganizationMembershipRequest
from events.service.membership_manager import MembershipEligibility, MembershipNextStep
from events.service.membership_manager.enums import ReasonCode


class JoinEligibilityQuery(Schema):
    """Query params for GET /me/organizations/{slug}/join-eligibility."""

    tier_id: UUID4 | None = None
    plan_id: UUID4 | None = None


class ApplyRequestSchema(Schema):
    """Body for POST /me/organizations/{slug}/apply."""

    tier_id: UUID4 | None = None
    plan_id: UUID4 | None = None
    notes: t.Annotated[str, Field(max_length=2000)] = ""
    questionnaire_submission_id: UUID4 | None = None


class MembershipEligibilitySchema(Schema):
    """Wire schema mirroring MembershipEligibility for API responses."""

    allowed: bool
    organization_id: UUID4
    tier_id: UUID4 | None = None
    plan_id: UUID4 | None = None
    reason: str | None = None
    reason_code: ReasonCode | None = None
    next_step: MembershipNextStep | None = None
    questionnaire_id: UUID4 | None = None
    application_id: UUID4 | None = None
    retry_on: AwareDatetime | None = None

    @classmethod
    def from_eligibility(cls, eligibility: MembershipEligibility) -> "MembershipEligibilitySchema":
        """Build the wire schema from a MembershipEligibility domain object."""
        return cls.model_validate(eligibility.model_dump())


class MembershipApplicationSchema(ModelSchema):
    """Read schema for OrganizationMembershipRequest (member-facing)."""

    organization_id: UUID4
    tier_id: UUID4 | None = None
    plan_id: UUID4 | None = None
    subscription_id: UUID4 | None = None
    questionnaire_submission_id: UUID4 | None = None
    status: OrganizationMembershipRequest.Status

    class Meta:
        model = OrganizationMembershipRequest
        fields = ["id", "message", "created_at", "updated_at"]


class ApplyResponseSchema(Schema):
    """Response from POST /apply: the application + the latest eligibility verdict."""

    application: MembershipApplicationSchema
    eligibility: MembershipEligibilitySchema
