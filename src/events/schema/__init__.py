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

# Application schemas
from .application import (
    ApplyRequestSchema,
    ApplyResponseSchema,
    JoinEligibilityQuery,
    MembershipApplicationSchema,
    MembershipEligibilitySchema,
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

# Waitlist (advanced) schemas
from .waitlist import (
    WaitlistOfferCreateSchema,
    WaitlistOfferReactivateSchema,
    WaitlistOfferSchema,
    WaitlistSettingsSchema,
    WaitlistSettingsUpdateSchema,
)

__all__ = [
    # Misc
    "AdditionalResourceCreateSchema",
    "AdditionalResourceSchema",
    "AdditionalResourceUpdateSchema",
    # Ticket and payment
    "AdminCancelTicketSchema",
    "AdminRefundTicketSchema",
    "AdminTicketSchema",
    # Dietary
    "AggregatedDietaryPreferenceSchema",
    "AggregatedDietaryRestrictionSchema",
    # Announcement
    "AnnouncementCreateSchema",
    "AnnouncementListSchema",
    "AnnouncementPublicSchema",
    "AnnouncementScheduleSchema",
    "AnnouncementSchema",
    "AnnouncementUpdateSchema",
    # Application
    "ApplyRequestSchema",
    "ApplyResponseSchema",
    # Organization
    "ApproveMembershipRequestSchema",
    # Invoice
    "AttendeeInvoiceCreditNoteSchema",
    "AttendeeInvoiceDetailSchema",
    "AttendeeInvoiceSchema",
    # Event
    "AttendeeSchema",
    "BatchCheckoutPWYCPayload",
    "BatchCheckoutPayload",
    "BatchCheckoutResponse",
    # Blacklist
    "BlacklistCreateSchema",
    "BlacklistEntrySchema",
    "BlacklistUpdateSchema",
    "BuyerBillingInfoSchema",
    "CancelOccurrenceSchema",
    # Subscriptions
    "CancelSubscriptionSchema",
    "CancellationBlockedErrorSchema",
    "CancellationPreviewSchema",
    "CheckInRequestSchema",
    "CheckInResponseSchema",
    "CheckoutSessionResponse",
    # Mixins and utilities
    "CityEditMixin",
    # Invitation
    "CombinedInvitationListSchema",
    "ConfirmPaymentSchema",
    # Venue
    "Coordinate2D",
    "Currencies",
    # Financials
    "CurrencyFinancialsSchema",
    "DirectInvitationCreateSchema",
    "DirectInvitationResponseSchema",
    # Discount codes
    "DiscountCodeCreateSchema",
    "DiscountCodeDeleteResponse",
    "DiscountCodeSchema",
    "DiscountCodeUpdateSchema",
    "DiscountCodeValidationResponse",
    "DiscountCodeValidationSchema",
    # Questionnaire
    "EventAssignmentSchema",
    # Bookmark
    "EventBookmarkSchema",
    "EventCreateSchema",
    "EventDetailSchema",
    "EventDietarySummarySchema",
    "EventDuplicateSchema",
    "EventEditSchema",
    "EventEditSlugSchema",
    "EventFinancialsSchema",
    "EventInListSchema",
    "EventInvitationListSchema",
    "EventInvitationRequestCreateSchema",
    "EventInvitationRequestInternalSchema",
    "EventInvitationRequestSchema",
    # Pronouns
    "EventPronounDistributionSchema",
    # RSVP
    "EventRSVPSchema",
    "EventScheduleSessionSchema",
    "EventScheduleUpdateSchema",
    "EventSeriesAssignmentSchema",
    "EventSeriesDriftSchema",
    # Event series
    "EventSeriesEditSchema",
    # Follow
    "EventSeriesFollowCreateSchema",
    "EventSeriesFollowSchema",
    "EventSeriesFollowStatusSchema",
    "EventSeriesFollowUpdateSchema",
    "EventSeriesInListSchema",
    "EventSeriesRecurrenceDetailSchema",
    "EventSeriesRecurrenceUpdateSchema",
    "EventSeriesRetrieveSchema",
    "EventStatusUpdatePayload",
    "EventTokenCreateSchema",
    "EventTokenRejectionSchema",
    "EventTokenSchema",
    "EventTokenUpdateSchema",
    "EventUserStatusResponse",
    # Export
    "FileExportSchema",
    # Preferences
    "GeneralUserPreferencesSchema",
    "GeneralUserPreferencesUpdateSchema",
    "GenerateSeriesEventsSchema",
    "GuestActionConfirmSchema",
    "GuestActionPayload",
    "GuestActionResponseSchema",
    "GuestBatchCheckoutPWYCPayload",
    "GuestBatchCheckoutPayload",
    "GuestCheckoutResponseSchema",
    "GuestPWYCCheckoutSchema",
    "GuestRSVPJWTPayloadSchema",
    "GuestRSVPRequestSchema",
    "GuestTicketItemPayload",
    "GuestTicketJWTPayloadSchema",
    "GuestUserDataSchema",
    # Series pass
    "HeldSeriesPassAdminSchema",
    "HeldSeriesPassCancelSchema",
    "HeldSeriesPassSchema",
    "InvitationBaseSchema",
    "InvitationSchema",
    "InvoiceDownloadURLSchema",
    "InvoiceLineItemSchema",
    "InvoicingModeUpdateSchema",
    "JoinEligibilityQuery",
    "McOptionStatSchema",
    "McQuestionStatSchema",
    "MemberAddSchema",
    "MembershipApplicationSchema",
    "MembershipEligibilitySchema",
    "MembershipPaymentSchema",
    "MembershipTierCreateSchema",
    "MembershipTierSchema",
    "MembershipTierUpdateSchema",
    "MinimalEventSchema",
    "MinimalEventSeriesFollowSchema",
    "MinimalEventSeriesSchema",
    "MinimalOrganizationFollowSchema",
    "MinimalOrganizationMemberSchema",
    "MinimalOrganizationSchema",
    "MinimalSeatSchema",
    "MyEventInvitationSchema",
    "MyMembershipSchema",
    "MySubscriptionSchema",
    "OrganizationAdminDetailSchema",
    "OrganizationBillingInfoSchema",
    "OrganizationBillingInfoUpdateSchema",
    "OrganizationContactMessageCreateSchema",
    "OrganizationContactMessageSchema",
    "OrganizationCreateSchema",
    "OrganizationEditSchema",
    "OrganizationFinancialsSchema",
    "OrganizationFollowCreateSchema",
    "OrganizationFollowSchema",
    "OrganizationFollowStatusSchema",
    "OrganizationFollowUpdateSchema",
    "OrganizationInListSchema",
    "OrganizationMemberSchema",
    "OrganizationMemberUpdateSchema",
    "OrganizationMembershipRequestCreateSchema",
    "OrganizationMembershipRequestRetrieve",
    "OrganizationPermissionsSchema",
    "OrganizationQuestionnaireCreateSchema",
    "OrganizationQuestionnaireInListSchema",
    "OrganizationQuestionnaireSchema",
    "OrganizationQuestionnaireUpdateSchema",
    "OrganizationRetrieveSchema",
    "OrganizationStaffSchema",
    "OrganizationTokenCreateSchema",
    "OrganizationTokenRejectionSchema",
    "OrganizationTokenSchema",
    "OrganizationTokenUpdateSchema",
    "PWYCCheckoutPayloadSchema",
    "PaymentRecordSchema",
    "PaymentSchema",
    "PendingEventInvitationListSchema",
    "PlanCreateSchema",
    "PlanSchema",
    "PlanUpdateSchema",
    "PlatformFeeCreditNoteSchema",
    "PlatformFeeInvoiceSchema",
    "PolygonShape",
    # Potluck
    "PotluckItemCreateSchema",
    "PotluckItemRetrieveSchema",
    "PronounCountSchema",
    "QuestionnaireDuplicateSchema",
    "QuestionnaireSummarySchema",
    "RSVPCreateSchema",
    "RSVPDetailSchema",
    "RSVPNoteSchema",
    "RSVPUpdateSchema",
    "RateBucketSchema",
    "RecipientCountSchema",
    # Recurrence
    "RecurrenceRuleCreateSchema",
    "RecurrenceRuleSchema",
    "RecurrenceRuleUpdateSchema",
    "RecurringEventCreateSchema",
    "RefundPolicySchema",
    "RefundPolicyTierSchema",
    "RefundSchema",
    "RefundWindowSchema",
    "ReorderSchema",
    # Revenue report
    "RevenueReportRequestSchema",
    "ScoreStatsSchema",
    "SectorAvailabilitySchema",
    "SeriesPassAdminSchema",
    "SeriesPassCheckoutResponseSchema",
    "SeriesPassCreateSchema",
    "SeriesPassLinkInputSchema",
    "SeriesPassQuoteSchema",
    "SeriesPassSchema",
    "SeriesPassSeriesInfoSchema",
    "SeriesPassTierLinkAdminSchema",
    "SeriesPassTierLinkInputSchema",
    "SeriesPassUpdateSchema",
    "SocialMediaSchemaEditMixin",
    "SocialMediaSchemaRetrieveMixin",
    "StaffAddSchema",
    "StatusBreakdownSchema",
    "StripeAccountStatusSchema",
    "StripeCheckoutSessionSchema",
    "StripeOnboardingLinkSchema",
    "SubscriptionCreateSchema",
    "SubscriptionSchema",
    "TagUpdateSchema",
    "TemplateEditSchema",
    "TicketCancellationRequestSchema",
    "TicketCancellationResponseSchema",
    "TicketDiscountCodeSchema",
    "TicketPurchaseItem",
    "TicketSeriesPassSchema",
    "TicketTierCreateSchema",
    "TicketTierDetailSchema",
    "TicketTierSchema",
    "TicketTierUpdateSchema",
    "TierRemainingTicketsSchema",
    "UpdateAttendeeInvoiceSchema",
    "UserRSVPSchema",
    "UserTicketSchema",
    "VATIdUpdateSchema",
    "VATPreviewItemSchema",
    "VATPreviewLineItemSchema",
    "VATPreviewRequestSchema",
    "VATPreviewResponseSchema",
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
    "VerifyOrganizationContactEmailJWTPayloadSchema",
    "WaitlistEntrySchema",
    # Waitlist (advanced)
    "WaitlistOfferCreateSchema",
    "WaitlistOfferReactivateSchema",
    "WaitlistOfferSchema",
    "WaitlistSettingsSchema",
    "WaitlistSettingsUpdateSchema",
    "WhitelistEntrySchema",
    "WhitelistRequestCreateSchema",
    "WhitelistRequestSchema",
    "point_in_polygon",
]
