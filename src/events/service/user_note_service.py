"""Private organizer notes about users (see ``OrganizationUserNote``)."""

import html
import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.sanitizers import sanitize_markdown
from common.utils import get_or_create_with_race_protection
from events.models import (
    HeldSeriesPass,
    MembershipRecord,
    Organization,
    OrganizationMember,
    OrganizationUserNote,
    Ticket,
)
from events.models.user_note import USER_NOTE_MAX_LENGTH


def get_note(organization: Organization, user_id: UUID) -> OrganizationUserNote:
    """Return the organization's note about ``user_id`` or raise 404."""
    return get_object_or_404(
        OrganizationUserNote.objects.select_related("updated_by"), organization=organization, user_id=user_id
    )


def get_note_body(organization_id: UUID, user_id: UUID) -> str | None:
    """Return just the note body (one query), or ``None`` when there is no note."""
    return (
        OrganizationUserNote.objects.filter(organization_id=organization_id, user_id=user_id)
        .values_list("body", flat=True)
        .first()
    )


def _has_relationship(organization: Organization, user_id: UUID) -> bool:
    """Whether the user is a member, ticket holder or pass holder of the organization."""
    return (
        OrganizationMember.objects.filter(organization=organization, user_id=user_id).exists()
        or Ticket.objects.filter(event__organization=organization, user_id=user_id).exists()
        or HeldSeriesPass.objects.filter(series_pass__event_series__organization=organization, user_id=user_id).exists()
    )


def _check_length(body: str) -> None:
    if len(html.unescape(body)) > USER_NOTE_MAX_LENGTH:
        raise HttpError(400, str(_("The note is too long.")))


def _check_not_empty(body: str) -> None:
    if not sanitize_markdown(body).strip():
        raise HttpError(400, str(_("The note is empty.")))


def upsert_note(organization: Organization, user_id: UUID, body: str, updated_by: RevelUser) -> OrganizationUserNote:
    """Create or replace the note about ``user_id`` and record who wrote it.

    The 5000-character cap applies to the unescaped text, so a sanitized (HTML-escaped) body
    returned by the API and saved back unchanged never trips it. ``MarkdownField`` sanitizes on
    save and is idempotent, so the round trip is stable (same as event descriptions).

    Raises:
        Http404: The user does not exist or has no relationship with the organization (same
            answer for both, so the endpoint is no existence oracle).
        HttpError: 400 when the body is empty after stripping/sanitizing or exceeds the cap.
    """
    body = body.strip()
    _check_length(body)
    user = get_object_or_404(RevelUser, pk=user_id)
    if not _has_relationship(organization, user_id):
        raise Http404
    _check_not_empty(body)
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


def get_record_note(organization: Organization, record_id: UUID) -> OrganizationUserNote:
    """Return the note of a register entry (404 if none): the user's note once linked, else its own."""
    record = get_object_or_404(MembershipRecord, pk=record_id, organization=organization)
    if record.user_id is not None:
        return get_note(organization, record.user_id)
    return get_object_or_404(
        OrganizationUserNote.objects.select_related("updated_by"), organization=organization, record=record
    )


def _upsert(
    organization: Organization, target: Q, create: dict[str, t.Any], body: str, updated_by: RevelUser
) -> OrganizationUserNote:
    note, created = get_or_create_with_race_protection(
        OrganizationUserNote,
        target,
        {"organization": organization, "body": body, "updated_by": updated_by, **create},
    )
    if not created:
        note.body = body
        note.updated_by = updated_by
        note.save(update_fields=["body", "updated_by", "updated_at"])
    return note


def _lock_record(organization: Organization, record_id: UUID) -> MembershipRecord:
    record = (
        MembershipRecord.objects.select_for_update(of=("self",)).filter(pk=record_id, organization=organization).first()
    )
    if record is None:
        raise Http404
    return record


def upsert_record_note(
    organization: Organization, record_id: UUID, body: str, updated_by: RevelUser
) -> OrganizationUserNote:
    """Create or replace the note of a register entry and record who wrote it.

    A linked entry proxies to its user's note (the entry is the relationship, so no relationship check);
    an unlinked one keeps its own note, which moves onto the user's note when the entry gets linked.

    Raises:
        Http404: The entry does not exist in the organization.
        HttpError: 400 when the body is empty or too long, or the entry was merged into another one.
    """
    body = body.strip()
    _check_length(body)
    _check_not_empty(body)
    with transaction.atomic():
        record = _lock_record(organization, record_id)
        if record.merged_into_id is not None:
            raise HttpError(
                400,
                str(
                    _("A merged register entry has no note of its own; edit the note on the entry it was merged into.")
                ),
            )
        if record.user_id is not None:
            return _upsert(
                organization,
                Q(organization=organization, user_id=record.user_id),
                {"user_id": record.user_id},
                body,
                updated_by,
            )
        return _upsert(organization, Q(organization=organization, record=record), {"record": record}, body, updated_by)


def delete_record_note(organization: Organization, record_id: UUID) -> None:
    """Delete the note of a register entry (the user's note when linked); 404 if none."""
    with transaction.atomic():
        _lock_record(organization, record_id)
        get_record_note(organization, record_id).delete()
