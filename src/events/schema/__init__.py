"""Events schema package.

This package contains all schema definitions for the events app,
organized into modules that mirror the models package structure.

All schemas are re-exported here for backward compatibility.
"""

# Mixins and utilities
from .mixins import (
    CityBaseMixin,
    CityEditMixin,
    CityRetrieveMixin,
    SocialMediaSchemaEditMixin,
    SocialMediaSchemaRetrieveMixin,
    TaggableSchemaMixin,
    _SOCIAL_MEDIA_FIELDS,
    _SOCIAL_MEDIA_PATTERNS,
    _validate_social_media_url,
    ensure_url,
)

# Organization schemas
from .organization import (
    ApproveMembershipRequestSchema,
    MemberAddSchema,
    MembershipTierCreateSchema,
    MembershipTierSchema,
    MembershipTierUpdateSchema,
    MinimalOrganizationMemberSchema,
    MinimalOrganizationSchema,
    OrganizationAdminDetailSchema,
    OrganizationCreateSchema,
    OrganizationEditSchema,
    OrganizationInListSchema,
    OrganizationMemberSchema,
    OrganizationMembershipRequestCreateSchema,
    OrganizationMembershipRequestRetrieve,
    OrganizationMemberUpdateSchema,
    OrganizationPermissionsSchema,
    OrganizationRetrieveSchema,
    OrganizationStaffSchema,
    OrganizationTokenBaseSchema,
    OrganizationTokenCreateSchema,
    OrganizationTokenSchema,
    OrganizationTokenUpdateSchema,
    StaffAddSchema,
    VerifyOrganizationContactEmailJWTPayloadSchema,
)

# Event series schemas
from .event_series import (
    EventSeriesEditSchema,
    EventSeriesInListSchema,
    EventSeriesRetrieveSchema,
    MinimalEventSeriesSchema,
)

# Venue schemas
from .venue import (
    Coordinate2D,
    MinimalSeatSchema,
    PolygonShape,
    SectorAvailabilitySchema,
    VenueAvailabilitySchema,
    VenueCreateSchema,
    VenueDetailSchema,
    VenueSchema,
    VenueSeatBulkCreateSchema,
    VenueSeatBulkDeleteSchema,
    VenueSeatBulkUpdateItemSchema,
    VenueSeatBulkUpdateSchema,
    VenueSeatInputSchema,
    VenueSeatSchema,
    VenueSeatUpdateSchema,
    VenueSectorCreateSchema,
    VenueSectorSchema,
    VenueSectorUpdateSchema,
    VenueSectorWithSeatsSchema,
    VenueUpdateSchema,
    VenueWithSeatsSchema,
    point_in_polygon,
)

# Event schemas
from .event import (
    AttendeeSchema,
    EventBaseSchema,
    EventCreateSchema,
    EventDetailSchema,
    EventDuplicateSchema,
    EventEditSchema,
    EventEditSlugSchema,
    EventInListSchema,
    MinimalEventSchema,
    SlugString,
    TagUpdateSchema,
)

# Ticket and payment schemas
from .ticket import (
    AdminTicketSchema,
    BatchCheckoutPayload,
    BatchCheckoutPWYCPayload,
    BatchCheckoutResponse,
    CheckInRequestSchema,
    CheckInResponseSchema,
    Currencies,
    GuestActionConfirmSchema,
    GuestActionPayload,
    GuestActionResponseSchema,
    GuestBatchCheckoutPayload,
    GuestBatchCheckoutPWYCPayload,
    GuestCheckoutResponseSchema,
    GuestPWYCCheckoutSchema,
    GuestRSVPJWTPayloadSchema,
    GuestTicketItemPayload,
    GuestTicketJWTPayloadSchema,
    GuestUserDataSchema,
    MinimalPaymentSchema,
    PaymentSchema,
    PWYCCheckoutPayloadSchema,
    StripeAccountStatusSchema,
    StripeCheckoutSessionSchema,
    StripeOnboardingLinkSchema,
    TicketPurchaseItem,
    TicketTierCreateSchema,
    TicketTierDetailSchema,
    TicketTierPriceValidationMixin,
    TicketTierSchema,
    TicketTierUpdateSchema,
    UserTicketSchema,
)

