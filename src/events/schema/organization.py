"""Organization-related schemas."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, EmailStr, Field, StringConstraints, model_validator

from accounts.schema import MemberUserSchema, MinimalRevelUserSchema, _BaseEmailJWTPayloadSchema
from common.schema import OneToOneFiftyString, StrippedString
from events import models
from events.models import (
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    PermissionsSchema,
)

from .mixins import (
    CityEditMixin,
    CityRetrieveMixin,
    SocialMediaSchemaEditMixin,
    SocialMediaSchemaRetrieveMixin,
    TaggableSchemaMixin,
)


class OrganizationCreateSchema(CityEditMixin):
    """Schema for creating a new organization."""

    name: OneToOneFiftyString
    description: StrippedString | None = None
    contact_email: EmailStr


class VerifyOrganizationContactEmailJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload schema for organization contact email verification."""

    type: t.Literal["org_contact_email_verification"] = "org_contact_email_verification"
    organization_id: UUID4


class OrganizationEditSchema(CityEditMixin, SocialMediaSchemaEditMixin):
    """Schema for editing an existing organization.

    Note: contact_email is excluded from this schema as it requires
    a separate verification flow via the update-contact-email endpoint.
    """

    description: StrippedString = ""
    visibility: Organization.Visibility
    accept_membership_requests: bool = False


class MinimalOrganizationSchema(Schema):
    """Lightweight organization schema for use in event lists - excludes city and tags to avoid N+1 queries."""

    id: UUID
    name: str
    slug: str
    description: str | None = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    is_stripe_connected: bool
    platform_fee_percent: Decimal | None = Field(None, ge=0, le=100)
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool


class OrganizationInListSchema(CityRetrieveMixin, TaggableSchemaMixin):
    """Schema for organization list endpoints - includes city and tags with proper prefetching."""

    id: UUID
    name: str
    slug: str
    description: str | None = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    is_stripe_connected: bool
    platform_fee_percent: Decimal | None = Field(None, ge=0, le=100)
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None


class OrganizationRetrieveSchema(CityRetrieveMixin, TaggableSchemaMixin, SocialMediaSchemaRetrieveMixin):
    id: UUID
    name: str
    slug: str
    description: str | None = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    is_stripe_connected: bool
    platform_fee_percent: Decimal | None = Field(None, ge=0, le=100)
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool


class OrganizationAdminDetailSchema(CityRetrieveMixin, TaggableSchemaMixin, SocialMediaSchemaRetrieveMixin):
    """Comprehensive organization schema for admin use with all fields including platform fees and Stripe details."""

    id: UUID
    name: str
    slug: str
    description: str | None = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    platform_fee_percent: Decimal
    platform_fee_fixed: Decimal
    is_stripe_connected: bool
    stripe_account_email: str | None = None
    stripe_account_id: str | None = None
    stripe_charges_enabled: bool
    stripe_details_submitted: bool
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool


class OrganizationPermissionsSchema(Schema):
    memberships: dict[str, "MinimalOrganizationMemberSchema"] = Field(default_factory=dict)
    organization_permissions: dict[str, PermissionsSchema | t.Literal["owner"]] | None = None


class OrganizationMembershipRequestCreateSchema(Schema):
    message: t.Annotated[str, StringConstraints(max_length=500, strip_whitespace=True)] | None = None


class OrganizationMembershipRequestRetrieve(ModelSchema):
    user: MinimalRevelUserSchema
    status: OrganizationMembershipRequest.Status

    class Meta:
        model = OrganizationMembershipRequest
        fields = ["id", "status", "message", "created_at", "user"]


class ApproveMembershipRequestSchema(Schema):
    """Schema for approving a membership request with required tier assignment."""

    tier_id: UUID4


class MembershipTierSchema(ModelSchema):
    description: str | None = None

    class Meta:
        model = models.MembershipTier
        fields = ["id", "name", "description"]


class MembershipTierCreateSchema(Schema):
    name: OneToOneFiftyString
    description: str | None = None


class MembershipTierUpdateSchema(Schema):
    name: OneToOneFiftyString | None = None
    description: str | None = None


class MinimalOrganizationMemberSchema(ModelSchema):
    """Organization member info without user details - used in permission contexts."""

    member_since: AwareDatetime = Field(alias="created_at")
    tier: MembershipTierSchema | None = None

    class Meta:
        model = models.OrganizationMember
        fields = ["created_at", "status", "tier"]


class OrganizationMemberSchema(Schema):
    user: MemberUserSchema
    member_since: AwareDatetime = Field(alias="created_at")
    status: OrganizationMember.MembershipStatus
    tier: MembershipTierSchema | None = None


class OrganizationMemberUpdateSchema(Schema):
    status: OrganizationMember.MembershipStatus | None = None
    tier_id: UUID4 | None = None


class OrganizationStaffSchema(Schema):
    user: MemberUserSchema
    staff_since: AwareDatetime = Field(alias="created_at")
    permissions: PermissionsSchema


class MemberAddSchema(Schema):
    tier_id: UUID


class StaffAddSchema(Schema):
    user_id: UUID
    permissions: PermissionsSchema | None = None


class OrganizationTokenSchema(ModelSchema):
    class Meta:
        model = models.OrganizationToken
        fields = "__all__"


class OrganizationTokenBaseSchema(Schema):
    name: OneToOneFiftyString | None = None
    max_uses: int = 1
    grants_membership: bool = True
    grants_staff_status: bool = False
    membership_tier_id: UUID4 | None = None


class OrganizationTokenCreateSchema(OrganizationTokenBaseSchema):
    duration: int = 24 * 60

    @model_validator(mode="after")
    def validate_membership_tier(self) -> "OrganizationTokenCreateSchema":
        """Validate that membership_tier_id is provided when grants_membership is True."""
        if self.grants_membership and not self.membership_tier_id:
            raise ValueError("membership_tier_id is required when grants_membership is True")
        return self


class OrganizationTokenUpdateSchema(OrganizationTokenBaseSchema):
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_membership_tier(self) -> "OrganizationTokenUpdateSchema":
        """Validate that membership_tier_id is provided when grants_membership is explicitly set to True."""
        # Only validate if grants_membership was explicitly set to True in the update payload
        if (
            "grants_membership" in self.__pydantic_fields_set__
            and self.grants_membership
            and not self.membership_tier_id
        ):
            raise ValueError("membership_tier_id is required when grants_membership is True")
        return self
