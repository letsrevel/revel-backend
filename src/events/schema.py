import re
import typing as t
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from django.utils.translation import gettext as _
from ninja import ModelSchema, Schema
from pydantic import (
    UUID4,
    AnyUrl,
    AwareDatetime,
    BaseModel,
    Discriminator,
    EmailStr,
    Field,
    HttpUrl,
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
    ResourceVisibility,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
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


class CityBaseMixin(Schema):
    city_id: int | None = None

    @field_validator("city_id", mode="after")
    @classmethod
    def validate_city_exists(cls, v: int | None) -> int | None:
        """Validate that city exists."""
        if v is not None and not City.objects.filter(pk=v).exists():
            raise ValueError(f"City with ID {v} does not exist.")
        return v


class CityEditMixin(CityBaseMixin):
    address: StrippedString | None = None
    location_maps_url: HttpUrl | None = None
    location_maps_embed: StrippedString | None = None

    @field_validator("location_maps_embed", mode="after")
    @classmethod
    def extract_src_from_iframe(cls, v: str | None) -> str | None:
        """Extract src URL from iframe HTML, or pass through if already a URL.

        Users paste the full iframe from Google Maps share dialog.
        We extract and store just the src URL for cleaner data storage
        and frontend flexibility.

        Also accepts already-extracted URLs (for re-saving existing data).
        """
        if not v:
            return None
        # Already a URL (re-saving existing data) - pass through
        if v.startswith(("http://", "https://")):
            return v
        # Must be an iframe
        if not v.lower().startswith("<iframe"):
            raise ValueError("Must be an iframe element (paste the embed code from Google Maps)")
        match = re.search(r'src=["\']([^"\']+)["\']', v)
        if not match:
            raise ValueError("Could not extract src URL from iframe")
        return match.group(1)


class CityRetrieveMixin(Schema):
    city: CitySchema | None = None
    address: str | None = None
    location_maps_url: str | None = None
    location_maps_embed: str | None = None


class TaggableSchemaMixin(Schema):
    tags: list[str] = Field(default_factory=list)

    @staticmethod
    def resolve_tags(obj: models.Event) -> list[str]:
        """Flattify tags."""
        if hasattr(obj, "prefetched_tagassignments"):
            return [ta.tag.name for ta in obj.prefetched_tagassignments]
        return [ta.tag.name for ta in obj.tags.all()]


# Social media URL field names
_SOCIAL_MEDIA_FIELDS = ("instagram_url", "facebook_url", "bluesky_url", "telegram_url")

# Social media platform URL patterns for validation
_SOCIAL_MEDIA_PATTERNS: dict[str, tuple[str, ...]] = {
    "instagram_url": ("instagram.com", "www.instagram.com"),
    "facebook_url": ("facebook.com", "www.facebook.com", "fb.com", "www.fb.com"),
    "bluesky_url": ("bsky.app", "bsky.social"),
    "telegram_url": ("t.me", "telegram.me", "telegram.dog"),
}


def _validate_social_media_url(url: str, field_name: str) -> None:
    """Validate that a URL matches the expected social media platform.

    Args:
        url: The URL to validate.
        field_name: The field name to look up allowed domains.

    Raises:
        ValueError: If the URL doesn't match the expected platform.
    """
    from urllib.parse import urlparse

    allowed_domains = _SOCIAL_MEDIA_PATTERNS.get(field_name, ())
    if not allowed_domains:
        return

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if hostname not in allowed_domains:
        platform_name = field_name.replace("_url", "").replace("_", " ").title()
        raise ValueError(
            f"URL must be a valid {platform_name} link. Got: url={url} | {hostname!r} not in {allowed_domains}"
        )


class SocialMediaSchemaRetrieveMixin(Schema):
    """Mixin for reading social media URL fields. No validation needed."""

    instagram_url: str | None = None
    facebook_url: str | None = None
    bluesky_url: str | None = None
    telegram_url: str | None = None


class SocialMediaSchemaEditMixin(Schema):
    """Mixin for editing social media URL fields with platform validation.

    - Automatically prepends https:// if no scheme is provided
    - Validates that each URL matches its expected platform domain
    """

    instagram_url: AnyUrl | None = None
    facebook_url: AnyUrl | None = None
    bluesky_url: AnyUrl | None = None
    telegram_url: AnyUrl | None = None

    @field_validator(*_SOCIAL_MEDIA_FIELDS, mode="before")
    @classmethod
    def validate_social_media_urls(cls, v: t.Any, info: t.Any) -> str | None:
        """Prepend https:// if needed and validate platform domain."""
        if not v or not isinstance(v, str):
            return None

        # Prepend https:// if no scheme provided
        url: str = v if v.startswith(("http://", "https://")) else f"https://{v}"

        # Validate platform domain
        _validate_social_media_url(url, info.field_name)
        return url


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


class MinimalEventSeriesSchema(Schema):
    """Lightweight event series schema for use in event lists - excludes tags and uses minimal organization."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesInListSchema(TaggableSchemaMixin):
    """Schema for event series list endpoints - includes tags with proper prefetching."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None


class EventSeriesRetrieveSchema(TaggableSchemaMixin):
    """Full event series schema for detail views - uses minimal organization to prevent cascading queries."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesEditSchema(Schema):
    name: OneToOneFiftyString
    description: StrippedString | None = None


class EventEditSchema(CityEditMixin):
    name: OneToOneFiftyString | None = None
    address_visibility: ResourceVisibility = ResourceVisibility.PUBLIC
    description: StrippedString | None = None
    event_type: Event.EventType | None = None
    status: Event.EventStatus = Event.EventStatus.DRAFT
    visibility: Event.Visibility | None = None
    invitation_message: StrippedString | None = Field(None, description="Invitation message")
    max_attendees: int = 0
    max_tickets_per_user: int | None = Field(None, description="Max tickets per user (null = unlimited)")
    waitlist_open: bool = False
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    rsvp_before: AwareDatetime | None = Field(None, description="RSVP deadline for events that do not require tickets")
    check_in_starts_at: AwareDatetime | None = Field(None, description="When check-in opens for this event")
    check_in_ends_at: AwareDatetime | None = Field(None, description="When check-in closes for this event")
    event_series_id: UUID | None = None
    venue_id: UUID | None = None
    potluck_open: bool = False
    accept_invitation_requests: bool = False
    apply_before: AwareDatetime | None = Field(
        None, description="Deadline for submitting invitation requests or questionnaires"
    )
    can_attend_without_login: bool = False


class EventCreateSchema(EventEditSchema):
    name: OneToOneFiftyString
    start: AwareDatetime
    requires_ticket: bool = False


class EventDuplicateSchema(Schema):
    """Schema for duplicating an event."""

    name: OneToOneFiftyString
    start: AwareDatetime


# Slug must be lowercase alphanumeric with hyphens, 1-255 chars
SlugString = t.Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class EventEditSlugSchema(Schema):
    """Schema for editing an event's slug."""

    slug: SlugString


class EventBaseSchema(TaggableSchemaMixin):
    id: UUID
    event_type: Event.EventType
    visibility: Event.Visibility
    address_visibility: ResourceVisibility = ResourceVisibility.PUBLIC
    organization: MinimalOrganizationSchema
    status: Event.EventStatus
    event_series: MinimalEventSeriesSchema | None = None
    venue: "VenueSchema | None" = None
    name: str
    slug: str
    description: str | None = None
    invitation_message: str | None = None
    max_attendees: int = 0
    max_tickets_per_user: int | None = None
    waitlist_open: bool | None = None
    start: AwareDatetime
    end: AwareDatetime
    rsvp_before: AwareDatetime | None = None
    logo: str | None = None
    cover_art: str | None = None
    requires_ticket: bool
    potluck_open: bool
    attendee_count: int
    accept_invitation_requests: bool
    apply_before: AwareDatetime | None = None
    can_attend_without_login: bool
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None


class EventInListSchema(EventBaseSchema):
    city: CitySchema | None = None


class EventDetailSchema(EventBaseSchema):
    city: CitySchema | None = None
    address: str | None = None
    location_maps_url: str | None = None
    location_maps_embed: str | None = None

    @staticmethod
    def resolve_address(obj: Event, context: t.Any) -> str | None:
        """Conditionally return address based on address_visibility setting.

        If the user cannot see the address, returns an explanatory message
        about who can see it based on the address_visibility setting.
        """
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.address

        # Return explanation based on visibility setting
        visibility_messages: dict[str, str] = {
            ResourceVisibility.PRIVATE: _("Address visible to invited guests only"),
            ResourceVisibility.MEMBERS_ONLY: _("Address visible to organization members only"),
            ResourceVisibility.STAFF_ONLY: _("Address visible to staff only"),
            ResourceVisibility.ATTENDEES_ONLY: _("Address visible to attendees only"),
        }
        return visibility_messages.get(obj.address_visibility)

    @staticmethod
    def resolve_location_maps_url(obj: Event, context: t.Any) -> str | None:
        """Return maps URL only if user can see the address."""
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.location_maps_url
        return None

    @staticmethod
    def resolve_location_maps_embed(obj: Event, context: t.Any) -> str | None:
        """Return maps embed URL only if user can see the address."""
        user = context["request"].user
        if obj.can_user_see_address(user):
            return obj.location_maps_embed
        return None


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
    created_at: AwareDatetime
    updated_at: AwareDatetime
    membership: "MinimalOrganizationMemberSchema | None" = None

    class Meta:
        model = EventRSVP
        fields = ["id", "status", "created_at", "updated_at"]

    @staticmethod
    def resolve_membership(obj: EventRSVP) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None


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
    created_at: AwareDatetime
    updated_at: AwareDatetime

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
    restricted_to_membership_tiers: list["MembershipTierSchema"] | None = None
    seat_assignment_mode: TicketTier.SeatAssignmentMode
    max_tickets_per_user: int | None = None
    venue: "VenueSchema | None" = None
    sector: "VenueSectorSchema | None" = None

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
            "seat_assignment_mode",
            "max_tickets_per_user",
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


class MinimalPaymentSchema(ModelSchema):
    """Minimal payment info for inclusion in ticket schemas."""

    status: Payment.PaymentStatus

    class Meta:
        model = Payment
        fields = ["id", "status"]


class MinimalSeatSchema(ModelSchema):
    """Minimal seat schema for ticket responses."""

    class Meta:
        model = VenueSeat
        fields = ["id", "label", "row", "number", "is_accessible", "is_obstructed_view"]


class AdminTicketSchema(ModelSchema):
    """Schema for pending tickets in admin interface.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    user: MemberUserSchema
    tier: TicketTierSchema
    payment: PaymentSchema | None = None
    guest_name: str
    seat: MinimalSeatSchema | None = None
    membership: "MinimalOrganizationMemberSchema | None" = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "guest_name", "seat"]

    @staticmethod
    def resolve_membership(obj: Ticket) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None


class UserTicketSchema(ModelSchema):
    """Schema for user's own tickets with event details.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    event: "MinimalEventSchema"
    tier: TicketTierSchema
    status: Ticket.TicketStatus
    apple_pass_available: bool
    guest_name: str
    payment: MinimalPaymentSchema | None = None
    seat: MinimalSeatSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "checked_in_at", "guest_name", "seat"]

    @staticmethod
    def resolve_payment(obj: Ticket) -> Payment | None:
        """Resolve payment for pending tickets."""
        if hasattr(obj, "payment"):
            return obj.payment
        return None


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


