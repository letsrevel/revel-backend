"""Events schema package.

This package contains all schema definitions for the events app,
organized into modules that mirror the models package structure.

All schemas are re-exported here for backward compatibility.
"""

# Mixins and utilities
# Announcement schemas
from .announcement import (
    AnnouncementCreateSchema,
    AnnouncementListSchema,
    AnnouncementPublicSchema,
    AnnouncementScheduleSchema,
    AnnouncementSchema,
    AnnouncementUpdateSchema,
    RecipientCountSchema,
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
from .bookmark import EventBookmarkSchema

# Dietary schemas
from .dietary import (
    AggregatedDietaryPreferenceSchema,
    AggregatedDietaryRestrictionSchema,
    EventDietarySummarySchema,
)

# Discount code schemas
from .discount_code import (
    DiscountCodeCreateSchema,
    DiscountCodeDeleteResponse,
    DiscountCodeSchema,
    DiscountCodeUpdateSchema,
    DiscountCodeValidationResponse,
    DiscountCodeValidationSchema,
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
    EventScheduleSessionSchema,
    EventScheduleUpdateSchema,
    EventStatusUpdatePayload,
    MinimalEventSchema,
    SeriesPassLinkInputSchema,
    TagUpdateSchema,
)

# Event series schemas
from .event_series import (
    EventSeriesEditSchema,
    EventSeriesInListSchema,
    EventSeriesRetrieveSchema,
    MinimalEventSeriesSchema,
)

# Export schemas
from .export import (
    FileExportSchema,
)

# Financials schemas
from .financials import (
    CurrencyFinancialsSchema,
    EventFinancialsSchema,
    OrganizationFinancialsSchema,
    RateBucketSchema,
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
    EventTokenRejectionSchema,
    EventTokenSchema,
    EventTokenUpdateSchema,
    InvitationBaseSchema,
    InvitationSchema,
    MyEventInvitationSchema,
    PendingEventInvitationListSchema,
)

# Invoice schemas
from .invoice import (
    AttendeeInvoiceCreditNoteSchema,
    AttendeeInvoiceDetailSchema,
    AttendeeInvoiceSchema,
    InvoiceDownloadURLSchema,
    InvoiceLineItemSchema,
    InvoicingModeUpdateSchema,
    PlatformFeeCreditNoteSchema,
    PlatformFeeInvoiceSchema,
    UpdateAttendeeInvoiceSchema,
)

# Misc schemas
from .misc import (
    AdditionalResourceCreateSchema,
    AdditionalResourceSchema,
    AdditionalResourceUpdateSchema,
)
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
    OrganizationBillingInfoSchema,
    OrganizationBillingInfoUpdateSchema,
    OrganizationContactMessageCreateSchema,
    OrganizationContactMessageSchema,
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
    OrganizationTokenRejectionSchema,
    OrganizationTokenSchema,
    OrganizationTokenUpdateSchema,
    StaffAddSchema,
    VATIdUpdateSchema,
    VerifyOrganizationContactEmailJWTPayloadSchema,
)

# Potluck schemas
from .potluck import (
    PotluckItemCreateSchema,
    PotluckItemRetrieveSchema,
)

# Preferences schemas
from .preferences import (
    GeneralUserPreferencesSchema,
    GeneralUserPreferencesUpdateSchema,
)

# Pronoun schemas
from .pronouns import (
    EventPronounDistributionSchema,
    PronounCountSchema,
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
    QuestionnaireDuplicateSchema,
    QuestionnaireSummarySchema,
    ScoreStatsSchema,
    StatusBreakdownSchema,
)

# Recurrence rule schemas
from .recurrence_rule import (
    RecurrenceRuleCreateSchema,
    RecurrenceRuleSchema,
    RecurrenceRuleUpdateSchema,
)

# Recurring event schemas
from .recurring_event import (
    CancelOccurrenceSchema,
    EventSeriesDriftSchema,
    EventSeriesRecurrenceDetailSchema,
    EventSeriesRecurrenceUpdateSchema,
    GenerateSeriesEventsSchema,
    RecurringEventCreateSchema,
    TemplateEditSchema,
)

