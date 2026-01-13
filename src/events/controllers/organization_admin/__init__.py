"""Organization admin controllers package.

This package splits the organization admin endpoints into logical groupings
while preserving the original endpoint order.
"""

from .blacklist import OrganizationAdminBlacklistController
from .core import OrganizationAdminCoreController
from .members import OrganizationAdminMembersController
from .membership_requests import OrganizationAdminMembershipRequestsController
from .resources import OrganizationAdminResourcesController
from .tokens import OrganizationAdminTokensController
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
]

__all__ = [
    "OrganizationAdminCoreController",
    "OrganizationAdminTokensController",
    "OrganizationAdminMembershipRequestsController",
    "OrganizationAdminResourcesController",
    "OrganizationAdminMembersController",
    "OrganizationAdminVenuesController",
    "OrganizationAdminBlacklistController",
    "OrganizationAdminWhitelistController",
    "ORGANIZATION_ADMIN_CONTROLLERS",
]