class EventUserStatusResponse(Schema):
    """Response for user's status at an event.

    This is a unified response that includes:
    - Tickets: List of user's tickets for this event (if any)
    - RSVP: User's RSVP status (for non-ticketed events)
    - Eligibility: Whether user can purchase tickets and why not
    - Purchase limits: How many more tickets can be purchased
    """

    tickets: list[UserTicketSchema] = Field(default_factory=list)
    rsvp: EventRSVPSchema | None = None
    can_purchase_more: bool = True
    remaining_tickets: int | None = None  # None = unlimited


class InvitationBaseSchema(Schema):
    waives_questionnaire: bool = False
    waives_purchase: bool = False
    overrides_max_attendees: bool = False
    waives_membership_required: bool = False
    waives_rsvp_deadline: bool = False
    waives_apply_deadline: bool = False
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
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


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
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


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
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


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
    waives_apply_deadline: bool
    custom_message: str | None = None
    created_at: AwareDatetime


# Questionnaires


class MinimalEventSchema(Schema):
    id: UUID
    slug: str
    name: str
    start: AwareDatetime
    end: AwareDatetime
    logo: str | None = None
    cover_art: str | None = None
    venue: "VenueSchema | None" = None


class BaseOrganizationQuestionnaireSchema(Schema):
    id: UUID
    events: list[MinimalEventSchema] = Field(default_factory=list)
    event_series: list[MinimalEventSeriesSchema] = Field(default_factory=list)
    max_submission_age: timedelta | int | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType
    members_exempt: bool

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
    """Mixin for OrganizationQuestionnaire-specific fields (max_submission_age, questionnaire_type, members_exempt)."""

    max_submission_age: timedelta | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType = (
        OrganizationQuestionnaire.QuestionnaireType.ADMISSION
    )
    members_exempt: bool = False


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
    members_exempt: bool | None = None


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
    grants_invitation: bool = False
    invitation_payload: InvitationBaseSchema | None = None
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
        if (
            "grants_membership" in self.__pydantic_fields_set__
            and self.grants_membership
            and not self.membership_tier_id
        ):
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

    class Meta:
        model = models.PotluckItem
        fields = ["id", "name", "item_type", "quantity", "note"]


