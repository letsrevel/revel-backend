import logging
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import File, Form, Query
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from ninja_jwt.authentication import JWTAuth

from accounts.models import RevelUser
from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import safe_save_uploaded_file
from events import filters, models, schema
from events.models import OrganizationMembershipRequest
from events.service import organization_service, resource_service, stripe_service, update_db_instance

from .permissions import IsOrganizationOwner, IsOrganizationStaff, OrganizationPermission
from .user_aware_controller import UserAwareController

logger = logging.getLogger(__name__)


@api_controller("/organization-admin/{slug}", auth=JWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.Organization]:
        """Get the queryset based on the user."""
        return models.Organization.objects.for_user(self.user())

    def get_one(self, slug: str) -> models.Organization:
        """Get one organization."""
        return self.get_object_or_exception(self.get_queryset(), slug=slug)  # type: ignore[no-any-return]

    @route.get(
        "",
        url_name="get_organization_admin",
        response=schema.OrganizationAdminDetailSchema,
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def get_organization(self, slug: str) -> models.Organization:
        """Get comprehensive organization details including all platform fee and Stripe fields."""
        return self.get_one(slug)

    @route.put(
        "",
        url_name="edit_organization",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_organization(self, slug: str, payload: schema.OrganizationEditSchema) -> models.Organization:
        """Update organization by slug."""
        organization = self.get_one(slug)
        return update_db_instance(organization, payload)

    @route.post(
        "/stripe/connect",
        url_name="stripe_connect",
        response=schema.StripeOnboardingLinkSchema,
        permissions=[IsOrganizationOwner()],
    )
    def stripe_connect(self, slug: str) -> schema.StripeOnboardingLinkSchema:
        """Get a link to onboard the organization to Stripe."""
        organization = self.get_one(slug)
        account_id = organization.stripe_account_id or stripe_service.create_connect_account(organization)
        onboarding_url = stripe_service.create_account_link(account_id, organization)
        return schema.StripeOnboardingLinkSchema(onboarding_url=onboarding_url)

    @route.post(
        "/stripe/account/verify",
        url_name="stripe_account_verify",
        response=schema.StripeAccountStatusSchema,
        permissions=[IsOrganizationOwner()],
    )
    def stripe_account_verify(self, slug: str) -> schema.StripeAccountStatusSchema:
        """Get the organization's Stripe account status."""
        organization = stripe_service.stripe_verify_account(self.get_one(slug))
        return schema.StripeAccountStatusSchema(
            is_connected=True,
            charges_enabled=organization.stripe_charges_enabled,
            details_submitted=organization.stripe_details_submitted,
        )

    @route.post(
        "/upload-logo",
        url_name="org_upload_logo",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def upload_logo(self, slug: str, logo: File[UploadedFile]) -> models.Organization:
        """Upload logo to organization."""
        organization = self.get_one(slug)
        organization = safe_save_uploaded_file(instance=organization, field="logo", file=logo, uploader=self.user())
        return organization

    @route.post(
        "/upload-cover-art",
        url_name="org_upload_cover_art",
        response=schema.OrganizationRetrieveSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def upload_cover_art(self, slug: str, cover_art: File[UploadedFile]) -> models.Organization:
        """Upload cover art to organization."""
        organization = self.get_one(slug)
        organization = safe_save_uploaded_file(
            instance=organization, field="cover_art", file=cover_art, uploader=self.user()
        )
        return organization

    @route.delete(
        "/delete-logo",
        url_name="org_delete_logo",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_logo(self, slug: str) -> tuple[int, None]:
        """Delete logo from organization."""
        organization = self.get_one(slug)
        if organization.logo:
            organization.logo.delete(save=True)
        return 204, None

    @route.delete(
        "/delete-cover-art",
        url_name="org_delete_cover_art",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def delete_cover_art(self, slug: str) -> tuple[int, None]:
        """Delete cover art from organization."""
        organization = self.get_one(slug)
        if organization.cover_art:
            organization.cover_art.delete(save=True)
        return 204, None

    @route.post(
        "/create-event-series",
        url_name="create_event_series",
        response={200: schema.EventSeriesRetrieveSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event_series")],
    )
    def create_event_series(self, slug: str, payload: schema.EventSeriesEditSchema) -> models.EventSeries:
        """Create a new event series."""
        organization = self.get_one(slug)
        return models.EventSeries.objects.create(organization=organization, **payload.model_dump())

    @route.post(
        "/create-event",
        url_name="create_event",
        response={200: schema.EventDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event")],
    )
    def create_event(self, slug: str, payload: schema.EventCreateSchema) -> models.Event:
        """Create a new event."""
        organization = self.get_one(slug)
        return models.Event.objects.create(organization=organization, **payload.model_dump())

    @route.get(
        "/tokens",
        url_name="list_organization_tokens",
        response=PaginatedResponseSchema[schema.OrganizationTokenSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name"])
    def list_organization_tokens(
        self,
        slug: str,
        params: filters.OrganizationTokenFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.OrganizationToken]:
        """Retrieve all membership invitation tokens for this organization.

        Organization tokens serve two purposes:
        1. **Visibility** - Grant temporary access to view private organizations via `?ot=` URL parameter
        2. **Membership** - Allow users to join as members or staff

        Use this to view, manage, and track all active invitation links.

        **Returns:**
        Paginated list of tokens with:
        - `id`: The unique token code (used in shareable links)
        - `name`: Display name for organization (e.g., "New Member Link", "Staff Onboarding")
        - `issuer`: The user who created this token
        - `expires_at`: When the token stops working (null = never expires)
        - `uses`: How many people have joined using this token
        - `max_uses`: Maximum allowed uses (0 = unlimited)
        - `grants_membership`: If true, users become organization members
        - `grants_staff_status`: If true, users become staff with permissions
        - `created_at`: When the token was created

        **Filtering & Search:**
        - Search by token name
        - Filter by expiration status, grant type (member/staff), or usage count
        - Results are paginated (20 per page by default)

        **Frontend Implementation:**
        Build an organization token management dashboard:

        1. **Token List Table:**
           ```
           Name                 | Type    | Uses      | Expires    | Actions
           ─────────────────────┼─────────┼───────────┼────────────┼──────────
           New Member Link      | Member  | 45/100    | May 1      | Copy Edit Delete
           Staff Onboarding     | Staff   | 3/5       | Never      | Copy Edit Delete
           Trial Period Access  | Member  | 120/∞     | Expired    | View Delete
           ```

        2. **Status Badges:**
           - Active (green): Can still be used
           - Expired (red): Past expiration date
           - Full (yellow): Reached max_uses
           - Staff (purple badge): grants_staff_status=true

        3. **Shareable Link Format:**
           - For visibility: `https://yourapp.com/organizations/{slug}?ot={token_id}`
             (Frontend extracts `?ot=` and sends as `X-Organization-Token` header to API)
           - For claiming: `https://yourapp.com/join/org/{token_id}` → POST `/organizations/claim-invitation/{token_id}`

        4. **Analytics Display:**
           - Show usage trends over time
           - Display member vs staff token breakdown
           - Track which tokens are most effective for growth

        **Use Cases:**
        - Monitor all organization invitation links in one place
        - Audit who created which tokens and track their usage
        - Identify which recruitment channels are most effective
        - Clean up expired or unused tokens
        - Verify staff invitation tokens are properly restricted
        """
        organization = self.get_one(slug)
        return params.filter(models.OrganizationToken.objects.filter(organization=organization)).distinct()

    @route.post(
        "/tokens",
        url_name="create_organization_token",
        response=schema.OrganizationTokenSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def create_organization_token(
        self, slug: str, payload: schema.OrganizationTokenCreateSchema
    ) -> models.OrganizationToken:
        """Create a new shareable token for this organization.

        Organization tokens serve dual purposes:
        1. **Primary: Visibility** - Share links like `/organizations/{slug}?ot={token}`
            to let non-members view private orgs
        2. **Secondary: Membership** - Optionally allow users to claim membership/staff status

        This enables sharing organization details in group chats, social media, or with partners without
        requiring them to become members first.

        **Use Cases:**
        - **Member Recruitment:** Share on social media or email to grow membership
        - **Staff Onboarding:** Create staff-level tokens for new employees/volunteers
        - **Partner Access:** Give to partner organizations for bulk member addition
        - **Time-Limited Campaigns:** Trial memberships that expire after promotional period
        - **Capacity Management:** Limit new members (e.g., "Accept first 50 applicants")
        - **Department-Specific Links:** Track which recruitment channels are most effective
        - **Bulk Invitations:** Instead of entering 100 emails, share one link

        **Parameters:**
        - `name`: Display name for organization (e.g., "Spring 2025 Recruitment", "Staff Portal")
        - `duration`: Minutes until expiration (default: 24*60 = 1 day)
        - `max_uses`: Maximum people who can use this token (default: 1, use 0 for unlimited)
        - `grants_membership`: If true, users become organization members (default: true)
        - `grants_staff_status`: If true, users become staff with permissions (default: false)

        **Important:** You CANNOT set both `grants_membership` and `grants_staff_status` to false.
        The token must grant at least one type of access.

        **Returns:**
        The created token with a unique `id` that serves as the shareable code.

        **Business Logic:**
        - Token issuer is automatically set to the current authenticated user
        - Expiration is calculated from current time + duration
        - Token ID is a secure random 8-character alphanumeric code
        - Staff tokens also grant member status automatically
        - Created tokens start with 0 uses

        **Frontend Implementation:**
        After creation, immediately display the shareable link:

        ```javascript
        // On successful creation:
        const shareableUrl = `https://yourapp.com/join/org/${response.id}`;

        // Show UI with:
        - Prominent "Copy Link" button
        - QR code for physical distribution (posters, flyers)
        - Email template with embedded link
        - Social media share buttons (Twitter, LinkedIn, Facebook)
        - Usage tracking: "0 / 100 people joined" with progress bar
        - Expiration countdown: "Expires in 6 days, 23 hours"
        - Type badge: "Member Access" or "Staff Access" (with warning icon for staff)
        ```

        **Example Workflows:**

        1. **Member Recruitment Campaign:**
           ```
           POST /organization-admin/my-org/token
           {
             "name": "Fall 2025 Open House",
             "duration": 30 * 24 * 60,  // 30 days
             "max_uses": 0,  // unlimited
             "grants_membership": true,
             "grants_staff_status": false
           }
           ```
           → Share link on website/social media
           → New members join automatically

        2. **Staff Onboarding:**
           ```
           POST /organization-admin/my-org/token
           {
             "name": "New Staff Onboarding Q1 2025",
             "duration": 7 * 24 * 60,  // 7 days
             "max_uses": 5,  // exactly 5 new staff
             "grants_membership": true,
             "grants_staff_status": true
           }
           ```
           → Send link privately to 5 new hires
           → They get staff access immediately
           → Token auto-expires after 5 uses

        **Security Considerations:**
        - **Staff tokens are sensitive** - only share privately (email, Slack DM)
        - Member tokens can be public (social media, website)
        - Consider short expiration for staff tokens (1-7 days)
        - Use max_uses to prevent link abuse
        - Staff get default permissions defined in PermissionMap

        **Error Cases:**
        - 403: User lacks "manage_members" permission
        - 404: Organization slug not found or user lacks access
        """
        organization = self.get_one(slug)
        return organization_service.create_organization_token(
            organization=organization, issuer=self.user(), **payload.model_dump()
        )

    @route.put(
        "/tokens/{token_id}",
        url_name="edit_organization_token",
        response=schema.OrganizationTokenSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def update_organization_token(
        self, slug: str, token_id: str, payload: schema.OrganizationTokenUpdateSchema
    ) -> models.OrganizationToken:
        """Update an existing organization token's configuration.

        Organization tokens are shareable codes/links that allow users to join your organization.
        Use this endpoint to modify token settings after creation.

        **Use Cases:**
        - Extend expiration date for ongoing recruitment campaigns
        - Increase/decrease maximum uses based on capacity needs
        - Change token name for better organization
        - Convert member-only token to grant staff status (for promotions)
        - Disable token by setting max_uses equal to current uses

        **Parameters:**
        - `name`: Optional display name (e.g., "Q2 Recruitment", "Core Team")
        - `max_uses`: Maximum number of people who can use this (0 = unlimited)
        - `expires_at`: When the token becomes invalid (null = never expires)
        - `grants_membership`: Whether users become organization members
        - `grants_staff_status`: Whether users become staff with permissions

        **Business Logic:**
        - The token's usage count (current uses) is NOT reset when updating
        - If you set max_uses lower than current uses, the token becomes inactive immediately
        - Staff tokens automatically include membership (grants_membership is implied)
        - The token ID itself never changes (shareable link remains the same)
        - Changing grant types only affects future users, not those who already joined

        **Frontend Implementation:**
        Display a token edit form with:

        1. **Current Status Display:**
           ```
           Current uses: 45 / 100
           Expires: May 15, 2025 (in 12 days)
           Type: Member Access
           ```

        2. **Edit Form with Validation:**
           - Name input (optional, max 150 chars)
           - Max uses input (warn if lowering below current uses)
           - Expiration date picker
           - Checkbox: "Grant membership access" (checked by default)
           - Checkbox: "Grant staff access" (warning icon - requires manage_members)

        3. **Warnings & Validation:**
           ```javascript
           if (newMaxUses < token.uses) {
             showWarning("This will disable the token immediately! Current uses exceed new limit.");
           }
           if (payload.grants_staff_status) {
             showWarning("Staff tokens grant sensitive permissions. Share carefully.");
           }
           ```

        4. **Real-time Effects Display:**
           - "If you reduce max_uses from 100 to 40, this link will stop working (already 45 uses)"
           - "Extending expiration will allow the link to work for 30 more days"

        **Example Scenarios:**

        1. **Extend Successful Campaign:**
           User created a token expiring in 7 days, but it's going viral.
           → Update expires_at to 30 days from now
           → Keep max_uses at 0 (unlimited)

        2. **Limit Runaway Token:**
           Token was shared publicly and getting too many signups.
           → Update max_uses from 0 to current_uses + 10
           → This stops growth at 10 more people

        3. **Promote Members to Staff:**
           Want to give existing token staff-granting powers.
           → Update grants_staff_status from false to true
           → Future users get staff access (past users unchanged)

        **Security Note:**
        Be careful when granting staff status - staff members can:
        - Create events
        - Manage tickets
        - Invite other users
        - View sensitive organization data

        **Error Cases:**
        - 403: User lacks "manage_members" permission
        - 404: Token ID not found or doesn't belong to this organization
        """
        organization = self.get_one(slug)
        token = get_object_or_404(models.OrganizationToken, pk=token_id, organization=organization)
        return update_db_instance(token, payload)

    @route.delete(
        "/tokens/{token_id}",
        url_name="delete_organization_token",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def delete_organization_token(self, slug: str, token_id: str) -> tuple[int, None]:
        """Permanently delete an organization token and invalidate all links using it.

        **Use Cases:**
        - Revoke access when a token is compromised or shared inappropriately
        - Clean up expired tokens to keep the token list organized
        - Remove tokens after completing a recruitment campaign
        - Invalidate staff tokens after onboarding is complete
        - Cancel public links that are no longer needed

        **Important Warnings:**
        - This action is IRREVERSIBLE - the token and its link become permanently invalid
        - New users with the link will no longer be able to join the organization
        - However, users who ALREADY joined using this token remain as members/staff
        - The token's usage statistics are permanently lost
        - If you want to temporarily disable a token, consider updating max_uses instead

        **What Happens to Existing Users:**
        - Members who joined via this token: Still members (unchanged)
        - Staff who joined via this token: Still staff with permissions (unchanged)
        - Their membership/staff status persists even after token deletion
        - Only NEW attempts to use the link will fail

        **Frontend Implementation:**

        1. **Confirmation Dialog:**
           ```
           Title: Delete "{token.name}"?

           This will permanently disable the link. No one will be able to use it to join.

           • {token.uses} people already joined using this link
           • Those members/staff will keep their access
           • This action cannot be undone

           [Cancel] [Delete Token]
           ```

        2. **Token Type Warnings:**
           ```javascript
           if (token.grants_staff_status) {
             showExtraWarning(
               "This is a STAFF token. Make sure all intended staff have already joined."
             );
           }
           ```

        3. **Pre-Deletion Actions:**
           - Show "Copy link" button in confirmation dialog (for archival)
           - Display current usage stats one last time
           - Optionally allow user to download list of people who used it

        4. **Post-Deletion:**
           - Remove token from list immediately
           - Show success toast: "Token deleted. The invitation link is now invalid."
           - Update organization stats (total tokens count)

        **Alternative to Deletion:**
        Instead of deleting, consider setting `max_uses` = current `uses` to disable it
        while preserving usage statistics for analytics.

        **Example Scenarios:**

        1. **Compromised Staff Token:**
           Staff token accidentally posted publicly → Delete immediately
           → Existing staff keep access, no new staff can join

        2. **Campaign Cleanup:**
           Recruitment campaign ended → Delete old tokens
           → Keeps member list clean and organized

        3. **Accidental Creation:**
           Created token with wrong settings → Delete and recreate
           → No one used it yet, safe to remove

        **Error Cases:**
        - 403: User lacks "manage_members" permission
        - 404: Token ID not found or doesn't belong to this organization
        """
        organization = self.get_one(slug)
        token = get_object_or_404(models.OrganizationToken, pk=token_id, organization=organization)
        token.delete()
        return 204, None

    @route.get(
        "/membership-requests",
        url_name="list_membership_requests",
        response=PaginatedResponseSchema[schema.OrganizationMembershipRequestRetrieve],
        permissions=[OrganizationPermission("manage_members")],
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
        permissions=[OrganizationPermission("manage_members")],
    )
    def approve_membership_request(self, slug: str, request_id: UUID) -> tuple[int, None]:
        """Approve a membership request."""
        organization = self.get_one(slug)
        membership_request = get_object_or_404(OrganizationMembershipRequest, pk=request_id, organization=organization)
        organization_service.approve_membership_request(membership_request, self.user())
        return 204, None

    @route.post(
        "/membership-requests/{request_id}/reject",
        url_name="reject_membership_request",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def reject_membership_request(self, slug: str, request_id: UUID) -> tuple[int, None]:
        """Reject a membership request."""
        organization = self.get_one(slug)
        membership_request = get_object_or_404(OrganizationMembershipRequest, pk=request_id, organization=organization)
        organization_service.reject_membership_request(membership_request, self.user())
        return 204, None

    @route.get(
        "/resources",
        url_name="list_organization_resources_admin",
        response=PaginatedResponseSchema[schema.AdditionalResourceSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_resources(
        self,
        slug: str,
        params: filters.ResourceFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.AdditionalResource]:
        """List all resources for a specific organization."""
        organization = self.get_one(slug)
        return params.filter(organization.additional_resources.with_related()).distinct()

    @route.post(
        "/resources",
        url_name="create_organization_resource",
        response=schema.AdditionalResourceSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def create_resource(
        self, slug: str, payload: Form[schema.AdditionalResourceCreateSchema]
    ) -> models.AdditionalResource:
        """Create a new resource for the organization.

        Accepts multipart/form-data with individual form fields for each schema property.
        For FILE type resources, include the file parameter.
        """
        organization = self.get_one(slug)

        # Extract file from request.FILES if present
        # Django Ninja doesn't populate the File parameter when using Form[Schema]
        file = self.context.request.FILES.get("file") if self.context.request else None  # type: ignore[union-attr]

        return resource_service.create_resource(organization, payload, file=file)  # type: ignore[arg-type]

    @route.get(
        "/resources/{resource_id}",
        url_name="get_organization_resource",
        response=schema.AdditionalResourceSchema,
    )
    def get_resource(self, slug: str, resource_id: UUID) -> models.AdditionalResource:
        """Retrieve a specific resource for the organization."""
        organization = self.get_one(slug)
        return get_object_or_404(models.AdditionalResource, pk=resource_id, organization=organization)

    @route.put(
        "/resources/{resource_id}",
        url_name="update_organization_resource",
        response=schema.AdditionalResourceSchema,
        permissions=[OrganizationPermission("edit_organization")],
    )
    def update_resource(
        self, slug: str, resource_id: UUID, payload: schema.AdditionalResourceUpdateSchema
    ) -> models.AdditionalResource:
        """Update a resource for the organization."""
        organization = self.get_one(slug)
        resource = get_object_or_404(models.AdditionalResource, pk=resource_id, organization=organization)
        return resource_service.update_resource(resource, payload)

    @route.delete(
        "/resources/{resource_id}",
        url_name="delete_organization_resource",
        response={204: None},
        permissions=[OrganizationPermission("manage_organization")],
    )
    def delete_resource(self, slug: str, resource_id: UUID) -> tuple[int, None]:
        """Delete a resource from the organization."""
        organization = self.get_one(slug)
        resource = get_object_or_404(models.AdditionalResource, pk=resource_id, organization=organization)
        resource.delete()
        return 204, None

    @route.get(
        "/members",
        url_name="list_organization_members",
        response=PaginatedResponseSchema[schema.OrganizationMemberSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__username", "user__email", "user__first_name", "user__last_name"])
    def list_members(self, slug: str) -> QuerySet[models.OrganizationMember]:
        """List all members of an organization."""
        organization = self.get_one(slug)
        return models.OrganizationMember.objects.filter(organization=organization).select_related("user").distinct()

    @route.delete(
        "/members/{user_id}",
        url_name="remove_organization_member",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def remove_member(self, slug: str, user_id: UUID) -> tuple[int, None]:
        """Remove a member from an organization."""
        organization = self.get_one(slug)
        user_to_remove = get_object_or_404(RevelUser, id=user_id)
        organization_service.remove_member(organization, user_to_remove)
        return 204, None

    @route.get(
        "/staff",
        url_name="list_organization_staff",
        response=PaginatedResponseSchema[schema.OrganizationStaffSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__username", "user__email", "user__first_name", "user__last_name"])
    def list_staff(self, slug: str) -> QuerySet[models.OrganizationStaff]:
        """List all staff of an organization."""
        organization = self.get_one(slug)
        return models.OrganizationStaff.objects.filter(organization=organization).select_related("user").distinct()

    @route.delete(
        "/staff/{user_id}",
        url_name="remove_organization_staff",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def remove_staff(self, slug: str, user_id: UUID) -> tuple[int, None]:
        """Remove a staff member from an organization."""
        organization = self.get_one(slug)
        user_to_remove = get_object_or_404(RevelUser, id=user_id)
        organization_service.remove_staff(organization, user_to_remove)
        return 204, None

    @route.post(
        "/staff/{user_id}",
        url_name="create_organization_staff",
        response={201: schema.OrganizationStaffSchema},
        permissions=[OrganizationPermission("manage_members")],
    )
    def add_staff(
        self, slug: str, user_id: UUID, payload: models.PermissionsSchema | None = None
    ) -> tuple[int, models.OrganizationStaff]:
        """Add a staff member to an organization."""
        organization = self.get_one(slug)
        if organization.owner != self.user():
            raise HttpError(403, "Only the organization owner can create staff members.")
        users_to_add = get_object_or_404(RevelUser, id=user_id)
        return 201, organization_service.add_staff(organization, users_to_add, permissions=payload)

    @route.put(
        "/staff/{user_id}/permissions",
        url_name="update_staff_permissions",
        response=schema.OrganizationStaffSchema,
        permissions=[OrganizationPermission("edit_organization")],  # Only owners can do this
    )
    def update_staff_permissions(
        self, slug: str, user_id: UUID, payload: models.PermissionsSchema
    ) -> models.OrganizationStaff:
        """Update a staff member's permissions."""
        organization = self.get_one(slug)
        # Check if current user is the owner
        if organization.owner != self.user():
            raise HttpError(403, "Only the organization owner can change staff permissions.")

        staff_member = get_object_or_404(models.OrganizationStaff, organization=organization, user_id=user_id)
        return organization_service.update_staff_permissions(staff_member, payload)

    @route.post(
        "/tags",
        url_name="add_organization_tags",
        response=list[TagSchema],
        permissions=[OrganizationPermission("edit_organization")],
    )
    def add_tags(self, slug: str, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add one or more tags to the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.add(*payload.tags)
        return organization.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_organization_tags",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def clear_tags(self, slug: str) -> tuple[int, None]:
        """Clear akk tags from the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_organization_tags",
        response=list[TagSchema],
        permissions=[OrganizationPermission("edit_organization")],
    )
    def remove_tags(self, slug: str, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove one or more tags from the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.remove(*payload.tags)
        return organization.tags_manager.all()