# RSVP schemas
from .rsvp import (
    EventRSVPSchema,
    EventUserStatusResponse,
    RSVPCreateSchema,
    RSVPDetailSchema,
    RSVPUpdateSchema,
    UserRSVPSchema,
    WaitlistEntrySchema,
)

# Invitation schemas
from .invitation import (
    CombinedInvitationListSchema,
    DirectInvitationCreateSchema,
    DirectInvitationResponseSchema,
    EventInvitationListSchema,
    EventInvitationRequestCreateSchema,
    EventInvitationRequestInternalSchema,
    EventInvitationRequestSchema,
    EventJWTInvitationTier,
    EventTokenBaseSchema,
    EventTokenCreateSchema,
    EventTokenSchema,
    EventTokenUpdateSchema,
    InvitationBaseSchema,
    InvitationSchema,
    MyEventInvitationSchema,
    PendingEventInvitationListSchema,
)

# Potluck schemas
from .potluck import (
    PotluckItemCreateSchema,
    PotluckItemRetrieveSchema,
)

# Questionnaire schemas
from .questionnaire import (
    BaseOrganizationQuestionnaireSchema,
    EventAssignmentSchema,
    EventSeriesAssignmentSchema,
    OrganizationQuestionnaireCreateSchema,
    OrganizationQuestionnaireFieldsMixin,
    OrganizationQuestionnaireInListSchema,
    OrganizationQuestionnaireSchema,
    OrganizationQuestionnaireUpdateSchema,
)

# Misc schemas
from .misc import (
    AdditionalResourceCreateSchema,
    AdditionalResourceSchema,
    AdditionalResourceUpdateSchema,
)

# Preferences schemas
from .preferences import (
    DEFAULT_VISIBILITY_PREFERENCE,
    GeneralUserPreferencesSchema,
    GeneralUserPreferencesUpdateSchema,
)

# Blacklist schemas
from .blacklist import (
    BlacklistCreateSchema,
    BlacklistEntrySchema,
    BlacklistUpdateSchema,
    WhitelistEntrySchema,
    WhitelistRequestCreateSchema,
    WhitelistRequestSchema,
)

# Dietary schemas
from .dietary import (
    AggregatedDietaryPreferenceSchema,
    AggregatedDietaryRestrictionSchema,
    EventDietarySummarySchema,
)

