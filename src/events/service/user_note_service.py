"""Private organizer notes about users (see ``OrganizationUserNote``)."""

from uuid import UUID

from django.db.models import Q
from django.shortcuts import get_object_or_404

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import Organization, OrganizationUserNote


def get_note(organization: Organization, user_id: UUID) -> OrganizationUserNote:
    """Return the organization's note about ``user_id`` or raise 404."""
    return get_object_or_404(OrganizationUserNote, organization=organization, user_id=user_id)


def get_note_body(organization_id: UUID, user_id: UUID) -> str | None:
    """Return just the note body (one query), or ``None`` when there is no note."""
    return (
        OrganizationUserNote.objects.filter(organization_id=organization_id, user_id=user_id)
        .values_list("body", flat=True)
        .first()
    )


def upsert_note(organization: Organization, user_id: UUID, body: str, updated_by: RevelUser) -> OrganizationUserNote:
    """Create or replace the note about ``user_id`` and record who wrote it."""
    user = get_object_or_404(RevelUser, pk=user_id)
    note, created = get_or_create_with_race_protection(
        OrganizationUserNote,
        Q(organization=organization, user=user),
        {"organization": organization, "user": user, "body": body, "updated_by": updated_by},
    )
    if not created:
        note.body = body
        note.updated_by = updated_by
        note.save(update_fields=["body", "updated_by", "updated_at"])
    return note


def delete_note(organization: Organization, user_id: UUID) -> None:
    """Delete the note about ``user_id`` (404 if none)."""
    get_note(organization, user_id).delete()
