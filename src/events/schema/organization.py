"""Organization-related schemas."""

import re
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
    LogoCoverArtThumbnailMixin,
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


class OrganizationBillingInfoSchema(Schema):
    """Read-only schema for organization billing info and VAT settings."""

    billing_name: str
    vat_id: str
    vat_country_code: str
    vat_rate: Decimal
    vat_id_validated: bool
    vat_id_validated_at: AwareDatetime | None = None
    billing_address: str
    billing_email: str


class OrganizationBillingInfoUpdateSchema(Schema):
    """Schema for updating organization billing info (excludes vat_id, use PUT /vat-id).

    When vat_country_code is provided, it must be a valid EU member state.
    """

    billing_name: t.Annotated[str, StringConstraints(strip_whitespace=True)] = ""
    vat_country_code: (
        t.Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2)] | None
    ) = None
    vat_rate: Decimal | None = Field(None, ge=0, le=100)
    billing_address: str = ""
    billing_email: str = ""

    @model_validator(mode="after")
    def validate_country_code(self) -> "OrganizationBillingInfoUpdateSchema":
        """Validate country code is an EU member state."""
        from common.constants import EU_MEMBER_STATES

        if self.vat_country_code is not None and self.vat_country_code not in EU_MEMBER_STATES:
            raise ValueError(f"Country code must be a valid EU member state. Got: {self.vat_country_code}")
        return self


class VATIdUpdateSchema(Schema):
    """Schema for setting/updating the organization's VAT ID."""

    vat_id: t.Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True)]

    @model_validator(mode="after")
    def validate_vat_id_format(self) -> "VATIdUpdateSchema":
        """Validate VAT ID format and country prefix."""
        from common.constants import EU_MEMBER_STATES, VAT_ID_PATTERN

        if not re.match(VAT_ID_PATTERN, self.vat_id):
            raise ValueError(
                "VAT ID must start with a 2-letter country code followed by 2-13 alphanumeric characters "
                "(e.g., IT12345678901, DE123456789)."
            )
        country_prefix = self.vat_id[:2]
        if country_prefix not in EU_MEMBER_STATES:
            raise ValueError(f"VAT ID country prefix must be a valid EU member state. Got: {country_prefix}")
        return self


class MinimalOrganizationSchema(LogoCoverArtThumbnailMixin):
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


class OrganizationInListSchema(CityRetrieveMixin, TaggableSchemaMixin, LogoCoverArtThumbnailMixin):
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


class OrganizationRetrieveSchema(
    CityRetrieveMixin, TaggableSchemaMixin, SocialMediaSchemaRetrieveMixin, LogoCoverArtThumbnailMixin
):
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


class OrganizationAdminDetailSchema(
    CityRetrieveMixin, TaggableSchemaMixin, SocialMediaSchemaRetrieveMixin, LogoCoverArtThumbnailMixin
):
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
    # VAT / billing
    billing_name: str
    vat_id: str
    vat_country_code: str
    vat_rate: Decimal
    vat_id_validated: bool
    vat_id_validated_at: AwareDatetime | None = None
    billing_address: str
    billing_email: str


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
        fields = [
            "id",
            "name",
            "issuer",
            "organization",
            "expires_at",
            "uses",
            "max_uses",
            "grants_membership",
            "grants_staff_status",
            "membership_tier",
            "created_at",
        ]


class OrganizationTokenBaseSchema(Schema):
    name: OneToOneFiftyString | None = None
    max_uses: int = 1
    grants_membership: bool = True
    grants_staff_status: bool = False
    membership_tier_id: UUID4 | None = None


class OrganizationTokenCreateSchema(OrganizationTokenBaseSchema):
    duration: int = 24 * 60

    @model_validator(mode="after")
    def validate_token_grants(self) -> "OrganizationTokenCreateSchema":
        """Validate token grant configuration.

        Ensures:
        - At least one of grants_membership or grants_staff_status is True
        - membership_tier_id is provided when grants_membership is True
        """
        if not self.grants_membership and not self.grants_staff_status:
            raise ValueError("At least one of grants_membership or grants_staff_status must be True")
        if self.grants_membership and not self.membership_tier_id:
            raise ValueError("membership_tier_id is required when grants_membership is True")
        return self


class OrganizationTokenUpdateSchema(OrganizationTokenBaseSchema):
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_token_grants(self) -> "OrganizationTokenUpdateSchema":
        """Validate token grant configuration for updates.

        Ensures:
        - If both grants are explicitly set, at least one must be True
        - membership_tier_id is provided when grants_membership is explicitly set to True
        """
        both_explicitly_set = (
            "grants_membership" in self.model_fields_set and "grants_staff_status" in self.model_fields_set
        )
        if both_explicitly_set and not self.grants_membership and not self.grants_staff_status:
            raise ValueError("At least one of grants_membership or grants_staff_status must be True")
        # Only validate tier if grants_membership was explicitly set to True in the update payload
        if "grants_membership" in self.model_fields_set and self.grants_membership and not self.membership_tier_id:
            raise ValueError("membership_tier_id is required when grants_membership is True")
        return self