# Revenue report schemas
from .revenue_report import RevenueReportRequestSchema

# RSVP schemas
from .rsvp import (
    EventRSVPSchema,
    EventUserStatusResponse,
    GuestRSVPRequestSchema,
    RSVPCreateSchema,
    RSVPDetailSchema,
    RSVPNoteSchema,
    RSVPUpdateSchema,
    TierRemainingTicketsSchema,
    UserRSVPSchema,
    WaitlistEntrySchema,
)

# Seating schemas
from .seating import (
    BestAvailableHoldRequest,
    BoxOfficeReseatRequest,
    BoxOfficeSellRequest,
    ChartSeatSchema,
    ChartSectorSchema,
    HoldResponseSchema,
    HoldSeatsRequest,
    ReleaseSeatsRequest,
    SeatingAvailabilitySchema,
    SeatOverrideItemSchema,
    SeatOverridesRequest,
    SeatOverridesResponse,
    StandingAvailabilitySchema,
    VenueChartSchema,
    ZoneAvailabilitySchema,
)

# Series pass schemas
from .series_pass import (
    HeldSeriesPassAdminSchema,
    HeldSeriesPassCancelSchema,
    HeldSeriesPassSchema,
    SeriesPassAdminSchema,
    SeriesPassCheckoutResponseSchema,
    SeriesPassCreateSchema,
    SeriesPassQuoteSchema,
    SeriesPassSchema,
    SeriesPassSeriesInfoSchema,
    SeriesPassTierLinkAdminSchema,
    SeriesPassTierLinkInputSchema,
    SeriesPassUpdateSchema,
)

# Subscription schemas
from .subscription import (
    CancelSubscriptionSchema,
    MyMembershipSchema,
    MySubscriptionSchema,
    PaymentRecordSchema,
    PlanCreateSchema,
    PlanSchema,
    PlanUpdateSchema,
    RefundSchema,
    SubscriptionCreateSchema,
    SubscriptionSchema,
)
from .subscription import (
    PaymentSchema as MembershipPaymentSchema,
)

# Ticket and payment schemas
from .ticket import (
    AdminCancelTicketSchema,
    AdminRefundTicketSchema,
    AdminTicketSchema,
    BatchCheckoutPayload,
    BatchCheckoutPWYCPayload,
    BatchCheckoutResponse,
    BuyerBillingInfoSchema,
    CancellationBlockedErrorSchema,
    CancellationPreviewSchema,
    CheckInRequestSchema,
    CheckInResponseSchema,
    CheckoutSessionResponse,
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
    RefundPolicySchema,
    RefundPolicyTierSchema,
    RefundWindowSchema,
    ReorderSchema,
    StripeAccountStatusSchema,
    StripeCheckoutSessionSchema,
    StripeOnboardingLinkSchema,
    TicketCancellationRequestSchema,
    TicketCancellationResponseSchema,
    TicketDiscountCodeSchema,
    TicketPurchaseItem,
    TicketSeriesPassSchema,
    TicketTierCreateSchema,
    TierCategoryPriceSchema,
    TierSeatPricingSchema,
    TicketTierDetailSchema,
    TicketTierSchema,
    TicketTierUpdateSchema,
    UserTicketSchema,
    VATPreviewItemSchema,
    VATPreviewLineItemSchema,
    VATPreviewRequestSchema,
    VATPreviewResponseSchema,
)

