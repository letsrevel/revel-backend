"""Organization admin controllers package.

This package splits the organization admin endpoints into logical groupings
while preserving the original endpoint order.
"""

from .announcements import OrganizationAdminAnnouncementsController
from .blacklist import OrganizationAdminBlacklistController
from .discount_codes import OrganizationAdminDiscountCodesController
from .core import OrganizationAdminCoreController
from .custom_domain import OrganizationAdminCustomDomainController
from .member_register import OrganizationAdminMemberRegisterController
from .member_register_linking import OrganizationAdminMemberRegisterLinkingController
from .members import OrganizationAdminMembersController
from .membership_requests import OrganizationAdminMembershipRequestsController
from .resources import OrganizationAdminResourcesController
from .revenue import OrganizationAdminRevenueController
from .tokens import OrganizationAdminTokensController
from .recurring_events import OrganizationAdminRecurringEventsController
from .subscriptions import OrganizationAdminSubscriptionsController
from .tickets import OrganizationAdminTicketsController
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
    # Before the register controller: its ``/member-register/{record_id}`` would otherwise swallow the
    # literal ``/member-register/invite-all`` (Django picks the first matching path, then 405s the method).
    OrganizationAdminMemberRegisterLinkingController,
    OrganizationAdminMemberRegisterController,
]

__all__ = [
    "OrganizationAdminAnnouncementsController",
    "OrganizationAdminDiscountCodesController",
    "OrganizationAdminCoreController",
    "OrganizationAdminCustomDomainController",
    "OrganizationAdminTokensController",
    "OrganizationAdminMembershipRequestsController",
    "OrganizationAdminResourcesController",
    "OrganizationAdminMembersController",
    "OrganizationAdminVenuesController",
    "OrganizationAdminBlacklistController",
    "OrganizationAdminWhitelistController",
    "OrganizationAdminRecurringEventsController",
    "OrganizationAdminSubscriptionsController",
    "OrganizationAdminVATController",
    "OrganizationAdminRevenueController",
    "ORGANIZATION_ADMIN_CONTROLLERS",
    "OrganizationAdminTicketsController",
    "OrganizationAdminUserLookupController",
    "OrganizationAdminUserNotesController",
    "OrganizationAdminMemberRegisterController",
    "OrganizationAdminMemberRegisterLinkingController",
]
