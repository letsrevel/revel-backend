import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    Discriminator,
    EmailStr,
    Field,
    StringConstraints,
    Tag,
    field_serializer,
    field_validator,
    model_validator,
)

from accounts.models import DietaryRestriction, RevelUser
from accounts.schema import MemberUserSchema, MinimalRevelUserSchema, _BaseEmailJWTPayloadSchema
from common.schema import OneToOneFiftyString, OneToSixtyFourString, StrippedString
from events import models
from events.models import (
    AdditionalResource,
    Event,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    Payment,
    PermissionsSchema,
    Ticket,
    TicketTier,
)
from geo.models import City
from geo.schema import CitySchema
from questionnaires import schema as questionnaires_schema
from questionnaires.models import Questionnaire


def ensure_url(value: str) -> str:
    """Mock function for now."""
    if not value.startswith("http"):
        return "http://localhost:8000" + value
    return value


class CityEditMixin(Schema):
    city_id: int | None = None
    address: StrippedString | None = None

    @field_validator("city_id", mode="after")
    @classmethod
    def validate_city_exists(cls, v: int | None) -> int | None:
        """Validate that city exists."""
        if v is not None and not City.objects.filter(pk=v).exists():
            raise ValueError(f"City with ID {v} does not exist.")
        return v


class CityRetrieveMixin(Schema):
    city: CitySchema | None = None
    address: str | None = None


class TaggableSchemaMixin(Schema):
    tags: list[str] = Field(default_factory=list)

    @staticmethod
    def resolve_tags(obj: models.Event) -> list[str]:
        """Flattify tags."""
        if hasattr(obj, "prefetched_tagassignments"):
            return [ta.tag.name for ta in obj.prefetched_tagassignments]
        return [ta.tag.name for ta in obj.tags.all()]


class OrganizationEditSchema(CityEditMixin):
    description: StrippedString = ""
    visibility: Organization.Visibility
    accept_membership_requests: bool = False
    contact_email: EmailStr | None = None


class MinimalOrganizationSchema(Schema):
    """Lightweight organization schema for use in event lists - excludes city and tags to avoid N+1 queries."""

    id: UUID
    name: str
    slug: str
    description: str | None = ""
    description_html: str = ""
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
    description_html: str = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    is_stripe_connected: bool
    platform_fee_percent: Decimal | None = Field(None, ge=0, le=100)
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool
    updated_at: datetime | None = None
    created_at: datetime | None = None


class OrganizationRetrieveSchema(CityRetrieveMixin, TaggableSchemaMixin):
    id: UUID
    name: str
    slug: str
    description: str | None = ""
    description_html: str = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    is_stripe_connected: bool
    platform_fee_percent: Decimal | None = Field(None, ge=0, le=100)
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool


class OrganizationAdminDetailSchema(CityRetrieveMixin, TaggableSchemaMixin):
    """Comprehensive organization schema for admin use with all fields including platform fees and Stripe details."""

    id: UUID
    name: str
    slug: str
    description: str | None = ""
    description_html: str = ""
    logo: str | None = None
    cover_art: str | None = None
    visibility: Organization.Visibility
    platform_fee_percent: Decimal
    platform_fee_fixed: Decimal
    is_stripe_connected: bool
    stripe_account_id: str | None = None
    stripe_charges_enabled: bool
    stripe_details_submitted: bool
    accept_membership_requests: bool
    contact_email: str | None = None
    contact_email_verified: bool


class MinimalEventSeriesSchema(Schema):
    """Lightweight event series schema for use in event lists - excludes tags and uses minimal organization."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    description_html: str = ""
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesInListSchema(TaggableSchemaMixin):
    """Schema for event series list endpoints - includes tags with proper prefetching."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    description_html: str = ""
    slug: str
    logo: str | None = None
    cover_art: str | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None