# ---- Additional Resources ----


class AdditionalResourceSchema(ModelSchema):
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
    visibility: ResourceVisibility = ResourceVisibility.MEMBERS_ONLY
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
    visibility: ResourceVisibility | None = None
    display_on_organization_page: bool | None = None
    link: str | None = None
    text: str | None = None
    event_series_ids: list[UUID] | None = None
    event_ids: list[UUID] | None = None


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


class TagUpdateSchema(BaseModel):
    tags: list[OneToSixtyFourString] = Field(..., description="A list of tag names to add or remove.")


# ---- User Preferences Schemas ----


DEFAULT_VISIBILITY_PREFERENCE = models.BaseUserPreferences.VisibilityPreference.NEVER


class GeneralUserPreferencesSchema(Schema):
    """Schema for general user preferences (visibility and location)."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE
    city: CitySchema | None = None


class GeneralUserPreferencesUpdateSchema(CityBaseMixin):
    """Schema for updating general user preferences."""

    show_me_on_attendee_list: models.BaseUserPreferences.VisibilityPreference = DEFAULT_VISIBILITY_PREFERENCE


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


# ---- Batch Checkout Schemas ----


class TicketPurchaseItem(Schema):
    """Single ticket item in a batch purchase."""

    guest_name: StrippedString = Field(..., min_length=1, max_length=255, description="Name of the ticket holder")
    seat_id: UUID | None = Field(default=None, description="Seat ID for USER_CHOICE seat assignment mode")


class BatchCheckoutPayload(Schema):
    """Payload for batch ticket checkout (authenticated users)."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")