# Venue schemas
from .venue import (
    AffectedTierSchema,
    Coordinate2D,
    MinimalSeatSchema,
    PolygonShape,
    PriceCategoryCreateSchema,
    PriceCategorySchema,
    PriceCategoryUpdateSchema,
    SeatPaintResultSchema,
    SeatPriceChangeSchema,
    SectorAvailabilitySchema,
    TierPricingGapSchema,
    VenueAvailabilitySchema,
    VenueCreateSchema,
    VenueDetailSchema,
    VenueSchema,
    VenueSeatBulkCreateSchema,
    VenueSeatBulkDeleteSchema,
    VenueSeatBulkUpdateItemSchema,
    VenueSeatBulkUpdateSchema,
    VenueSeatInputSchema,
    VenueSeatPaintSchema,
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

# Waitlist (advanced) schemas
from .waitlist import (
    WaitlistOfferCreateSchema,
    WaitlistOfferReactivateSchema,
    WaitlistOfferSchema,
    WaitlistSettingsSchema,
    WaitlistSettingsUpdateSchema,
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
    "OrganizationContactMessageCreateSchema",
    "OrganizationContactMessageSchema",
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
    "OrganizationTokenRejectionSchema",
    "OrganizationTokenSchema",
    "OrganizationTokenUpdateSchema",
    "OrganizationBillingInfoSchema",
    "OrganizationBillingInfoUpdateSchema",
    "StaffAddSchema",
    "VATIdUpdateSchema",
    "VerifyOrganizationContactEmailJWTPayloadSchema",
    # Event series
    "EventSeriesEditSchema",
    "EventSeriesInListSchema",
    "EventSeriesRetrieveSchema",
    "MinimalEventSeriesSchema",
    # Venue
    "AffectedTierSchema",
    "Coordinate2D",
    "MinimalSeatSchema",
    "PolygonShape",
    "PriceCategoryCreateSchema",
    "PriceCategorySchema",
    "PriceCategoryUpdateSchema",
    "SeatPaintResultSchema",
    "SeatPriceChangeSchema",
    "SectorAvailabilitySchema",
    "TierPricingGapSchema",
    "VenueAvailabilitySchema",
    "VenueCreateSchema",
    "VenueDetailSchema",
    "VenueSchema",
    "VenueSeatBulkCreateSchema",
    "VenueSeatBulkDeleteSchema",
    "VenueSeatBulkUpdateItemSchema",
    "VenueSeatBulkUpdateSchema",
    "VenueSeatInputSchema",
    "VenueSeatPaintSchema",
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
    "EventScheduleSessionSchema",
    "EventScheduleUpdateSchema",
    "EventStatusUpdatePayload",
    "MinimalEventSchema",
    "SeriesPassLinkInputSchema",
    "TagUpdateSchema",
    # Ticket and payment
    "AdminCancelTicketSchema",
    "AdminRefundTicketSchema",
    "AdminTicketSchema",
    "BatchCheckoutPayload",
    "BatchCheckoutPWYCPayload",
    "BatchCheckoutResponse",
    "BuyerBillingInfoSchema",
    "CancellationBlockedErrorSchema",
    "CancellationPreviewSchema",
    "VATPreviewItemSchema",
    "VATPreviewLineItemSchema",
    "VATPreviewRequestSchema",
    "VATPreviewResponseSchema",
    "CheckInRequestSchema",
    "CheckInResponseSchema",
    "CheckoutSessionResponse",
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
    "RefundPolicySchema",
    "RefundPolicyTierSchema",
    "RefundWindowSchema",
    "ReorderSchema",
    "StripeAccountStatusSchema",
    "StripeCheckoutSessionSchema",
    "StripeOnboardingLinkSchema",
    "TicketCancellationRequestSchema",
    "TicketCancellationResponseSchema",
    "TicketDiscountCodeSchema",
    "TicketPurchaseItem",
    "TicketSeriesPassSchema",
    "TicketTierCreateSchema",
    "TicketTierDetailSchema",
    "TicketTierSchema",
    "TicketTierUpdateSchema",
    "TierCategoryPriceSchema",
    "TierSeatPricingSchema",
    "UserTicketSchema",
    # Series pass
    "HeldSeriesPassAdminSchema",
    "HeldSeriesPassCancelSchema",
    "HeldSeriesPassSchema",
    "SeriesPassAdminSchema",
    "SeriesPassCheckoutResponseSchema",
    "SeriesPassCreateSchema",
    "SeriesPassQuoteSchema",
    "SeriesPassSchema",
    "SeriesPassSeriesInfoSchema",
    "SeriesPassTierLinkAdminSchema",
    "SeriesPassTierLinkInputSchema",
    "SeriesPassUpdateSchema",
    # RSVP
    "EventRSVPSchema",
    "EventUserStatusResponse",
    "GuestRSVPRequestSchema",
    "RSVPCreateSchema",
    "RSVPDetailSchema",
    "RSVPNoteSchema",
    "RSVPUpdateSchema",
    "TierRemainingTicketsSchema",
    "UserRSVPSchema",
    "WaitlistEntrySchema",
    # Seating
    "BestAvailableHoldRequest",
    "BoxOfficeReseatRequest",
    "BoxOfficeSellRequest",
    "ChartSeatSchema",
    "ChartSectorSchema",
    "HoldResponseSchema",
    "HoldSeatsRequest",
    "ReleaseSeatsRequest",
    "SeatOverrideItemSchema",
    "SeatOverridesRequest",
    "SeatOverridesResponse",
    "SeatingAvailabilitySchema",
    "StandingAvailabilitySchema",
    "VenueChartSchema",
    "ZoneAvailabilitySchema",
    # Invitation
    "CombinedInvitationListSchema",
    "DirectInvitationCreateSchema",
    "DirectInvitationResponseSchema",
    "EventInvitationListSchema",
    "EventInvitationRequestCreateSchema",
    "EventInvitationRequestInternalSchema",
    "EventInvitationRequestSchema",
    "EventTokenCreateSchema",
    "EventTokenRejectionSchema",
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
    "QuestionnaireDuplicateSchema",
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
    # Bookmark
    "EventBookmarkSchema",
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
    "DiscountCodeDeleteResponse",
    "DiscountCodeSchema",
    "DiscountCodeUpdateSchema",
    "DiscountCodeValidationResponse",
    "DiscountCodeValidationSchema",
    # Invoice
    "AttendeeInvoiceCreditNoteSchema",
    "AttendeeInvoiceDetailSchema",
    "AttendeeInvoiceSchema",
    "InvoiceDownloadURLSchema",
    "InvoiceLineItemSchema",
    "InvoicingModeUpdateSchema",
    "PlatformFeeCreditNoteSchema",
    "PlatformFeeInvoiceSchema",
    "UpdateAttendeeInvoiceSchema",
    # Export
    "FileExportSchema",
    # Recurrence
    "RecurrenceRuleCreateSchema",
    "RecurrenceRuleSchema",
    "RecurrenceRuleUpdateSchema",
    "CancelOccurrenceSchema",
    "EventSeriesDriftSchema",
    "EventSeriesRecurrenceDetailSchema",
    "EventSeriesRecurrenceUpdateSchema",
    "GenerateSeriesEventsSchema",
    "RecurringEventCreateSchema",
    "TemplateEditSchema",
    # Subscriptions
    "CancelSubscriptionSchema",
    "MembershipPaymentSchema",
    "MyMembershipSchema",
    "MySubscriptionSchema",
    "PaymentRecordSchema",
    "PlanCreateSchema",
    "PlanSchema",
    "PlanUpdateSchema",
    "RefundSchema",
    "SubscriptionCreateSchema",
    "SubscriptionSchema",
    # Announcement
    "AnnouncementCreateSchema",
    "AnnouncementListSchema",
    "AnnouncementPublicSchema",
    "AnnouncementScheduleSchema",
    "AnnouncementSchema",
    "AnnouncementUpdateSchema",
    "RecipientCountSchema",
    # Waitlist (advanced)
    "WaitlistOfferCreateSchema",
    "WaitlistOfferReactivateSchema",
    "WaitlistOfferSchema",
    "WaitlistSettingsSchema",
    "WaitlistSettingsUpdateSchema",
    # Revenue report
    "RevenueReportRequestSchema",
    # Financials
    "CurrencyFinancialsSchema",
    "EventFinancialsSchema",
    "OrganizationFinancialsSchema",
    "RateBucketSchema",
]
