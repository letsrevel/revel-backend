"""Org-admin lookup of an account by exact verified email, to add them as a member or staff."""

import typing as t

from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja_extra import api_controller, route
from pydantic import EmailStr

from common.authentication import I18nJWTAuth
from common.throttling import OrgUserLookupThrottle
from events import schema
from events.controllers.permissions import OrganizationPermission
from events.service import org_user_lookup_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    # Session-only (no RequireScope): an account-existence oracle that no OAuth scope's consent label promises.
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminUserLookupController(OrganizationAdminBaseController):
    """Resolve an exact email to a ``user_id`` for the add-member and add-staff routes."""

    @route.get(
        "/users/lookup",
        url_name="lookup_organization_user",
        response={200: schema.OrgUserLookupSchema, 404: schema.OrgUserLookupNotFoundSchema},
        throttle=OrgUserLookupThrottle(),
    )
    def lookup_user(
        self, slug: str, email: t.Annotated[EmailStr, Query(...)]
    ) -> tuple[int, schema.OrgUserLookupSchema | schema.OrgUserLookupNotFoundSchema]:
        """Find a verified account by its exact email. Every non-match returns the same 404."""
        found = org_user_lookup_service.lookup_user_by_email(self.get_one(slug), self.user(), email)
        if found is None:
            return 404, schema.OrgUserLookupNotFoundSchema(detail=str(_("User not found.")), code="user_not_found")
        return 200, found
