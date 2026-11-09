"""Moving a register entry's private note when the entry gets linked or merged (see ``OrganizationUserNote``)."""

import html
from uuid import UUID

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from events.models import MembershipRecord, OrganizationUserNote
from events.models.user_note import USER_NOTE_MAX_LENGTH
from events.utils import get_organization_timezone

logger = structlog.get_logger(__name__)

_TRUNCATED_MARKER = "…"


def _merged_text(
    target: OrganizationUserNote, source: OrganizationUserNote, record: MembershipRecord
) -> tuple[str, bool, bool]:
    """Target text, a dated separator and the entry's text, capped. Returns (text, truncated, dropped).

    The target's own text is never cut; when even the separator and the truncation marker do not fit, the
    entry's text is dropped entirely (``dropped``).
    """
    target_text = html.unescape(target.body)
    record_text = html.unescape(source.body)
    local_date = source.updated_at.astimezone(get_organization_timezone(record.organization)).date()
    head = "\n\n" + str(_("— From register entry ({date}) —")).format(date=local_date.isoformat()) + "\n\n"
    appended = head + record_text
    if len(target_text) + len(appended) <= USER_NOTE_MAX_LENGTH:
        return target_text + appended, False, False
    suffix = f"{_TRUNCATED_MARKER} {str(_('[truncated]'))}"
    room = USER_NOTE_MAX_LENGTH - len(target_text) - len(suffix)
    if room < len(head):
        return target_text, True, True
    return target_text + appended[:room] + suffix, True, False


def _merge_into(target: OrganizationUserNote, source: OrganizationUserNote, record: MembershipRecord) -> None:
    """Append ``source`` to ``target`` and delete ``source``; the later editor/stamp of the two wins."""
    text, truncated, dropped = _merged_text(target, source, record)
    latest = source if source.updated_at > target.updated_at else target
    latest_at = latest.updated_at
    target.body = text
    target.updated_by_id = latest.updated_by_id
    # ``auto_now`` rewrites ``updated_at`` on any save that includes it: restore the later stamp via update().
    target.save(update_fields=["body", "updated_by"])
    OrganizationUserNote.objects.filter(pk=target.pk).update(updated_at=latest_at)
    target.updated_at = latest_at
    source.delete()
    if truncated:
        logger.info(
            "organizer_note_transfer_truncated", note_id=str(target.pk), record_id=str(record.pk), dropped=dropped
        )


def transfer_record_note(
    record: MembershipRecord, *, to_user_id: UUID | None = None, to_record: MembershipRecord | None = None
) -> None:
    """Move ``record``'s own note to the user it was just linked to, or to the record it was merged into.

    Callers already hold the member-row and record locks (lock order: member row, record, note). Without a
    target note the note row is re-pointed (``updated_at``/``updated_by`` are kept); with one, the texts are
    merged (target first, a dated separator, then the entry's text) under the 5000-character cap, never cutting
    the target's own text. A note must never make a link fail, so nothing here raises for note reasons.
    """
    if (to_user_id is None) == (to_record is None):
        raise ValueError("Exactly one of to_user_id or to_record is required.")
    source = OrganizationUserNote.objects.select_for_update(of=("self",)).filter(record=record).first()
    if source is None:
        return
    target_filter = {"user_id": to_user_id} if to_user_id is not None else {"record": to_record}
    target = (
        OrganizationUserNote.objects.select_for_update(of=("self",))
        .filter(organization_id=record.organization_id, **target_filter)
        .order_by("pk")
        .first()
    )
    if target is None:
        try:
            with transaction.atomic():
                if to_user_id is not None:
                    source.user_id, source.record = to_user_id, None
                else:
                    source.record = to_record
                source.save(update_fields=["user", "record"])
            return
        except IntegrityError, ValidationError:
            # A concurrent PUT created the target note meanwhile: merge into it instead.
            source = OrganizationUserNote.objects.select_for_update(of=("self",)).get(pk=source.pk)
            target = OrganizationUserNote.objects.select_for_update(of=("self",)).get(
                organization_id=record.organization_id, **target_filter
            )
    _merge_into(target, source, record)
