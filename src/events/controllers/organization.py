import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja import Query
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from ninja_jwt.authentication import JWTAuth

from common.authentication import OptionalAuth
from common.schema import ResponseMessage
from common.throttling import (
    UserRequestThrottle,
)
from events import filters, models, schema
from events.service import organization_service

from ..service.event_service import order_by_distance
from .user_aware_controller import UserAwareController


@api_controller("/organizations", auth=OptionalAuth(), tags=["Organization"])
class OrganizationController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.Organization]:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if ot := self.get_organization_token():
            allowed_ids = [ot.organization_id]
        return models.Organization.objects.for_user(self.maybe_user(), allowed_ids=allowed_ids)

    def get_organization_token(self) -> models.OrganizationToken | None:
        """Get an event token if exists."""
        if ot := self.context.request.GET.get("ot"):  # type: ignore[union-attr]
            return organization_service.get_organization_token(ot)
        return None

    def get_one(self, slug: str) -> models.Organization:
        """Get one organization."""
        return self.get_object_or_exception(self.get_queryset(), slug=slug)  # type: ignore[no-any-return]

    @route.get("/", url_name="list_organizations", response=PaginatedResponseSchema[schema.OrganizationRetrieveSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_organizations(
        self,
        params: filters.OrganizationFilterSchema = Query(...),  # type: ignore[type-arg]
        order_by: t.Literal["name", "-name", "distance"] = "distance",
    ) -> QuerySet[models.Organization]:
        """List all organizations."""
        qs = params.filter(self.get_queryset())
        if order_by == "distance":
            return order_by_distance(self.user_location(), qs)
        return qs.order_by(order_by)

    @route.get("/{slug}", url_name="get_organization", response=schema.OrganizationRetrieveSchema)
    def get_organization(self, slug: str) -> models.Organization:
        """Get organization by slug."""
        return self.get_one(slug)

    @route.get(
        "/{slug}/resources",
        url_name="list_organization_resources",
        response=PaginatedResponseSchema[schema.AdditionalResourceSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_resources(
        self,
        slug: str,
        params: filters.ResourceFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.AdditionalResource]:
        """List all visible resources for a specific organization."""
        organization = self.get_one(slug)
        qs = models.AdditionalResource.objects.for_user(self.maybe_user()).filter(
            organization=organization, display_on_organization_page=True
        )
        return params.filter(qs)

    @route.post(
        "/{slug}/membership-requests",
        url_name="create_membership_request",
        response=schema.OrganizationMembershipRequestRetrieve,
        auth=JWTAuth(),
        throttle=UserRequestThrottle(),
    )
    def create_membership_request(self, slug: str) -> models.OrganizationMembershipRequest:
        """Request to join an organization."""
        organization = self.get_one(slug)
        return organization_service.create_membership_request(organization, self.user())

    @route.post(
        "/claim-invitation/{token}",
        url_name="organization_claim_invitation",
        response={200: schema.OrganizationRetrieveSchema, 400: ResponseMessage},
        auth=JWTAuth(),
    )
    def claim_invitation(self, token: str) -> tuple[int, models.Organization | ResponseMessage]:
        """Claim an invitation to an organization."""
        if organization := organization_service.claim_invitation(self.user(), token):
            return 200, organization
        return 400, ResponseMessage(message="The token is invalid or expired.")