class EventSeriesRetrieveSchema(TaggableSchemaMixin):
    """Full event series schema for detail views - uses minimal organization to prevent cascading queries."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    description_html: str = ""
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesEditSchema(Schema):
    name: OneToOneFiftyString
    description: StrippedString | None = None


class EventEditSchema(CityEditMixin):
    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    event_type: Event.EventType | None = None
    status: Event.EventStatus = Event.EventStatus.DRAFT
    visibility: Event.Visibility | None = None
    invitation_message: StrippedString | None = Field(None, description="Invitation message")
    max_attendees: int = 0
    waitlist_open: bool = False
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    rsvp_before: AwareDatetime | None = Field(None, description="RSVP deadline for events that do not require tickets")
    check_in_starts_at: AwareDatetime | None = Field(None, description="When check-in opens for this event")
    check_in_ends_at: AwareDatetime | None = Field(None, description="When check-in closes for this event")
    event_series_id: UUID | None = None
    free_for_members: bool = False
    free_for_staff: bool = True
    # requires_ticket: bool = False
    potluck_open: bool = False
    accept_invitation_requests: bool = False
    can_attend_without_login: bool = False


class EventCreateSchema(EventEditSchema):
    name: OneToOneFiftyString
    start: AwareDatetime


class EventBaseSchema(CityRetrieveMixin, TaggableSchemaMixin):
    id: UUID
    event_type: Event.EventType
    visibility: Event.Visibility
    organization: MinimalOrganizationSchema
    status: Event.EventStatus
    event_series: MinimalEventSeriesSchema | None = None
    name: str
    slug: str
    description: str | None = None
    # description_html: str = ""
    invitation_message: str | None = None
    # invitation_message_html: str = ""
    max_attendees: int = 0
    waitlist_open: bool | None = None
    start: datetime
    end: datetime
    rsvp_before: datetime | None = None
    logo: str | None = None
    cover_art: str | None = None
    free_for_members: bool
    free_for_staff: bool
    requires_ticket: bool
    potluck_open: bool
    attendee_count: int
    accept_invitation_requests: bool
    can_attend_without_login: bool
    updated_at: datetime | None = None
    created_at: datetime | None = None


class EventInListSchema(EventBaseSchema):
    pass


class EventDetailSchema(EventBaseSchema):
    description_html: str = ""
    invitation_message_html: str = ""


class EventRSVPSchema(ModelSchema):
    event_id: UUID
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["status"]


# RSVP Admin Schemas


class RSVPDetailSchema(ModelSchema):
    """Schema for RSVP details in admin views."""

    id: UUID
    event_id: UUID
    user: MinimalRevelUserSchema
    status: EventRSVP.RsvpStatus
    created_at: datetime
    updated_at: datetime

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "created_at", "updated_at"]


class RSVPCreateSchema(Schema):
    """Schema for creating an RSVP on behalf of a user."""

    user_id: UUID
    status: EventRSVP.RsvpStatus


class RSVPUpdateSchema(Schema):
    """Schema for updating an RSVP."""

    status: EventRSVP.RsvpStatus


# Waitlist Admin Schemas


class WaitlistEntrySchema(ModelSchema):
    """Schema for waitlist entry details in admin views."""

    id: UUID
    event_id: UUID
    user: MinimalRevelUserSchema
    created_at: datetime
    updated_at: datetime

    class Meta:
        model = models.EventWaitList
        fields = ["id", "created_at", "updated_at"]


class UserRSVPSchema(ModelSchema):
    """Schema for user's own RSVPs with event details."""

    event: "MinimalEventSchema"
    status: EventRSVP.RsvpStatus

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "created_at", "updated_at"]


class TicketTierSchema(ModelSchema):
    id: UUID
    event_id: UUID
    price: Decimal
    currency: str
    total_available: int | None
    description_html: str = ""
    restricted_to_membership_tiers: list["MembershipTierSchema"] | None = None

    class Meta:
        model = TicketTier
        fields = [
            "id",
            "name",
            "description",
            "price",
            "price_type",
            "pwyc_min",
            "pwyc_max",
            "currency",
            "sales_start_at",
            "sales_end_at",
            "purchasable_by",
            "payment_method",
            "manual_payment_instructions",
        ]


