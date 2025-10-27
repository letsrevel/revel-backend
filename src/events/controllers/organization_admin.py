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
        """List all tokens for an organization that the user has admin rights for."""
        organization = self.get_one(slug)
        return params.filter(models.OrganizationToken.objects.filter(organization=organization)).distinct()

    @route.post(
        "/token",
        url_name="create_organization_token",
        response=schema.OrganizationTokenSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def create_organization_token(
        self, slug: str, payload: schema.OrganizationTokenCreateSchema
    ) -> models.OrganizationToken:
        """Create a new token for an organization."""
        organization = self.get_one(slug)
        return organization_service.create_organization_token(
            organization=organization, issuer=self.user(), **payload.model_dump()
        )

    @route.put(
        "/token/{token_id}",
        url_name="edit_organization_token",
        response=schema.OrganizationTokenSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def update_organization_token(
        self, slug: str, token_id: str, payload: schema.OrganizationTokenUpdateSchema
    ) -> models.OrganizationToken:
        """Update an organization token."""
        organization = self.get_one(slug)
        token = get_object_or_404(models.OrganizationToken, pk=token_id, organization=organization)
        return update_db_instance(token, payload)

    @route.delete(
        "/token/{token_id}",
        url_name="delete_organization_token",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def delete_organization_token(self, slug: str, token_id: str) -> tuple[int, None]:
        """Delete an organization token."""
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
