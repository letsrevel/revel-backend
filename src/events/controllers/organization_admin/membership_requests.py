from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import OrganizationPermission
from events.models import OrganizationMembershipRequest
from events.service import organization_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminMembershipRequestsController(OrganizationAdminBaseController):
    """Organization membership request management endpoints."""

    @route.get(
        "/membership-requests",
        url_name="list_membership_requests",
        response=PaginatedResponseSchema[schema.OrganizationMembershipRequestRetrieve],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_membership_requests(
        self,
        slug: str,
        params: filters.MembershipRequestFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[OrganizationMembershipRequest]:
        """List all membership requests for an organization.

        By default shows all requests. Use ?status=pending to filter by status.
        """
        organization = self.get_one(slug)
        qs = OrganizationMembershipRequest.objects.filter(organization=organization).select_related("user")
        return params.filter(qs).distinct()

    @route.post(
        "/membership-requests/{request_id}/approve",
        url_name="approve_membership_request",
        response={204: None},
    )
    def approve_membership_request(
        self, slug: str, request_id: UUID, payload: schema.ApproveMembershipRequestSchema
    ) -> tuple[int, None]:
        """Approve a membership application.

        ``tier_id`` may be omitted if the application already carries a tier.
        """
        organization = self.get_one(slug)
        membership_request = get_object_or_404(
            OrganizationMembershipRequest.objects.select_related("tier"),
            pk=request_id,
            organization=organization,
        )

        tier = None
        if payload.tier_id:
            tier = get_object_or_404(models.MembershipTier, pk=payload.tier_id, organization=organization)

        organization_service.approve_membership_request(membership_request, self.user(), tier)
        return 204, None

    @route.post(
        "/membership-requests/{request_id}/reject",
        url_name="reject_membership_request",
        response={204: None},
    )
    def reject_membership_request(self, slug: str, request_id: UUID) -> tuple[int, None]:
        """Reject a membership request."""
        organization = self.get_one(slug)
        membership_request = get_object_or_404(OrganizationMembershipRequest, pk=request_id, organization=organization)
        organization_service.reject_membership_request(membership_request, self.user())
        return 204, None