Currencies = t.Literal[
    "EUR",  # Euro
    "USD",  # US Dollar
    "GBP",  # British Pound Sterling
    "JPY",  # Japanese Yen
    "AUD",  # Australian Dollar
    "CAD",  # Canadian Dollar
    "CHF",  # Swiss Franc
    "CNY",  # Chinese Yuan Renminbi
    "HKD",  # Hong Kong Dollar
    "NZD",  # New Zealand Dollar
    "SEK",  # Swedish Krona
    "KRW",  # South Korean Won
    "SGD",  # Singapore Dollar
    "NOK",  # Norwegian Krone
    "MXN",  # Mexican Peso
    "INR",  # Indian Rupee
    "RUB",  # Russian Ruble
    "ZAR",  # South African Rand
    "TRY",  # Turkish Lira
    "BRL",  # Brazilian Real
    "TWD",  # New Taiwan Dollar
    "DKK",  # Danish Krone
    "PLN",  # Polish Zloty
    "THB",  # Thai Baht
    "IDR",  # Indonesian Rupiah
    "HUF",  # Hungarian Forint
    "CZK",  # Czech Koruna
    "ILS",  # Israeli Shekel
    "AED",  # UAE Dirham
    "SAR",  # Saudi Riyal
    "MYR",  # Malaysian Ringgit
    "PHP",  # Philippine Peso
    "CLP",  # Chilean Peso
    "COP",  # Colombian Peso
    "PKR",  # Pakistani Rupee
    "EGP",  # Egyptian Pound
    "NGN",  # Nigerian Naira
    "VND",  # Vietnamese Dong
    "BDT",  # Bangladeshi Taka
    "ARS",  # Argentine Peso
    "QAR",  # Qatari Riyal
    "KWD",  # Kuwaiti Dinar
    "BHD",  # Bahraini Dinar
    "OMR",  # Omani Rial
    "MAD",  # Moroccan Dirham
    "KES",  # Kenyan Shilling
    "UAH",  # Ukrainian Hryvnia
    "RON",  # Romanian Leu
    "BGN",  # Bulgarian Lev
    "HRK",  # Croatian Kuna (still valid for legacy data)
    "ISK",  # Icelandic Krona
]


class PaymentSchema(ModelSchema):
    """Public representation of a Payment record."""

    status: Payment.PaymentStatus
    currency: Currencies
    stripe_dashboard_url: str

    class Meta:
        model = Payment
        exclude = ["user", "ticket", "raw_response"]


class EventTicketSchema(ModelSchema):
    event_id: UUID | None
    tier: TicketTierSchema | None = None
    status: Ticket.TicketStatus

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "checked_in_at"]


class AdminTicketSchema(ModelSchema):
    """Schema for pending tickets in admin interface."""

    user: MemberUserSchema
    tier: TicketTierSchema
    payment: PaymentSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at"]


class UserTicketSchema(ModelSchema):
    """Schema for user's own tickets with event details."""

    event: "MinimalEventSchema"
    tier: TicketTierSchema
    status: Ticket.TicketStatus

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "checked_in_at"]


class CheckInRequestSchema(Schema):
    """Schema for ticket check-in requests."""

    ticket_id: UUID


class CheckInResponseSchema(ModelSchema):
    """Schema for ticket check-in response."""

    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "checked_in_at", "tier"]


class OrganizationPermissionsSchema(Schema):
    memberships: dict[str, "MinimalOrganizationMemberSchema"] = Field(default_factory=dict)
    organization_permissions: dict[str, PermissionsSchema | t.Literal["owner"]] | None = None


EventUserStatusSchema = EventRSVPSchema | EventTicketSchema


class InvitationBaseSchema(Schema):
    waives_questionnaire: bool = False
    waives_purchase: bool = False
    overrides_max_attendees: bool = False
    waives_membership_required: bool = False
    waives_rsvp_deadline: bool = False
    custom_message: str | None = None


class InvitationSchema(InvitationBaseSchema):
    event: EventInListSchema
    tier: TicketTierSchema | None = None
    user_id: UUID


class DirectInvitationCreateSchema(InvitationBaseSchema):
    """Schema for creating direct invitations to events."""

    emails: list[EmailStr] = Field(..., min_length=1, description="List of email addresses to invite")
    tier_id: UUID | None = Field(None, description="Ticket tier to assign to invitations")
    send_notification: bool = Field(True, description="Whether to send notification emails")


