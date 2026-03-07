"""Events schema package.

This package contains all schema definitions for the events app,
organized into modules that mirror the models package structure.

All schemas are re-exported here for backward compatibility.
"""

# Mixins and utilities
from .mixins import (
    CityEditMixin,
    SocialMediaSchemaEditMixin,
    SocialMediaSchemaRetrieveMixin,
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
    EventCreateSchema,
    EventDetailSchema,
    EventDuplicateSchema,
    EventEditSchema,
    EventEditSlugSchema,
    EventInListSchema,
    MinimalEventSchema,
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
    ConfirmPaymentSchema,
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
    PaymentSchema,
    PWYCCheckoutPayloadSchema,
    ReorderSchema,
    StripeAccountStatusSchema,
    StripeCheckoutSessionSchema,
    StripeOnboardingLinkSchema,
    TicketPurchaseItem,
    TicketTierCreateSchema,
    TicketTierDetailSchema,
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
    TierRemainingTicketsSchema,
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
    EventAssignmentSchema,
    EventSeriesAssignmentSchema,
    McOptionStatSchema,
    McQuestionStatSchema,
    OrganizationQuestionnaireCreateSchema,
    OrganizationQuestionnaireInListSchema,
    OrganizationQuestionnaireSchema,
    OrganizationQuestionnaireUpdateSchema,
    QuestionnaireSummarySchema,
    ScoreStatsSchema,
    StatusBreakdownSchema,
)

# Misc schemas
from .misc import (
    AdditionalResourceCreateSchema,
    AdditionalResourceSchema,
    AdditionalResourceUpdateSchema,
)

# Preferences schemas
from .preferences import (
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

# Pronoun schemas
from .pronouns import (
    EventPronounDistributionSchema,
    PronounCountSchema,
)

# Follow schemas
from .follow import (
    EventSeriesFollowCreateSchema,
    EventSeriesFollowSchema,
    EventSeriesFollowStatusSchema,
    EventSeriesFollowUpdateSchema,
    MinimalEventSeriesFollowSchema,
    MinimalOrganizationFollowSchema,
    OrganizationFollowCreateSchema,
    OrganizationFollowSchema,
    OrganizationFollowStatusSchema,
    OrganizationFollowUpdateSchema,
)

# Discount code schemas
from .discount_code import (
    DiscountCodeCreateSchema,
    DiscountCodeSchema,
    DiscountCodeUpdateSchema,
    DiscountCodeValidationResponse,
    DiscountCodeValidationSchema,
)

# Export schemas
from .export import (
    FileExportSchema,
)

# Announcement schemas
from .announcement import (
    AnnouncementCreateSchema,
    AnnouncementListSchema,
    AnnouncementPublicSchema,
    AnnouncementSchema,
    AnnouncementUpdateSchema,
    RecipientCountSchema,
)

__all__ = [
    # Mixins and utilities
    "CityEditMixin",
    "SocialMediaSchemaEditMixin",
    "SocialMediaSchemaRetrieveMixin",
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
    "EventCreateSchema",
    "EventDetailSchema",
    "EventDuplicateSchema",
    "EventEditSchema",
    "EventEditSlugSchema",
    "EventInListSchema",
    "MinimalEventSchema",
    "TagUpdateSchema",
    # Ticket and payment
    "AdminTicketSchema",
    "BatchCheckoutPayload",
    "BatchCheckoutPWYCPayload",
    "BatchCheckoutResponse",
    "CheckInRequestSchema",
    "CheckInResponseSchema",
    "ConfirmPaymentSchema",
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
    "PaymentSchema",
    "PWYCCheckoutPayloadSchema",
    "ReorderSchema",
    "StripeAccountStatusSchema",
    "StripeCheckoutSessionSchema",
    "StripeOnboardingLinkSchema",
    "TicketPurchaseItem",
    "TicketTierCreateSchema",
    "TicketTierDetailSchema",
    "TicketTierSchema",
    "TicketTierUpdateSchema",
    "UserTicketSchema",
    # RSVP
    "EventRSVPSchema",
    "EventUserStatusResponse",
    "RSVPCreateSchema",
    "RSVPDetailSchema",
    "RSVPUpdateSchema",
    "TierRemainingTicketsSchema",
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
    "EventAssignmentSchema",
    "EventSeriesAssignmentSchema",
    "McOptionStatSchema",
    "McQuestionStatSchema",
    "OrganizationQuestionnaireCreateSchema",
    "OrganizationQuestionnaireInListSchema",
    "OrganizationQuestionnaireSchema",
    "OrganizationQuestionnaireUpdateSchema",
    "QuestionnaireSummarySchema",
    "ScoreStatsSchema",
    "StatusBreakdownSchema",
    # Misc
    "AdditionalResourceCreateSchema",
    "AdditionalResourceSchema",
    "AdditionalResourceUpdateSchema",
    # Preferences
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
    # Pronouns
    "EventPronounDistributionSchema",
    "PronounCountSchema",
    # Follow
    "EventSeriesFollowCreateSchema",
    "EventSeriesFollowSchema",
    "EventSeriesFollowStatusSchema",
    "EventSeriesFollowUpdateSchema",
    "MinimalEventSeriesFollowSchema",
    "MinimalOrganizationFollowSchema",
    "OrganizationFollowCreateSchema",
    "OrganizationFollowSchema",
    "OrganizationFollowStatusSchema",
    "OrganizationFollowUpdateSchema",
    # Discount codes
    "DiscountCodeCreateSchema",
    "DiscountCodeSchema",
    "DiscountCodeUpdateSchema",
    "DiscountCodeValidationResponse",
    "DiscountCodeValidationSchema",
    # Export
    "FileExportSchema",
    # Announcement
    "AnnouncementCreateSchema",
    "AnnouncementListSchema",
    "AnnouncementPublicSchema",
    "AnnouncementSchema",
    "AnnouncementUpdateSchema",
    "RecipientCountSchema",
]
