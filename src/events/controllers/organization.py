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

from common.authentication import I18nJWTAuth, OptionalAuth
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
    def get_queryset(self, full: bool = True) -> QuerySet[models.Organization]:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if ot := self.get_organization_token():
            allowed_ids = [ot.organization_id]
        if not full:
            return models.Organization.objects.for_user(self.maybe_user(), allowed_ids=allowed_ids)
        return models.Organization.objects.full().for_user(self.maybe_user(), allowed_ids=allowed_ids)

    def get_organization_token(self) -> models.OrganizationToken | None:
        """Get an organization token from X-Organization-Token header or ot query param (legacy).

        Preferred: X-Organization-Token header
        Legacy: ?ot= query parameter (for backwards compatibility)
        """
        token = (
            self.context.request.META.get("HTTP_X_ORG_TOKEN")  # type: ignore[union-attr]
            or self.context.request.GET.get("ot")  # type: ignore[union-attr]
        )
        if token:
            return organization_service.get_organization_token(token)
        return None

    def get_one(self, slug: str) -> models.Organization:
        """Get one organization."""
        return self.get_object_or_exception(self.get_queryset(), slug=slug)  # type: ignore[no-any-return]

    @route.get("/", url_name="list_organizations", response=PaginatedResponseSchema[schema.OrganizationInListSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description", "tags__tag__name"])
    def list_organizations(
        self,
        params: filters.OrganizationFilterSchema = Query(...),  # type: ignore[type-arg]
        order_by: t.Literal["name", "-name", "distance"] = "distance",
    ) -> QuerySet[models.Organization]:
        """Browse and search organizations visible to the current user.

        Results are filtered by visibility settings and user memberships. By default orders by
        'distance' (nearest first based on user location). Can also sort alphabetically by 'name'
        or reverse with '-name'. Supports text search and filtering.
        """
        qs = params.filter(self.get_queryset()).distinct()
        if order_by == "distance":
            return order_by_distance(self.user_location(), qs)
        return qs.order_by(order_by)

    @route.get("/{slug}", url_name="get_organization", response=schema.OrganizationRetrieveSchema)
    def get_organization(self, slug: str) -> models.Organization:
        """Retrieve organization details using its unique slug.

        Returns full organization information including description, location, member count, and
        settings. Use this to display the organization profile page.
        """
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
        """Get resources attached to this organization and marked for display on organization page.

        Returns documents, links, or media files provided by organization admins. Resources may
        be public or restricted to members only. Supports filtering by type and text search.
        """
        organization = self.get_one(slug)
        qs = (
            models.AdditionalResource.objects.for_user(self.maybe_user())
            .filter(organization=organization, display_on_organization_page=True)
            .with_related()
        )
        return params.filter(qs).distinct()

    @route.post(
        "/{slug}/membership-requests",
        url_name="create_membership_request",
        response=schema.OrganizationMembershipRequestRetrieve,
        auth=I18nJWTAuth(),
        throttle=UserRequestThrottle(),
    )
    def create_membership_request(
        self, slug: str, payload: schema.OrganizationMembershipRequestCreateSchema
    ) -> models.OrganizationMembershipRequest:
        """Submit a request to become a member of this organization.

        Creates a membership request that organization admins can approve or reject. Being a
        member may be required to access certain members-only events. Returns the created
        request for tracking status.
        """
        organization = self.get_one(slug)
        return organization_service.create_membership_request(organization, self.user(), message=payload.message)

    @route.get(
        "/tokens/{token_id}",
        url_name="get_organization_token",
        response={200: schema.OrganizationTokenSchema, 404: ResponseMessage},
    )
    def get_organization_token_details(self, token_id: str) -> tuple[int, models.OrganizationToken | ResponseMessage]:
        """Preview an organization token to see what access it grants.

        This endpoint allows users to see token details before deciding whether to claim it.
        No authentication required - tokens are meant to be shareable.

        **Primary Use Case: Visibility via Token Header**
        The main purpose of organization tokens is to grant temporary visibility to organizations.
        Frontend extracts tokens from shareable URLs like `/organizations/{slug}?ot={token_id}`
        and passes them to the API via the `X-Organization-Token` header.

        **Returns:**
        - `id`: The token code (for use in URLs as `?ot=` query param)
        - `organization`: The organization this token grants access to
        - `name`: Display name (e.g., "New Member Recruitment")
        - `expires_at`: When the token stops working (null = never expires)
        - `max_uses`: Maximum number of claims (0 = unlimited)
        - `uses`: Current number of claims
        - `grants_membership`: Whether users become members when claiming
        - `grants_staff_status`: Whether users become staff when claiming

        **Frontend Usage:**
        ```javascript
        // When user visits /organizations/my-org?ot=xyz789, extract and use the token:
        const urlParams = new URLSearchParams(window.location.search);
        const orgToken = urlParams.get('ot');

        // Preview the token first
        const token = await fetch(`/api/organizations/tokens/${orgToken}`).then(r => r.json());

        // Then access the organization with token in header
        const org = await fetch(`/api/organizations/${token.organization.slug}`, {
          headers: { 'X-Organization-Token': orgToken }
        }).then(r => r.json());

        if (token.grants_membership || token.grants_staff_status) {
          // This token can be claimed for membership/staff access
          const access = token.grants_staff_status ? 'staff' : 'member';
          showClaimButton(`Join as ${access}: ${org.name}`);
        } else {
          // This is a read-only token for viewing only
          showMessage(`View access to: ${org.name}`);
        }
        ```

        **Token Types:**
        1. **Read-Only Tokens** (`grants_membership=False`, `grants_staff_status=False`)
           - Share organization link with non-members
           - Users can VIEW the organization but cannot automatically join
           - Example: Share with partners so they can see organization details

        2. **Membership Tokens** (`grants_membership=True`, `grants_staff_status=False`)
           - Users can both VIEW and JOIN as members
           - Creates OrganizationMember when claimed via POST `/organizations/claim-invitation/{token}`

        3. **Staff Tokens** (`grants_staff_status=True`)
           - Users can both VIEW and JOIN as staff with permissions
           - Creates OrganizationStaff when claimed
           - Should be shared privately (not publicly)

        **Error Cases:**
        - 404: Token doesn't exist or has been deleted
        """
        if token := organization_service.get_organization_token(token_id):
            return 200, token
        return 404, ResponseMessage(message="Token not found or expired.")

    @route.post(
        "/claim-invitation/{token}",
        url_name="organization_claim_invitation",
        response={200: schema.OrganizationRetrieveSchema, 400: ResponseMessage},
        auth=I18nJWTAuth(),
    )
    def claim_invitation(self, token: str) -> tuple[int, models.Organization | ResponseMessage]:
        """Accept an organization membership invitation using a token from invitation link.

        Creates an OrganizationMember record, granting you member status. Members may have
        access to members-only events and resources. Returns the organization on success,
        or 400 if the token is invalid/expired.
        """
        if organization := organization_service.claim_invitation(self.user(), token):
            return 200, organization
        return 400, ResponseMessage(message="The token is invalid or expired.")
