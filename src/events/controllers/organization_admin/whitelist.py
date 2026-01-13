from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import whitelist_service

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminWhitelistController(OrganizationAdminBaseController):
    """Organization whitelist management endpoints.

    Handles whitelist requests and whitelist entries.
    """

    # ---- Whitelist Request Management ----

    @route.get(
        "/whitelist-requests",
        url_name="list_whitelist_requests",
        response=PaginatedResponseSchema[schema.WhitelistRequestSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_whitelist_requests(
        self,
        slug: str,
        params: filters.WhitelistRequestFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.WhitelistRequest]:
        """List whitelist requests for the organization.

        By default shows all requests. Use ?status=pending to filter by status.

        Whitelist requests are created when a user's name fuzzy-matches a blacklist
        entry. Approving a request adds the user to the whitelist, allowing them
        full access despite the name match.
        """
        organization = self.get_one(slug)
        qs = models.WhitelistRequest.objects.filter(organization=organization).select_related("user", "decided_by")
        return params.filter(qs).distinct()

    @route.get(
        "/whitelist-requests/{request_id}",
        url_name="get_whitelist_request",
        response=schema.WhitelistRequestSchema,
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    def get_whitelist_request(self, slug: str, request_id: UUID) -> models.WhitelistRequest:
        """Get details of a whitelist request including matched blacklist entries."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.WhitelistRequest.objects.select_related("user", "decided_by").prefetch_related(
                "matched_blacklist_entries"
            ),
            pk=request_id,
            organization=organization,
        )

    @route.post(
        "/whitelist-requests/{request_id}/approve",
        url_name="approve_whitelist_request",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def approve_whitelist_request(self, slug: str, request_id: UUID) -> tuple[int, None]:
        """Approve a whitelist request.

        Creates a whitelist entry for the user, granting them full access
        to the organization despite any fuzzy name matches.
        """
        organization = self.get_one(slug)
        request = get_object_or_404(models.WhitelistRequest, pk=request_id, organization=organization)
        whitelist_service.approve_whitelist_request(request, self.user())
        return 204, None

    @route.post(
        "/whitelist-requests/{request_id}/reject",
        url_name="reject_whitelist_request",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def reject_whitelist_request(self, slug: str, request_id: UUID) -> tuple[int, None]:
        """Reject a whitelist request.

        The user will remain blocked from the organization due to the
        fuzzy name match with blacklist entries.
        """
        organization = self.get_one(slug)
        request = get_object_or_404(models.WhitelistRequest, pk=request_id, organization=organization)
        whitelist_service.reject_whitelist_request(request, self.user())
        return 204, None

    # ---- Whitelist Management ----

    @route.get(
        "/whitelist",
        url_name="list_whitelist_entries",
        response=PaginatedResponseSchema[schema.WhitelistEntrySchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name"])
    def list_whitelist(self, slug: str) -> QuerySet[models.WhitelistRequest]:
        """List all whitelisted users for the organization.

        These are users who were cleared despite fuzzy-matching blacklist entries.
        Whitelisted users are those with an APPROVED whitelist request.
        """
        organization = self.get_one(slug)
        return models.WhitelistRequest.objects.filter(
            organization=organization,
            status=models.WhitelistRequest.Status.APPROVED,
        ).select_related("user", "decided_by")

    @route.delete(
        "/whitelist/{entry_id}",
        url_name="delete_whitelist_entry",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def delete_whitelist_entry(self, slug: str, entry_id: UUID) -> tuple[int, None]:
        """Remove a user from the whitelist.

        After removal, if the user still fuzzy-matches blacklist entries,
        they will need to request whitelisting again.
        """
        organization = self.get_one(slug)
        entry = get_object_or_404(
            models.WhitelistRequest,
            pk=entry_id,
            organization=organization,
            status=models.WhitelistRequest.Status.APPROVED,
        )
        whitelist_service.remove_from_whitelist(entry)
        return 204, None