class DirectInvitationResponseSchema(Schema):
    """Response schema for direct invitation creation."""

    created_invitations: int = Field(..., description="Number of EventInvitation objects created")
    pending_invitations: int = Field(..., description="Number of PendingEventInvitation objects created")
    total_invited: int = Field(..., description="Total number of users invited")


class EventInvitationListSchema(Schema):
    """Schema for listing EventInvitation objects."""

    id: UUID
    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    custom_message: str | None = None
    created_at: datetime


class MyEventInvitationSchema(Schema):
    """Schema for listing user's own EventInvitation objects with event details."""

    id: UUID
    event: "EventInListSchema"
    tier: TicketTierSchema | None = None
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    custom_message: str | None = None
    created_at: datetime


class PendingEventInvitationListSchema(Schema):
    """Schema for listing PendingEventInvitation objects."""

    id: UUID
    email: str
    tier: TicketTierSchema | None = None
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    custom_message: str | None = None
    created_at: datetime


class CombinedInvitationListSchema(Schema):
    """Schema combining both EventInvitation and PendingEventInvitation for listing."""

    id: UUID
    type: str = Field(..., description="'registered' for EventInvitation, 'pending' for PendingEventInvitation")
    user: MinimalRevelUserSchema | None = Field(None, description="User for registered invitations")
    email: str | None = Field(None, description="Email for pending invitations")
    tier: TicketTierSchema | None = None
    waives_questionnaire: bool
    waives_purchase: bool
    overrides_max_attendees: bool
    waives_membership_required: bool
    waives_rsvp_deadline: bool
    custom_message: str | None = None
    created_at: datetime


# Questionnaires


class MinimalEventSchema(Schema):
    id: UUID
    slug: str
    name: str
    start: datetime
    end: datetime
    logo: str | None = None
    cover_art: str | None = None


class BaseOrganizationQuestionnaireSchema(Schema):
    id: UUID
    events: list[MinimalEventSchema] = Field(default_factory=list)
    event_series: list[MinimalEventSeriesSchema] = Field(default_factory=list)
    max_submission_age: timedelta | int | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType

    @field_serializer("max_submission_age")
    def serialize_max_submission_age(self, value: timedelta | int | None) -> int | None:
        """Convert timedelta to seconds for serialization."""
        if value is None:
            return None
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        return value


class OrganizationQuestionnaireInListSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireInListSchema
    pending_evaluations_count: int = 0


class OrganizationQuestionnaireSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireCreateSchema


class OrganizationQuestionnaireFieldsMixin(Schema):
    """Mixin for OrganizationQuestionnaire-specific fields (max_submission_age, questionnaire_type)."""

    max_submission_age: timedelta | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType = (
        OrganizationQuestionnaire.QuestionnaireType.ADMISSION
    )


class OrganizationQuestionnaireCreateSchema(
    questionnaires_schema.QuestionnaireCreateSchema, OrganizationQuestionnaireFieldsMixin
):
    """Schema for creating OrganizationQuestionnaire with its underlying Questionnaire.

    Combines Questionnaire creation fields (name, sections, questions, etc.) with
    OrganizationQuestionnaire wrapper fields (max_submission_age, questionnaire_type).
    """

    pass


class OrganizationQuestionnaireUpdateSchema(Schema):
    """Schema for updating OrganizationQuestionnaire and its underlying Questionnaire.

    Includes fields from both OrganizationQuestionnaire (wrapper) and Questionnaire (the actual questionnaire).
    All fields are optional to allow partial updates.
    """

    # Questionnaire fields (from QuestionnaireBaseSchema + additional)
    name: str | None = None
    min_score: Decimal | None = Field(None, ge=0, le=100)
    shuffle_questions: bool | None = None
    shuffle_sections: bool | None = None
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode | None = None
    llm_guidelines: str | None = None
    can_retake_after: timedelta | None = None
    max_attempts: int = Field(0, ge=0)

    # OrganizationQuestionnaire wrapper fields
    max_submission_age: timedelta | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType | None = None


