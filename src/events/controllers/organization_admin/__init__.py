"""Organization admin controllers package.

This package splits the organization admin endpoints into logical groupings
while preserving the original endpoint order.
"""

from .announcements import OrganizationAdminAnnouncementsController
from .blacklist import OrganizationAdminBlacklistController
from .core import OrganizationAdminCoreController
from .custom_domain import OrganizationAdminCustomDomainController
from .discount_codes import OrganizationAdminDiscountCodesController
from .member_invitations import OrganizationAdminMemberInvitationsController
from .member_register import OrganizationAdminMemberRegisterController
from .member_register_linking import OrganizationAdminMemberRegisterLinkingController
from .members import OrganizationAdminMembersController
from .membership_requests import OrganizationAdminMembershipRequestsController
from .recurring_events import OrganizationAdminRecurringEventsController
from .resources import OrganizationAdminResourcesController
from .revenue import OrganizationAdminRevenueController
from .subscriptions import OrganizationAdminSubscriptionsController
from .tickets import OrganizationAdminTicketsController
from .tokens import OrganizationAdminTokensController
from .user_lookup import OrganizationAdminUserLookupController
from .user_notes import OrganizationAdminUserNotesController
from .vat import OrganizationAdminVATController
from .venues import OrganizationAdminVenuesController
from .whitelist import OrganizationAdminWhitelistController

# Controllers in order to preserve original endpoint ordering
ORGANIZATION_ADMIN_CONTROLLERS: list[type] = [
    OrganizationAdminCoreController,
    OrganizationAdminTokensController,
    OrganizationAdminMembershipRequestsController,
    OrganizationAdminResourcesController,
    OrganizationAdminMembersController,
    OrganizationAdminVenuesController,
    OrganizationAdminBlacklistController,
    OrganizationAdminWhitelistController,
    OrganizationAdminAnnouncementsController,
    OrganizationAdminDiscountCodesController,
    OrganizationAdminRecurringEventsController,
    OrganizationAdminVATController,
    OrganizationAdminSubscriptionsController,
    OrganizationAdminRevenueController,
    OrganizationAdminTicketsController,
    OrganizationAdminCustomDomainController,
    OrganizationAdminUserNotesController,
    OrganizationAdminUserLookupController,
    OrganizationAdminMemberInvitationsController,
    # Before the register controller: its ``/member-register/{record_id}`` would otherwise swallow the
    # literal ``/member-register/invite-all`` (Django picks the first matching path, then 405s the method).
    OrganizationAdminMemberRegisterLinkingController,
    OrganizationAdminMemberRegisterController,
]

__all__ = [
    "ORGANIZATION_ADMIN_CONTROLLERS",
    "OrganizationAdminAnnouncementsController",
    "OrganizationAdminBlacklistController",
    "OrganizationAdminCoreController",
    "OrganizationAdminCustomDomainController",
    "OrganizationAdminDiscountCodesController",
    "OrganizationAdminMemberInvitationsController",
    "OrganizationAdminMemberRegisterController",
    "OrganizationAdminMemberRegisterLinkingController",
    "OrganizationAdminMembersController",
    "OrganizationAdminMembershipRequestsController",
    "OrganizationAdminRecurringEventsController",
    "OrganizationAdminResourcesController",
    "OrganizationAdminRevenueController",
    "OrganizationAdminSubscriptionsController",
    "OrganizationAdminTicketsController",
    "OrganizationAdminTokensController",
    "OrganizationAdminUserLookupController",
    "OrganizationAdminUserNotesController",
    "OrganizationAdminVATController",
    "OrganizationAdminVenuesController",
    "OrganizationAdminWhitelistController",
]