class BatchCheckoutPWYCPayload(BatchCheckoutPayload):
    """Payload for batch PWYC ticket checkout."""

    price_per_ticket: Decimal = Field(..., ge=1, description="Pay what you can amount per ticket (same for all)")


class BatchCheckoutResponse(Schema):
    """Response for batch checkout operations."""

    checkout_url: str | None = Field(None, description="Stripe checkout URL (for online payment)")
    tickets: list[UserTicketSchema] = Field(
        default_factory=list, description="Created tickets (for free/offline payments)"
    )


# ---- Guest User Schemas ----


class GuestUserDataSchema(Schema):
    """Base schema for guest user data (no authentication required)."""

    email: EmailStr
    first_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's first name")
    last_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's last name")


class GuestPWYCCheckoutSchema(GuestUserDataSchema):
    """Schema for guest PWYC ticket checkout."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


class GuestBatchCheckoutPayload(GuestUserDataSchema):
    """Payload for batch checkout by guest (unauthenticated) users."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")


class GuestBatchCheckoutPWYCPayload(GuestBatchCheckoutPayload):
    """Payload for batch PWYC checkout by guest users."""

    price_per_ticket: Decimal = Field(..., ge=1, description="Pay what you can amount per ticket (same for all)")


class GuestActionResponseSchema(Schema):
    """Response after guest action initiated (RSVP or non-online-payment ticket)."""

    message: str = Field(default="Please check your email to confirm your action")