class EventAssignmentSchema(Schema):
    event_ids: list[UUID]


class EventSeriesAssignmentSchema(Schema):
    event_series_ids: list[UUID]


class EventJWTInvitationTier(BaseModel):
    name: OneToOneFiftyString
    description: StrippedString | None = None


class EventInvitationRequestCreateSchema(Schema):
    message: StrippedString | None = None


class EventInvitationRequestSchema(ModelSchema):
    user: MinimalRevelUserSchema
    event: EventInListSchema

    class Meta:
        model = models.EventInvitationRequest
        fields = ["id", "message", "status", "created_at"]


class EventInvitationRequestInternalSchema(EventInvitationRequestSchema):
    decided_by: MinimalRevelUserSchema | None = None


class EventTokenSchema(ModelSchema):
    class Meta:
        model = models.EventToken
        fields = "__all__"


class EventTokenBaseSchema(Schema):
    name: OneToOneFiftyString | None = None
    max_uses: int = 1
    invitation: InvitationBaseSchema | None = None
    ticket_tier_id: UUID | None = None


class EventTokenCreateSchema(EventTokenBaseSchema):
    duration: int = 24 * 60


class EventTokenUpdateSchema(EventTokenBaseSchema):
    expires_at: AwareDatetime | None = None


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
        if "grants_membership" in self.__pydantic_fields_set__ and self.grants_membership and not self.membership_tier_id:
            raise ValueError("membership_tier_id is required when grants_membership is True")
        return self


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


class PotluckItemCreateSchema(ModelSchema):
    item_type: models.PotluckItem.ItemTypes

    class Meta:
        model = models.PotluckItem
        fields = ["name", "item_type", "quantity", "note"]


class PotluckItemRetrieveSchema(ModelSchema):
    is_assigned: bool = False
    is_owned: bool = False
    note_html: str = ""

    class Meta:
        model = models.PotluckItem
        fields = ["id", "name", "item_type", "quantity", "note"]


# ---- Additional Resources ----


class AdditionalResourceSchema(ModelSchema):
    description_html: str = ""
    text_html: str = ""
    event_ids: list[UUID] = Field(default_factory=list)
    event_series_ids: list[UUID] = Field(default_factory=list)

    @staticmethod
    def resolve_event_ids(obj: AdditionalResource) -> list[UUID]:
        """Return list of event UUIDs this resource is linked to.

        Uses values_list to fetch only IDs, avoiding loading full Event objects.
        """
        return list(obj.events.values_list("pk", flat=True))

    @staticmethod
    def resolve_event_series_ids(obj: AdditionalResource) -> list[UUID]:
        """Return list of event series UUIDs this resource is linked to.

        Uses values_list to fetch only IDs, avoiding loading full EventSeries objects.
        """
        return list(obj.event_series.values_list("pk", flat=True))

    class Meta:
        model = AdditionalResource
        fields = [
            "id",
            "resource_type",
            "name",
            "description",
            "file",
            "link",
            "text",
            "visibility",
            "display_on_organization_page",
        ]


class AdditionalResourceCreateSchema(Schema):
    name: str | None = None
    description: str | None = None
    resource_type: AdditionalResource.ResourceTypes
    visibility: AdditionalResource.Visibility = AdditionalResource.Visibility.MEMBERS_ONLY
    display_on_organization_page: bool = True
    link: str | None = None
    text: str | None = None
    event_series_ids: list[UUID] = Field(default_factory=list)
    event_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resource_content(self) -> "AdditionalResourceCreateSchema":
        """Ensure content fields match the resource_type.

        For FILE type: link and text must be None (file is passed separately as multipart).
        For LINK or TEXT type: exactly one of link or text must be provided and match resource_type.
        """
        content_fields = {"link": self.link, "text": self.text}
        provided_fields = [field for field, value in content_fields.items() if value]

        if self.resource_type == AdditionalResource.ResourceTypes.FILE:
            # For FILE type, link and text must not be provided (file comes separately)
            if provided_fields:
                raise ValueError(
                    f"When resource_type is 'file', 'link' and 'text' must not be provided. "
                    f"Found: {', '.join(provided_fields)}"
                )
        else:
            # For LINK or TEXT type, exactly one must be provided and match the type
            if len(provided_fields) != 1:
                raise ValueError(
                    f"For resource_type '{self.resource_type}', exactly one of 'link' or 'text' must be provided. "
                    f"Found: {len(provided_fields)}"
                )

            if provided_fields[0] != self.resource_type:
                raise ValueError(
                    f"The provided content field '{provided_fields[0]}' does not match "
                    f"the resource_type '{self.resource_type}'."
                )

        return self


