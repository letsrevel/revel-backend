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
from common.controllers import UserAwareController
from common.schema import ResponseMessage
from common.throttling import (
    UserRequestThrottle,
    WriteThrottle,
)
from events import filters, models, schema
from events.service import (
    announcement_service,
    blacklist_service,
    follow_service,
    organization_service,
    whitelist_service,
)

from ..service.event_service import order_by_distance


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

    @route.post(
        "/",
        url_name="create_organization",
        response={201: schema.OrganizationRetrieveSchema},
        auth=I18nJWTAuth(requires_verified_email=True),
        throttle=WriteThrottle(),
    )
    def create_organization(self, payload: schema.OrganizationCreateSchema) -> tuple[int, models.Organization]:
        """Create a new organization.

        Creates a new organization with the authenticated user as the owner. Each user
        can only own one organization.

        **Requirements:**
        - User must have a verified email address
        - User cannot already own an organization

        **Parameters:**
        - `name`: Organization name (max 150 characters, must be unique)
        - `description`: Optional description
        - `contact_email`: Contact email for the organization (will be verified if it matches user's verified email)

        **Returns:**
        - 201: The created organization

        **Error Cases:**
        - 400: User already owns an organization
        - 403: Email not verified
        """
        organization = organization_service.create_organization(owner=self.user(), **payload.model_dump())
        return 201, organization

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

    @route.post(
        "/{slug}/whitelist-request",
        url_name="create_whitelist_request",
        response={201: schema.WhitelistRequestSchema},
        auth=I18nJWTAuth(),
        throttle=UserRequestThrottle(),
    )
    def create_whitelist_request(
        self, slug: str, payload: schema.WhitelistRequestCreateSchema
    ) -> tuple[int, models.WhitelistRequest]:
        """Request whitelisting for an organization.

        If your name fuzzy-matches a blacklist entry, you may need to request
        verification (whitelisting) to access the organization. This endpoint
        creates a whitelist request that organization admins will review.

        **When to use:**
        This endpoint should be called when attempting to access an organization
        returns a "verification required" status in the eligibility check.

        **Parameters:**
        - `message`: Optional explanation for why you should be whitelisted

        **Returns:**
        - 201: The created whitelist request

        **Error Cases:**
        - 400: Already whitelisted, request already exists, or no matching blacklist entries
        - 404: Organization not found
        """
        # Get organization (even if user is soft-blocked, they need to create a request)
        organization = self.get_object_or_exception(models.Organization.objects.all(), slug=slug)

        # Get fuzzy matches for this user
        fuzzy_matches = blacklist_service.get_fuzzy_blacklist_matches(self.user(), organization)

        if not fuzzy_matches:
            from ninja.errors import HttpError

            raise HttpError(400, "No matching blacklist entries found.")

        # Extract blacklist entries from matches
        matched_entries = [entry for entry, _ in fuzzy_matches]

        request = whitelist_service.create_whitelist_request(
            user=self.user(),
            organization=organization,
            matched_entries=matched_entries,
            message=payload.message,
        )

        return 201, models.WhitelistRequest.objects.select_related("user").get(pk=request.pk)

    @route.get(
        "/{slug}/follow",
        url_name="get_organization_follow_status",
        response=schema.OrganizationFollowStatusSchema,
        auth=I18nJWTAuth(),
    )
    def get_follow_status(self, slug: str) -> schema.OrganizationFollowStatusSchema:
        """Check if the current user is following this organization.

        Returns whether the user is following and the follow details if applicable.
        """
        organization = self.get_one(slug)
        is_following = follow_service.is_following_organization(self.user(), organization)

        if is_following:
            follow = models.OrganizationFollow.objects.select_related("organization").get(
                user=self.user(), organization=organization, is_archived=False
            )
            return schema.OrganizationFollowStatusSchema(
                is_following=True,
                follow=schema.OrganizationFollowSchema.from_model(follow),
            )

        return schema.OrganizationFollowStatusSchema(is_following=False, follow=None)

    @route.post(
        "/{slug}/follow",
        url_name="follow_organization",
        response={201: schema.OrganizationFollowSchema},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def follow_organization(
        self, slug: str, payload: schema.OrganizationFollowCreateSchema
    ) -> tuple[int, schema.OrganizationFollowSchema]:
        """Follow an organization to receive notifications about new events and announcements.

        Creates a follow relationship with the organization. You'll receive notifications
        based on your preference settings (notify_new_events, notify_announcements).

        **Parameters:**
        - `notify_new_events`: Whether to receive notifications when the organization creates new events
        - `notify_announcements`: Whether to receive notifications for organization announcements

        **Returns:**
        - 201: The created follow relationship

        **Error Cases:**
        - 400: Already following this organization
        - 404: Organization not found or not visible
        """
        organization = self.get_one(slug)
        follow = follow_service.follow_organization(
            self.user(),
            organization,
            notify_new_events=payload.notify_new_events,
            notify_announcements=payload.notify_announcements,
        )
        return 201, schema.OrganizationFollowSchema.from_model(follow)

    @route.patch(
        "/{slug}/follow",
        url_name="update_organization_follow",
        response=schema.OrganizationFollowSchema,
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def update_organization_follow(
        self, slug: str, payload: schema.OrganizationFollowUpdateSchema
    ) -> schema.OrganizationFollowSchema:
        """Update notification preferences for an organization you're following.

        Allows you to toggle notification preferences without unfollowing.

        **Parameters:**
        - `notify_new_events`: Whether to receive new event notifications
        - `notify_announcements`: Whether to receive announcement notifications

        **Returns:**
        - The updated follow relationship

        **Error Cases:**
        - 400: Not following this organization
        """
        organization = self.get_one(slug)
        follow = follow_service.update_organization_follow_preferences(
            self.user(),
            organization,
            notify_new_events=payload.notify_new_events,
            notify_announcements=payload.notify_announcements,
        )
        return schema.OrganizationFollowSchema.from_model(follow)

    @route.delete(
        "/{slug}/follow",
        url_name="unfollow_organization",
        response={204: None},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def unfollow_organization(self, slug: str) -> tuple[int, None]:
        """Unfollow an organization to stop receiving notifications.

        Removes the follow relationship. Your follow history is preserved internally
        but you'll no longer receive notifications from this organization.

        **Returns:**
        - 204: Successfully unfollowed

        **Error Cases:**
        - 400: Not following this organization
        """
        organization = self.get_one(slug)
        follow_service.unfollow_organization(self.user(), organization)
        return 204, None

    @route.get(
        "/{slug}/member-announcements",
        url_name="list_member_announcements",
        response=list[schema.AnnouncementPublicSchema],
        auth=I18nJWTAuth(),
    )
    def list_member_announcements(self, slug: str) -> list[models.Announcement]:
        """List member/staff announcements for an organization.

        Returns sent announcements visible to the current user based on their
        organization relationship. Visibility is determined by:
        - User received the notification when it was sent, OR
        - User is currently eligible and announcement has past_visibility enabled

        This endpoint returns organization-level announcements (not event-specific).
        For event announcements, use the event announcements endpoint.

        Announcements are ordered by sent date (newest first).

        Note:
            Visibility filtering performs N+1 queries (1-2 queries per announcement).
            This is acceptable for typical announcement volumes per organization.
            See `is_user_eligible_for_announcement` for optimization notes.

        **Returns:**
        - List of visible announcements

        **Requirements:**
        - User must be a member, staff, or owner of the organization
        """
        organization = self.get_one(slug)
        user = self.user()

        # Get all sent, non-event announcements for this org with prefetched data
        announcements = list(
            models.Announcement.objects.filter(
                organization=organization,
                event__isnull=True,
                status=models.Announcement.AnnouncementStatus.SENT,
            )
            .select_related("organization")
            .order_by("-sent_at")
        )

        # Filter to only those the user can see
        visible_announcements = [
            a for a in announcements if announcement_service.is_user_eligible_for_announcement(a, user)
        ]

        return visible_announcements
