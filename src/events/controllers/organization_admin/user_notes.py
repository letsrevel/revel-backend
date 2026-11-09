"""Org-admin private notes about a person (staff-only, #1314)."""

from uuid import UUID

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import user_note_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    # Notes are session-only for reads and writes: no OAuth scope's consent label promises access to
    # private notes about people, and ``private_note`` is nulled for app tokens on every other route.
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminUserNotesController(OrganizationAdminBaseController):
    """Private organizer notes about members and attendees.

    One editable note per (organization, user). Door staff with ``check_in_attendees`` only see the
    note, read-only, in scan responses.
    """

    @route.get(
        "/users/{user_id}/note",
        url_name="get_user_note",
        response=schema.UserNoteSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_note(self, slug: str, user_id: UUID) -> models.OrganizationUserNote:
        """Get the private note about a user (404 if none)."""
        return user_note_service.get_note(self.get_one(slug), user_id)

    @route.put("/users/{user_id}/note", url_name="upsert_user_note", response=schema.UserNoteSchema)
    def upsert_note(
        self, slug: str, user_id: UUID, payload: schema.UserNoteUpsertSchema
    ) -> models.OrganizationUserNote:
        """Create or replace the private note about a user."""
        return user_note_service.upsert_note(self.get_one(slug), user_id, payload.body, self.user())

    @route.delete("/users/{user_id}/note", url_name="delete_user_note", response={204: None})
    def delete_note(self, slug: str, user_id: UUID) -> tuple[int, None]:
        """Delete the private note about a user."""
        user_note_service.delete_note(self.get_one(slug), user_id)
        return 204, None

    @route.get(
        "/member-register/{record_id}/note",
        url_name="get_member_register_record_note",
        response=schema.UserNoteSchema,
        throttle=UserDefaultThrottle(),
    )
    def get_record_note(self, slug: str, record_id: UUID) -> models.OrganizationUserNote:
        """Get the private note of a register entry (the account's note once linked; 404 if none)."""
        return user_note_service.get_record_note(self.get_one(slug), record_id)

    @route.put(
        "/member-register/{record_id}/note",
        url_name="upsert_member_register_record_note",
        response=schema.UserNoteSchema,
    )
    def upsert_record_note(
        self, slug: str, record_id: UUID, payload: schema.UserNoteUpsertSchema
    ) -> models.OrganizationUserNote:
        """Create or replace the private note of a register entry (the account's note once linked)."""
        return user_note_service.upsert_record_note(self.get_one(slug), record_id, payload.body, self.user())

    @route.delete(
        "/member-register/{record_id}/note", url_name="delete_member_register_record_note", response={204: None}
    )
    def delete_record_note(self, slug: str, record_id: UUID) -> tuple[int, None]:
        """Delete the private note of a register entry (the account's note once linked)."""
        user_note_service.delete_record_note(self.get_one(slug), record_id)
        return 204, None