class AdditionalResourceUpdateSchema(Schema):
    name: str | None = None
    description: str | None = None
    visibility: AdditionalResource.Visibility | None = None
    display_on_organization_page: bool | None = None
    link: str | None = None
    text: str | None = None
    event_series_ids: list[UUID] | None = None
    event_ids: list[UUID] | None = None


class MembershipTierSchema(ModelSchema):
    description: str | None = None
    description_html: str | None = None

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

    member_since: datetime = Field(alias="created_at")
    tier: MembershipTierSchema | None = None

    class Meta:
        model = models.OrganizationMember
        fields = ["created_at", "status", "tier"]


class OrganizationMemberSchema(Schema):
    user: MemberUserSchema
    member_since: datetime = Field(alias="created_at")
    status: OrganizationMember.MembershipStatus
    tier: MembershipTierSchema | None = None


class OrganizationMemberUpdateSchema(Schema):
    status: OrganizationMember.MembershipStatus | None = None
    tier_id: UUID4 | None = None


class OrganizationStaffSchema(Schema):
    user: MemberUserSchema
    staff_since: datetime = Field(alias="created_at")
    permissions: PermissionsSchema


class MemberAddSchema(Schema):
    user_id: UUID


class StaffAddSchema(Schema):
    user_id: UUID
    permissions: PermissionsSchema | None = None


class TagUpdateSchema(BaseModel):
    tags: list[OneToSixtyFourString] = Field(..., description="A list of tag names to add or remove.")


# ---- User Preferences Schemas ----


DEFAULT_VISIBILITY_PREFERENCE = models.BaseUserPreferences.VisibilityPreference.NEVER


class GeneralUserPreferencesSchema(Schema):
    """Schema for general user preferences (visibility and location)."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    city: CitySchema | None = None


class GeneralUserPreferencesUpdateSchema(Schema):
    """Schema for updating general user preferences."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    city_id: int | None = None


# --- Stripe Schemas ---


class StripeOnboardingLinkSchema(Schema):
    onboarding_url: str


class StripeAccountStatusSchema(Schema):
    is_connected: bool
    charges_enabled: bool = False
    details_submitted: bool = False


class StripeCheckoutSessionSchema(Schema):
    checkout_url: str


class PWYCCheckoutPayloadSchema(Schema):
    """Schema for Pay What You Can checkout payload."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


# ---- Guest User Schemas ----


class GuestUserDataSchema(Schema):
    """Base schema for guest user data (no authentication required)."""

    email: EmailStr
    first_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's first name")
    last_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's last name")


class GuestPWYCCheckoutSchema(GuestUserDataSchema):
    """Schema for guest PWYC ticket checkout."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


class GuestActionResponseSchema(Schema):
    """Response after guest action initiated (RSVP or non-online-payment ticket)."""

    message: str = Field(default="Please check your email to confirm your action")


class GuestActionConfirmSchema(Schema):
    """Request to confirm a guest action via JWT token."""

    token: str = Field(..., description="JWT token from confirmation email")


# ---- Guest JWT Payload Schemas (for email confirmation tokens) ----


class GuestRSVPJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest RSVP confirmation."""

    type: t.Literal["guest_rsvp"] = "guest_rsvp"
    event_id: UUID4
    answer: t.Literal["yes", "no", "maybe"]


class GuestTicketJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest ticket purchase confirmation.

    Only used for non-online-payment tickets (free/offline/at-the-door).
    Online payment tickets go directly to Stripe without email confirmation.
    """

    type: t.Literal["guest_ticket"] = "guest_ticket"
    event_id: UUID4
    tier_id: UUID4
    pwyc_amount: Decimal | None = None


