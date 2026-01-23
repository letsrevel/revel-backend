from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Form, Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import resource_service

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminResourcesController(OrganizationAdminBaseController):
    """Organization additional resources management endpoints."""

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
        return get_object_or_404(
            models.AdditionalResource.objects.with_related(),
            pk=resource_id,
            organization=organization,
        )

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
