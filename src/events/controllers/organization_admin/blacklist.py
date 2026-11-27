import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.api_controller import api_controller
from common.authentication import ScopedJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import blacklist_service
from events.service.holder_visibility import annotate_holder_known
from oauth.permissions import RequireScope

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=ScopedJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[RequireScope("org:read"), OrganizationPermission("manage_members")],
)
class OrganizationAdminBlacklistController(OrganizationAdminBaseController):
    """Organization blacklist management endpoints."""

    @route.get(
        "/blacklist",
        url_name="list_blacklist_entries",
        response=PaginatedResponseSchema[schema.BlacklistEntrySchema],
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
        params: t.Annotated[filters.BlacklistFilterSchema, Query(...)],
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
        return annotate_holder_known(params.filter(qs).distinct(), organization.pk)

    @route.post(
        "/blacklist",
        url_name="create_blacklist_entry",
        response={201: schema.BlacklistEntrySchema},
    )
    def create_blacklist_entry(self, slug: str, payload: schema.BlacklistCreateSchema) -> tuple[int, models.Blacklist]:
        """Add an entry to the organization blacklist.

        **Quick Mode:**
        Provide `user_id` to auto-populate all fields from the user's profile.
        Returns 404 if the user_id doesn't correspond to an existing user.

        **Manual Mode:**
        Provide individual fields (email, telegram_username, phone_number, names).
        At least one identifier or name is required.

        A manual entry links an account only by the verified email of an active, non-guest account.

        **Both modes:**
        When the account is verified and not a guest, an unlinked entry of the organization that holds its
        email is linked and returned instead of a new row (status 201). Its reason is kept. An empty reason is
        filled with the given reason. In manual mode, a typed phone number or Telegram username fills the entry
        when the entry has none. Names are not applied to the existing entry.

        Returns 400 when:
        - No identifier or name is given (manual mode).
        - The account already has an entry in the organization.
        - A linked entry of another account (for example an erased one) holds the account's verified email.
        - A typed identifier is held by another entry.
        - A typed phone number or Telegram username differs from the one on the linked entry.

        Only the organization owner can create an entry that matches a current
        staff member (by account, email, phone or telegram). Others get 403.
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

        # Refetch with relations: the entry may be an existing one that was linked, so keep its real creator
        entry = models.Blacklist.objects.select_related("user", "organization", "created_by").get(pk=entry.pk)
        return 201, entry

    @route.get(
        "/blacklist/{entry_id}",
        url_name="get_blacklist_entry",
        response=schema.BlacklistEntrySchema,
        throttle=UserDefaultThrottle(),
    )
    def get_blacklist_entry(self, slug: str, entry_id: UUID) -> models.Blacklist:
        """Get details of a specific blacklist entry."""
        organization = self.get_one(slug)
        return get_object_or_404(
            annotate_holder_known(models.Blacklist.objects.select_related("user", "created_by"), organization.pk),
            pk=entry_id,
            organization=organization,
        )

    @route.patch(
        "/blacklist/{entry_id}",
        url_name="update_blacklist_entry",
        response=schema.BlacklistEntrySchema,
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

        return annotate_holder_known(
            models.Blacklist.objects.select_related("user", "created_by"), organization.pk
        ).get(pk=entry.pk)

    @route.delete(
        "/blacklist/{entry_id}",
        url_name="delete_blacklist_entry",
        response={204: None},
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
