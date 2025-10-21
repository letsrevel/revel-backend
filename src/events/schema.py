import typing as t
from datetime import datetime, time
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, BaseModel, EmailStr, Field, field_serializer, field_validator, model_validator

from accounts.schema import MemberUserSchema, MinimalRevelUserSchema
from common.schema import OneToOneFiftyString, OneToSixtyFourString, StrippedString
from events import models
from events.models import (
    AdditionalResource,
    Event,
    EventRSVP,
    Organization,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
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
        # obj.tags is a RelatedManager of TagAssignment
        return [ta.tag.name for ta in obj.tags.all()]


class OrganizationEditSchema(CityEditMixin):
    description: StrippedString = ""
    visibility: Organization.Visibility


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


class EventSeriesRetrieveSchema(TaggableSchemaMixin):
    id: UUID
    organization: OrganizationRetrieveSchema
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
    event_type: Event.Types | None = None
    status: Event.Status = Event.Status.DRAFT
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
    requires_ticket: bool = False
    potluck_open: bool = False


class EventCreateSchema(EventEditSchema):
    name: OneToOneFiftyString
    start: AwareDatetime


class EventBaseSchema(CityRetrieveMixin, TaggableSchemaMixin):
    id: UUID
    event_type: Event.Types
    visibility: Event.Visibility
    organization: OrganizationRetrieveSchema
    status: Event.Status
    event_series: EventSeriesRetrieveSchema | None = None
    name: str
    slug: str
    description: str | None = None
    description_html: str = ""
    invitation_message: str | None = None
    invitation_message_html: str = ""
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


class EventInListSchema(EventBaseSchema):
    pass


class EventDetailSchema(EventBaseSchema):
    pass


class EventRSVPSchema(ModelSchema):
    event_id: UUID
    status: EventRSVP.Status

    class Meta:
        model = EventRSVP
        fields = ["status"]


class TierSchema(ModelSchema):
    event_id: UUID
    price: Decimal
    currency: str
    total_available: int | None
    description_html: str = ""

    class Meta:
        model = TicketTier
        fields = [
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
        ]


class EventTicketSchema(ModelSchema):
    event_id: UUID | None
    tier: TierSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "checked_in_at"]


class PendingTicketSchema(ModelSchema):
    """Schema for pending tickets in admin interface."""

    user: MemberUserSchema
    tier: TierSchema

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at"]


class CheckInRequestSchema(Schema):
    """Schema for ticket check-in requests."""

    ticket_id: UUID


class CheckInResponseSchema(ModelSchema):
    """Schema for ticket check-in response."""

    user: MinimalRevelUserSchema
    tier: TierSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "checked_in_at", "tier"]


class OrganizationPermissionsSchema(Schema):
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
    tier: TierSchema | None = None
    user_id: UUID


class DirectInvitationCreateSchema(InvitationBaseSchema):
    """Schema for creating direct invitations to events."""

    emails: list[EmailStr] = Field(..., min_length=1, description="List of email addresses to invite")
    tier_id: UUID = Field(description="Ticket tier to assign to invitations")
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
    tier: TierSchema | None = None
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
    tier: TierSchema | None = None
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
    tier: TierSchema | None = None
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
    start: datetime | None = None
    logo: str | None = None


class BaseOrganizationQuestionnaireSchema(Schema):
    id: UUID
    events: list[MinimalEventSchema] = Field(default_factory=list)
    event_series: list[EventSeriesRetrieveSchema] = Field(default_factory=list)
    max_submission_age: time | int | None = None
    questionnaire_type: OrganizationQuestionnaire.Types

    @field_serializer("max_submission_age")
    def serialize_max_submission_age(self, value: time | int | None) -> int | None:
        """Convert time to seconds since midnight for serialization."""
        if value is None:
            return None
        if isinstance(value, time):
            return value.hour * 3600 + value.minute * 60 + value.second
        return value


class OrganizationQuestionnaireInListSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireInListSchema


class OrganizationQuestionnaireSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireCreateSchema


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
    evaluation_mode: Questionnaire.EvaluationMode | None = None
    llm_guidelines: str | None = None
    can_retake_after: int | None = None  # Duration in seconds
    max_attempts: int | None = Field(None, ge=0)

    # OrganizationQuestionnaire wrapper fields
    max_submission_age: time | None = None  # Time field (accepts time strings like "HH:MM:SS")
    questionnaire_type: OrganizationQuestionnaire.Types | None = None


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
    invitation_tier_id: UUID | None = None


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


class OrganizationTokenCreateSchema(OrganizationTokenBaseSchema):
    duration: int = 24 * 60


class OrganizationTokenUpdateSchema(OrganizationTokenBaseSchema):
    expires_at: AwareDatetime | None = None


class OrganizationMembershipRequestRetrieve(ModelSchema):
    user: MinimalRevelUserSchema

    class Meta:
        model = OrganizationMembershipRequest
        fields = ["id", "status", "created_at", "user"]


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


class OrganizationMemberSchema(Schema):
    user: MemberUserSchema
    member_since: datetime = Field(alias="created_at")


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


class BaseUserPreferencesSchema(Schema):
    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    event_reminders: bool = True
    silence_all_notifications: bool = False


class GeneralUserPreferencesSchema(BaseUserPreferencesSchema):
    city: CitySchema | None = None


class BaseSubscriptionPreferencesSchema(BaseUserPreferencesSchema):
    is_subscribed: bool


class UserOrganizationPreferencesSchema(BaseSubscriptionPreferencesSchema):
    notify_on_new_events: bool


class UserEventSeriesPreferencesSchema(BaseSubscriptionPreferencesSchema):
    notify_on_new_events: bool


class UserEventPreferencesSchema(BaseSubscriptionPreferencesSchema):
    notify_on_potluck_updates: bool


class BaseUserPreferencesUpdateSchema(Schema):
    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    event_reminders: bool = True
    silence_all_notifications: bool = False


class GeneralUserPreferencesUpdateSchema(BaseUserPreferencesUpdateSchema):
    city_id: int | None = None


class BaseSubscriptionPreferencesUpdateSchema(BaseUserPreferencesUpdateSchema):
    is_subscribed: bool = False


class UserOrganizationPreferencesUpdateSchema(BaseSubscriptionPreferencesUpdateSchema):
    notify_on_new_events: bool = True


class UserEventSeriesPreferencesUpdateSchema(BaseSubscriptionPreferencesUpdateSchema):
    notify_on_new_events: bool = True


class UserEventPreferencesUpdateSchema(BaseSubscriptionPreferencesUpdateSchema):
    notify_on_potluck_updates: bool = False


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

    currency: str = Field(default="USD", max_length=3)
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None

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
        ]
