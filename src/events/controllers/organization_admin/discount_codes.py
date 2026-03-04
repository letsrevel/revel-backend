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
from events.service import discount_code_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_events")],
)
class OrganizationAdminDiscountCodesController(OrganizationAdminBaseController):
    """Organization discount code management endpoints."""

    @route.get(
        "/discount-codes",
        url_name="list_discount_codes",
        response=PaginatedResponseSchema[schema.DiscountCodeSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["code"])
    def list_discount_codes(
        self,
        slug: str,
        params: filters.DiscountCodeFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.DiscountCode]:
        """List all discount codes for the organization.

        Supports filtering by is_active and discount_type.
        Supports searching by code.
        """
        organization = self.get_one(slug)
        qs = models.DiscountCode.objects.filter(organization=organization).prefetch_related("series", "events", "tiers")
        return params.filter(qs).distinct()

    @route.post(
        "/discount-codes",
        url_name="create_discount_code",
        response={201: schema.DiscountCodeSchema},
    )
    def create_discount_code(
        self, slug: str, payload: schema.DiscountCodeCreateSchema
    ) -> tuple[int, models.DiscountCode]:
        """Create a new discount code for the organization."""
        organization = self.get_one(slug)
        dc = discount_code_service.create_discount_code(organization, payload)
        return 201, dc

    @route.get(
        "/discount-codes/{code_id}",
        url_name="get_discount_code",
        response=schema.DiscountCodeSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_discount_code(self, slug: str, code_id: UUID) -> models.DiscountCode:
        """Get details of a specific discount code."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.DiscountCode.objects.prefetch_related("series", "events", "tiers"),
            pk=code_id,
            organization=organization,
        )

    @route.patch(
        "/discount-codes/{code_id}",
        url_name="update_discount_code",
        response=schema.DiscountCodeSchema,
    )
    def update_discount_code(
        self, slug: str, code_id: UUID, payload: schema.DiscountCodeUpdateSchema
    ) -> models.DiscountCode:
        """Update a discount code. Code value cannot be changed after creation."""
        organization = self.get_one(slug)
        dc = get_object_or_404(models.DiscountCode, pk=code_id, organization=organization)
        return discount_code_service.update_discount_code(dc, payload)

    @route.delete(
        "/discount-codes/{code_id}",
        url_name="delete_discount_code",
        response={204: None},
    )
    def delete_discount_code(self, slug: str, code_id: UUID) -> tuple[int, None]:
        """Soft-delete a discount code by deactivating it."""
        organization = self.get_one(slug)
        dc = get_object_or_404(models.DiscountCode, pk=code_id, organization=organization)
        dc.is_active = False
        dc.save(update_fields=["is_active"])
        return 204, None