class GuestCheckoutResponseSchema(Schema):
    """Combined response for guest checkout - either email confirmation or Stripe checkout."""

    # For non-online payments (email confirmation)
    message: str | None = Field(None, description="Confirmation message (for non-online payments)")
    # For online payments (Stripe checkout)
    checkout_url: str | None = Field(None, description="Stripe checkout URL (for online payment)")
    tickets: list["UserTicketSchema"] = Field(
        default_factory=list,
        description="Created tickets (only present after guest email confirmation for free/offline payments)",
    )


class GuestActionConfirmSchema(Schema):
    """Request to confirm a guest action via JWT token."""

    token: str = Field(..., description="JWT token from confirmation email")


# ---- Guest JWT Payload Schemas (for email confirmation tokens) ----


class GuestRSVPJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest RSVP confirmation."""

    type: t.Literal["guest_rsvp"] = "guest_rsvp"
    event_id: UUID4
    answer: t.Literal["yes", "no", "maybe"]


class GuestTicketItemPayload(Schema):
    """Ticket item info stored in JWT payload for guest checkout confirmation."""

    guest_name: str
    seat_id: UUID4 | None = None


class GuestTicketJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest ticket purchase confirmation.

    Only used for non-online-payment tickets (free/offline/at-the-door).
    Online payment tickets go directly to Stripe without email confirmation.
    """

    type: t.Literal["guest_ticket"] = "guest_ticket"
    event_id: UUID4
    tier_id: UUID4
    pwyc_amount: Decimal | None = None
    tickets: list[GuestTicketItemPayload] = Field(default_factory=list)


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
    manual_payment_instructions: StrippedString | None = None

    # Venue/seating configuration
    seat_assignment_mode: TicketTier.SeatAssignmentMode = TicketTier.SeatAssignmentMode.NONE
    max_tickets_per_user: int | None = None
    venue_id: UUID | None = None
    sector_id: UUID | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that seat assignment modes require a sector."""
        if self.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE and self.sector_id is None:
            raise ValueError("Sector is required when seat assignment mode is not NONE.")
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
    manual_payment_instructions: StrippedString | None = None

    # Venue/seating configuration
    seat_assignment_mode: TicketTier.SeatAssignmentMode | None = None
    max_tickets_per_user: int | None = None
    venue_id: UUID | None = None
    sector_id: UUID | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_min and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that seat assignment modes require a sector when being set."""
        # Only validate if seat_assignment_mode is being explicitly set to a non-NONE value
        if (
            self.seat_assignment_mode is not None
            and self.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE
            and self.sector_id is None
        ):
            raise ValueError("Sector is required when seat assignment mode is not NONE.")
        return self


class TicketTierDetailSchema(ModelSchema):
    event_id: UUID
    total_available: int | None = None
    restricted_to_membership_tiers: list[MembershipTierSchema] | None = None
    seat_assignment_mode: TicketTier.SeatAssignmentMode
    max_tickets_per_user: int | None = None
    venue: "VenueSchema | None" = None
    sector: "VenueSectorSchema | None" = None

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
            "seat_assignment_mode",
            "max_tickets_per_user",
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


# ---- Venue Management Schemas ----


class Coordinate2D(Schema):
    """A 2D coordinate point with x and y values."""

    x: float
    y: float


