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
from events.service import blacklist_service

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminBlacklistController(OrganizationAdminBaseController):
    """Organization blacklist management endpoints."""

    @route.get(
        "/blacklist",
        url_name="list_blacklist_entries",
        response=PaginatedResponseSchema[schema.BlacklistEntrySchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["email", "telegram_username", "first_name", "last_name", "preferred_name", "user__email"],
    )
    def list_blacklist(
        self,
        slug: str,
        params: filters.BlacklistFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Blacklist]:
        """List all blacklist entries for the organization.

        Supports filtering by:
        - has_user: Whether entry is linked to a registered user
        - has_email: Whether entry has an email address
        - has_telegram: Whether entry has a telegram username

        Supports searching by email, telegram username, and name fields.
        """
        organization = self.get_one(slug)
        qs = models.Blacklist.objects.filter(organization=organization).select_related("user", "created_by")
        return params.filter(qs).distinct()

    @route.post(
        "/blacklist",
        url_name="create_blacklist_entry",
        response={201: schema.BlacklistEntrySchema},
        permissions=[OrganizationPermission("manage_members")],
    )
    def create_blacklist_entry(self, slug: str, payload: schema.BlacklistCreateSchema) -> tuple[int, models.Blacklist]:
        """Add an entry to the organization blacklist.

        **Quick Mode:**
        Provide `user_id` to auto-populate all fields from the user's profile.
        Returns 404 if the user_id doesn't correspond to an existing user.

        **Manual Mode:**
        Provide individual fields (email, telegram_username, phone_number, names).
        At least one identifier or name is required.

        If the provided identifiers match an existing registered user, the entry
        will be automatically linked to them.
        """
        organization = self.get_one(slug)
        current_user = self.user()

        if payload.user_id:
            # Quick mode - blacklist by user ID
            entry = blacklist_service.add_user_to_blacklist(
                organization=organization,
                user_id=payload.user_id,
                created_by=current_user,
                reason=payload.reason,
            )
        else:
            # Manual mode
            entry = blacklist_service.add_to_blacklist(
                organization=organization,
                created_by=current_user,
                email=payload.email,
                telegram_username=payload.telegram_username,
                phone_number=payload.phone_number,
                first_name=payload.first_name,
                last_name=payload.last_name,
                preferred_name=payload.preferred_name,
                reason=payload.reason,
            )

        # Refetch with user relationship (may have been auto-linked)
        # created_by is already available from current request
        entry = models.Blacklist.objects.select_related("user").get(pk=entry.pk)
        entry.created_by = current_user
        return 201, entry

    @route.get(
        "/blacklist/{entry_id}",
        url_name="get_blacklist_entry",
        response=schema.BlacklistEntrySchema,
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    def get_blacklist_entry(self, slug: str, entry_id: UUID) -> models.Blacklist:
        """Get details of a specific blacklist entry."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.Blacklist.objects.select_related("user", "created_by"),
            pk=entry_id,
            organization=organization,
        )

    @route.patch(
        "/blacklist/{entry_id}",
        url_name="update_blacklist_entry",
        response=schema.BlacklistEntrySchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def update_blacklist_entry(
        self, slug: str, entry_id: UUID, payload: schema.BlacklistUpdateSchema
    ) -> models.Blacklist:
        """Update a blacklist entry.

        Only the reason and name fields can be updated.
        Hard identifiers (email, telegram, phone) cannot be changed after creation.
        """
        organization = self.get_one(slug)
        entry = get_object_or_404(models.Blacklist, pk=entry_id, organization=organization)

        update_data = payload.model_dump(exclude_unset=True)
        if update_data:
            entry = blacklist_service.update_blacklist_entry(entry, **update_data)

        return models.Blacklist.objects.select_related("user", "created_by").get(pk=entry.pk)

    @route.delete(
        "/blacklist/{entry_id}",
        url_name="delete_blacklist_entry",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def delete_blacklist_entry(self, slug: str, entry_id: UUID) -> tuple[int, None]:
        """Remove an entry from the blacklist.

        This permanently removes the blacklist entry. The user (if linked)
        will regain access to the organization.
        """
        organization = self.get_one(slug)
        entry = get_object_or_404(models.Blacklist, pk=entry_id, organization=organization)
        blacklist_service.remove_from_blacklist(entry)
        return 204, None