__all__ = [
    # Mixins and utilities
    "CityBaseMixin",
    "CityEditMixin",
    "CityRetrieveMixin",
    "SocialMediaSchemaEditMixin",
    "SocialMediaSchemaRetrieveMixin",
    "TaggableSchemaMixin",
    "_SOCIAL_MEDIA_FIELDS",
    "_SOCIAL_MEDIA_PATTERNS",
    "_validate_social_media_url",
    "ensure_url",
    # Organization
    "ApproveMembershipRequestSchema",
    "MemberAddSchema",
    "MembershipTierCreateSchema",
    "MembershipTierSchema",
    "MembershipTierUpdateSchema",
    "MinimalOrganizationMemberSchema",
    "MinimalOrganizationSchema",
    "OrganizationAdminDetailSchema",
    "OrganizationCreateSchema",
    "OrganizationEditSchema",
    "OrganizationInListSchema",
    "OrganizationMemberSchema",
    "OrganizationMembershipRequestCreateSchema",
    "OrganizationMembershipRequestRetrieve",
    "OrganizationMemberUpdateSchema",
    "OrganizationPermissionsSchema",
    "OrganizationRetrieveSchema",
    "OrganizationStaffSchema",
    "OrganizationTokenBaseSchema",
    "OrganizationTokenCreateSchema",
    "OrganizationTokenSchema",
    "OrganizationTokenUpdateSchema",
    "StaffAddSchema",
    "VerifyOrganizationContactEmailJWTPayloadSchema",
    # Event series
    "EventSeriesEditSchema",
    "EventSeriesInListSchema",
    "EventSeriesRetrieveSchema",
    "MinimalEventSeriesSchema",
    # Venue
    "Coordinate2D",
    "MinimalSeatSchema",
    "PolygonShape",
    "SectorAvailabilitySchema",
    "VenueAvailabilitySchema",
    "VenueCreateSchema",
    "VenueDetailSchema",
    "VenueSchema",
    "VenueSeatBulkCreateSchema",
    "VenueSeatBulkDeleteSchema",
    "VenueSeatBulkUpdateItemSchema",
    "VenueSeatBulkUpdateSchema",
    "VenueSeatInputSchema",
    "VenueSeatSchema",
    "VenueSeatUpdateSchema",
    "VenueSectorCreateSchema",
    "VenueSectorSchema",
    "VenueSectorUpdateSchema",
    "VenueSectorWithSeatsSchema",
    "VenueUpdateSchema",
    "VenueWithSeatsSchema",
    "point_in_polygon",
    # Event
    "AttendeeSchema",
    "EventBaseSchema",
    "EventCreateSchema",
    "EventDetailSchema",
    "EventDuplicateSchema",
    "EventEditSchema",
    "EventEditSlugSchema",
    "EventInListSchema",
    "MinimalEventSchema",
    "SlugString",
    "TagUpdateSchema",
    # Ticket and payment
    "AdminTicketSchema",
    "BatchCheckoutPayload",
    "BatchCheckoutPWYCPayload",
    "BatchCheckoutResponse",
    "CheckInRequestSchema",
    "CheckInResponseSchema",
    "Currencies",
    "GuestActionConfirmSchema",
    "GuestActionPayload",
    "GuestActionResponseSchema",
    "GuestBatchCheckoutPayload",
    "GuestBatchCheckoutPWYCPayload",
    "GuestCheckoutResponseSchema",
    "GuestPWYCCheckoutSchema",
    "GuestRSVPJWTPayloadSchema",
    "GuestTicketItemPayload",
    "GuestTicketJWTPayloadSchema",
    "GuestUserDataSchema",
    "MinimalPaymentSchema",
    "PaymentSchema",
    "PWYCCheckoutPayloadSchema",
    "StripeAccountStatusSchema",
    "StripeCheckoutSessionSchema",
    "StripeOnboardingLinkSchema",
    "TicketPurchaseItem",
    "TicketTierCreateSchema",
    "TicketTierDetailSchema",
    "TicketTierPriceValidationMixin",
    "TicketTierSchema",
    "TicketTierUpdateSchema",
    "UserTicketSchema",
    # RSVP
    "EventRSVPSchema",
    "EventUserStatusResponse",
    "RSVPCreateSchema",
    "RSVPDetailSchema",
    "RSVPUpdateSchema",
    "UserRSVPSchema",
    "WaitlistEntrySchema",
    # Invitation
    "CombinedInvitationListSchema",
    "DirectInvitationCreateSchema",
    "DirectInvitationResponseSchema",
    "EventInvitationListSchema",
    "EventInvitationRequestCreateSchema",
    "EventInvitationRequestInternalSchema",
    "EventInvitationRequestSchema",
    "EventJWTInvitationTier",
    "EventTokenBaseSchema",
    "EventTokenCreateSchema",
    "EventTokenSchema",
    "EventTokenUpdateSchema",
    "InvitationBaseSchema",
    "InvitationSchema",
    "MyEventInvitationSchema",
    "PendingEventInvitationListSchema",
    # Potluck
    "PotluckItemCreateSchema",
    "PotluckItemRetrieveSchema",
    # Questionnaire
    "BaseOrganizationQuestionnaireSchema",
    "EventAssignmentSchema",
    "EventSeriesAssignmentSchema",
    "OrganizationQuestionnaireCreateSchema",
    "OrganizationQuestionnaireFieldsMixin",
    "OrganizationQuestionnaireInListSchema",
    "OrganizationQuestionnaireSchema",
    "OrganizationQuestionnaireUpdateSchema",
    # Misc
    "AdditionalResourceCreateSchema",
    "AdditionalResourceSchema",
    "AdditionalResourceUpdateSchema",
    # Preferences
    "DEFAULT_VISIBILITY_PREFERENCE",
    "GeneralUserPreferencesSchema",
    "GeneralUserPreferencesUpdateSchema",
    # Blacklist
    "BlacklistCreateSchema",
    "BlacklistEntrySchema",
    "BlacklistUpdateSchema",
    "WhitelistEntrySchema",
    "WhitelistRequestCreateSchema",
    "WhitelistRequestSchema",
    # Dietary
    "AggregatedDietaryPreferenceSchema",
    "AggregatedDietaryRestrictionSchema",
    "EventDietarySummarySchema",
]