# A polygon is a list of at least 3 coordinate points
PolygonShape = t.Annotated[list[Coordinate2D], Field(min_length=3)]


def point_in_polygon(point: Coordinate2D, polygon: list[Coordinate2D]) -> bool:
    """Check if a point is inside a polygon using ray casting algorithm.

    Args:
        point: Coordinate2D with x and y values
        polygon: List of Coordinate2D points forming the polygon vertices

    Returns:
        True if point is inside the polygon, False otherwise
    """
    x, y = point.x, point.y
    n = len(polygon)
    inside = False

    p1x, p1y = polygon[0].x, polygon[0].y
    for i in range(1, n + 1):
        p2x, p2y = polygon[i % n].x, polygon[i % n].y
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
        p1x, p1y = p2x, p2y

    return inside


class VenueSeatSchema(ModelSchema):
    """Schema for venue seat response.

    The `available` field defaults to True and can be overridden when returning
    seat availability for ticket purchase (e.g., via annotate or manual setting).
    """

    position: Coordinate2D | None = None
    available: bool = True  # For availability endpoints: False if taken by PENDING/ACTIVE ticket

    class Meta:
        model = VenueSeat
        fields = [
            "id",
            "label",
            "row",
            "number",
            "position",
            "is_accessible",
            "is_obstructed_view",
            "is_active",
        ]


class VenueSectorSchema(ModelSchema):
    """Schema for venue sector response (without seats)."""

    shape: list[Coordinate2D] | None = None
    metadata: dict[str, t.Any] | None = None

    class Meta:
        model = VenueSector
        fields = [
            "id",
            "name",
            "code",
            "shape",
            "capacity",
            "display_order",
            "metadata",
        ]


class VenueSectorWithSeatsSchema(VenueSectorSchema):
    """Schema for venue sector with nested seats."""

    seats: list[VenueSeatSchema] = Field(default_factory=list)


class VenueSchema(ModelSchema, CityRetrieveMixin):
    """Schema for venue response."""

    class Meta:
        model = Venue
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "capacity",
            "address",
            "location_maps_url",
            "location_maps_embed",
        ]


class VenueDetailSchema(VenueSchema):
    """Schema for venue detail response with sectors (no seats)."""

    sectors: list[VenueSectorSchema] = Field(default_factory=list)


class VenueWithSeatsSchema(VenueSchema):
    """Schema for venue with all sectors and seats."""

    sectors: list[VenueSectorWithSeatsSchema] = Field(default_factory=list)


# ---- Venue Availability Schemas (for ticket purchase flow) ----


class SectorAvailabilitySchema(Schema):
    """Sector with seat availability info.

    Extends VenueSectorSchema fields with availability counts.
    Uses VenueSeatSchema with `available` field for seat status.
    """

    id: UUID
    name: str
    code: str | None = None
    shape: list[Coordinate2D] | None = None
    capacity: int | None = None
    display_order: int = 0
    metadata: dict[str, t.Any] | None = None  # For frontend rendering (e.g., aisle positions)
    seats: list[VenueSeatSchema] = Field(default_factory=list)
    available_count: int = 0  # Number of available seats
    total_count: int = 0  # Total active seats


class VenueAvailabilitySchema(Schema):
    """Venue layout with seat availability for ticket purchase."""

    id: UUID
    name: str
    sectors: list[SectorAvailabilitySchema] = Field(default_factory=list)
    total_available: int = 0  # Total available seats across all sectors
    total_capacity: int = 0  # Total seats across all sectors


class VenueCreateSchema(CityEditMixin):
    """Schema for creating a venue."""

    name: OneToOneFiftyString
    description: StrippedString | None = None
    capacity: int | None = Field(None, ge=0)


class VenueUpdateSchema(CityEditMixin):
    """Schema for updating a venue."""

    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    capacity: int | None = Field(None, ge=0)


class VenueSeatInputSchema(Schema):
    """Schema for creating/updating a seat within a sector."""

    label: t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)]
    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool = False
    is_obstructed_view: bool = False
    is_active: bool = True