# Discriminated union for guest action payloads
GuestActionPayload = t.Annotated[
    t.Union[
        t.Annotated[GuestRSVPJWTPayloadSchema, Tag("guest_rsvp")],
        t.Annotated[GuestTicketJWTPayloadSchema, Tag("guest_ticket")],
    ],
    Discriminator("type"),
]


# ---- TicketTier Schemas for Admin CRUD ----


class TicketTierPriceValidationMixin(Schema):
    payment_method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.OFFLINE
    price: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_minimum_price(self) -> t.Self:
        """Validate the minimum price for ONLINE payments."""
        if self.payment_method == TicketTier.PaymentMethod.ONLINE and self.price < Decimal("1"):
            raise ValueError("Minimum price for ONLINE payments should be at least 1.")
        return self


class TicketTierCreateSchema(TicketTierPriceValidationMixin):
    name: OneToOneFiftyString
    description: StrippedString | None = None
    visibility: TicketTier.Visibility = TicketTier.Visibility.PUBLIC
    purchasable_by: TicketTier.PurchasableBy = TicketTier.PurchasableBy.PUBLIC
    price_type: TicketTier.PriceType = TicketTier.PriceType.FIXED
    pwyc_min: Decimal = Field(default=Decimal("1"), ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)

    currency: Currencies = Field(default="EUR", max_length=3)
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None
    restricted_to_membership_tiers_ids: list[UUID4] | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self


class TicketTierUpdateSchema(TicketTierPriceValidationMixin):
    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    visibility: TicketTier.Visibility | None = None
    purchasable_by: TicketTier.PurchasableBy | None = None
    price_type: TicketTier.PriceType | None = None
    pwyc_min: Decimal | None = Field(None, ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)
    currency: str | None = Field(None, max_length=3)
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None
    restricted_to_membership_tiers_ids: list[UUID4] | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_min and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self


class TicketTierDetailSchema(ModelSchema):
    event_id: UUID
    total_available: int | None = None
    restricted_to_membership_tiers: list[MembershipTierSchema] | None = None

    class Meta:
        model = TicketTier
        fields = [
            "id",
            "name",
            "description",
            "visibility",
            "payment_method",
            "purchasable_by",
            "price",
            "price_type",
            "pwyc_min",
            "pwyc_max",
            "currency",
            "sales_start_at",
            "sales_end_at",
            "created_at",
            "updated_at",
            "total_quantity",
            "quantity_sold",
            "manual_payment_instructions",
            "restricted_to_membership_tiers",
        ]


class AttendeeSchema(ModelSchema):
    display_name: str

    class Meta:
        model = RevelUser
        fields = ["preferred_name", "pronouns", "first_name", "last_name"]


# Dietary Summary Schemas


class AggregatedDietaryRestrictionSchema(Schema):
    """Aggregated dietary restriction data for event attendees."""

    food_item: str = Field(..., description="Food or ingredient name")
    severity: DietaryRestriction.RestrictionType = Field(
        ..., description="Restriction severity (dislike, intolerant, allergy, severe_allergy)"
    )
    attendee_count: int = Field(..., description="Number of attendees with this restriction")
    notes: list[str] = Field(default_factory=list, description="Non-empty notes from attendees")


class AggregatedDietaryPreferenceSchema(Schema):
    """Aggregated dietary preference data for event attendees."""

    name: str = Field(..., description="Dietary preference name")
    attendee_count: int = Field(..., description="Number of attendees with this preference")
    comments: list[str] = Field(default_factory=list, description="Non-empty comments from attendees")


class EventDietarySummarySchema(Schema):
    """Aggregated dietary information for event attendees."""

    restrictions: list[AggregatedDietaryRestrictionSchema] = Field(
        default_factory=list,
        description="Aggregated dietary restrictions",
    )
    preferences: list[AggregatedDietaryPreferenceSchema] = Field(
        default_factory=list,
        description="Aggregated dietary preferences",
    )