class VenueSectorCreateSchema(Schema):
    """Schema for creating a sector with optional nested seats."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    code: t.Annotated[str, StringConstraints(max_length=30, strip_whitespace=True)] | None = None
    shape: PolygonShape | None = Field(
        None,
        description="Polygon vertices [{x, y}, ...] for FE rendering. Minimum 3 points.",
    )
    capacity: int | None = Field(None, ge=0)
    display_order: int = Field(0, ge=0)
    metadata: dict[str, t.Any] | None = Field(
        None,
        description="Arbitrary JSON metadata for frontend rendering (e.g., aisle positions).",
    )
    seats: list[VenueSeatInputSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_seat_positions(self) -> "VenueSectorCreateSchema":
        """Validate that seat positions are within the sector shape if both are defined."""
        if not self.shape or not self.seats:
            return self

        for seat in self.seats:
            if seat.position is not None:
                if not point_in_polygon(seat.position, self.shape):
                    raise ValueError(f"Seat '{seat.label}' position is outside the sector shape.")

        return self


class VenueSectorUpdateSchema(Schema):
    """Schema for updating a sector's metadata."""

    name: t.Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)] | None = None
    code: t.Annotated[str, StringConstraints(max_length=30, strip_whitespace=True)] | None = None
    shape: PolygonShape | None = Field(
        None,
        description="Polygon vertices [{x, y}, ...] for FE rendering. Minimum 3 points.",
    )
    capacity: int | None = Field(None, ge=0)
    display_order: int | None = Field(None, ge=0)
    metadata: dict[str, t.Any] | None = Field(
        None,
        description="Arbitrary JSON metadata for frontend rendering (e.g., aisle positions).",
    )


class VenueSeatBulkCreateSchema(Schema):
    """Schema for bulk creating seats in a sector."""

    seats: list[VenueSeatInputSchema] = Field(
        ...,
        min_length=1,
        description="List of seats to create in the sector.",
    )


class VenueSeatBulkDeleteSchema(Schema):
    """Schema for bulk deleting seats in a sector."""

    labels: list[t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)]] = Field(
        ...,
        min_length=1,
        description="List of seat labels to delete.",
    )


class VenueSeatUpdateSchema(Schema):
    """Schema for updating an individual seat."""

    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool | None = None
    is_obstructed_view: bool | None = None
    is_active: bool | None = None


class VenueSeatBulkUpdateItemSchema(Schema):
    """Schema for a single seat update in bulk update operation.

    Identifies the seat by label and includes the fields to update.
    """

    label: t.Annotated[str, StringConstraints(min_length=1, max_length=50, strip_whitespace=True)] = Field(
        ...,
        description="The label of the seat to update (identifier).",
    )
    row: t.Annotated[str, StringConstraints(max_length=20, strip_whitespace=True)] | None = None
    number: int | None = Field(None, ge=0)
    position: Coordinate2D | None = Field(
        None,
        description="Seat position {x, y}. Must be within sector shape if shape is defined.",
    )
    is_accessible: bool | None = None
    is_obstructed_view: bool | None = None
    is_active: bool | None = None


class VenueSeatBulkUpdateSchema(Schema):
    """Schema for bulk updating seats in a sector."""

    seats: list[VenueSeatBulkUpdateItemSchema] = Field(
        ...,
        min_length=1,
        description="List of seats to update with their new values.",
    )


# ---- Blacklist/Whitelist Schemas ----


class BlacklistEntrySchema(ModelSchema):
    """Schema for retrieving a blacklist entry."""

    user_id: UUID | None = None
    user_display_name: str | None = None
    created_by_name: str | None = None

    class Meta:
        model = models.Blacklist
        fields = [
            "id",
            "email",
            "telegram_username",
            "phone_number",
            "first_name",
            "last_name",
            "preferred_name",
            "reason",
            "created_at",
        ]

    @staticmethod
    def resolve_user_id(obj: models.Blacklist) -> UUID | None:
        """Return the user ID if entry is linked to a user."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.Blacklist) -> str | None:
        """Return the display name of the linked user."""
        if obj.user:
            return obj.user.get_display_name()
        return None

    @staticmethod
    def resolve_created_by_name(obj: models.Blacklist) -> str | None:
        """Return the display name of who created the entry."""
        if obj.created_by:
            return obj.created_by.get_display_name()
        return None


class BlacklistCreateSchema(Schema):
    """Schema for creating a blacklist entry (manual mode).

    Provide at least one identifier (email, telegram, phone) or name.
    """

    user_id: UUID | None = Field(
        None,
        description="Quick mode: provide user ID to auto-populate all fields from user.",
    )
    email: str | None = Field(None, description="Email address")
    telegram_username: str | None = Field(
        None,
        description="Telegram username (with or without @ prefix)",
    )
    phone_number: str | None = Field(
        None,
        description="Phone number in E.164 format",
    )
    first_name: str | None = Field(None, max_length=150)
    last_name: str | None = Field(None, max_length=150)
    preferred_name: str | None = Field(None, max_length=150)
    reason: str = Field("", description="Reason for blacklisting")


class BlacklistUpdateSchema(Schema):
    """Schema for updating a blacklist entry.

    Only reason and name fields can be updated.
    Hard identifiers cannot be changed after creation.
    """

    reason: str | None = None
    first_name: str | None = Field(None, max_length=150)
    last_name: str | None = Field(None, max_length=150)
    preferred_name: str | None = Field(None, max_length=150)


class WhitelistRequestSchema(ModelSchema):
    """Schema for retrieving a whitelist request."""

    user_id: UUID
    user_display_name: str
    user_email: str
    matched_entries_count: int
    decided_by_name: str | None = None

    class Meta:
        model = models.WhitelistRequest
        fields = [
            "id",
            "message",
            "status",
            "created_at",
            "decided_at",
        ]

    @staticmethod
    def resolve_user_id(obj: models.WhitelistRequest) -> UUID:
        """Return the user ID of the requester."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.WhitelistRequest) -> str:
        """Return the display name of the requester."""
        return obj.user.get_display_name()

    @staticmethod
    def resolve_user_email(obj: models.WhitelistRequest) -> str:
        """Return the email of the requester."""
        return obj.user.email

    @staticmethod
    def resolve_matched_entries_count(obj: models.WhitelistRequest) -> int:
        """Return the count of matched blacklist entries."""
        return obj.matched_blacklist_entries.count()

    @staticmethod
    def resolve_decided_by_name(obj: models.WhitelistRequest) -> str | None:
        """Return the display name of who decided on the request."""
        if obj.decided_by:
            return obj.decided_by.get_display_name()
        return None


class WhitelistRequestCreateSchema(Schema):
    """Schema for users to request whitelisting."""

    message: str = Field("", description="Explanation for why you should be whitelisted")


class WhitelistEntrySchema(ModelSchema):
    """Schema for retrieving a whitelist entry (an APPROVED WhitelistRequest)."""

    user_id: UUID
    user_display_name: str
    user_email: str
    approved_by_name: str | None = None
    matched_entries_count: int

    class Meta:
        model = models.WhitelistRequest
        fields = ["id", "created_at", "decided_at"]

    @staticmethod
    def resolve_user_id(obj: models.WhitelistRequest) -> UUID:
        """Return the whitelisted user's ID."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.WhitelistRequest) -> str:
        """Return the display name of the whitelisted user."""
        return obj.user.get_display_name()

    @staticmethod
    def resolve_user_email(obj: models.WhitelistRequest) -> str:
        """Return the email of the whitelisted user."""
        return obj.user.email

    @staticmethod
    def resolve_approved_by_name(obj: models.WhitelistRequest) -> str | None:
        """Return the display name of who approved the whitelist entry."""
        if obj.decided_by:
            return obj.decided_by.get_display_name()
        return None

    @staticmethod
    def resolve_matched_entries_count(obj: models.WhitelistRequest) -> int:
        """Return the count of matched blacklist entries."""
        return obj.matched_blacklist_entries.count()
