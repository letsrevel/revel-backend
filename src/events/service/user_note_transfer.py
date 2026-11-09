"""Moving a register entry's private note when the entry gets linked or merged (see ``OrganizationUserNote``)."""

import html
from uuid import UUID

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from common.sanitizers import sanitize_markdown
from events.models import MembershipRecord, OrganizationUserNote
from events.models.user_note import USER_NOTE_MAX_LENGTH
from events.utils import get_organization_timezone

logger = structlog.get_logger(__name__)

_TRUNCATED_MARKER = "…"


def _fits(candidate: str) -> bool:
    """What the next PUT would measure: the cap applies to the unescaped, sanitized text."""
    return len(html.unescape(sanitize_markdown(candidate))) <= USER_NOTE_MAX_LENGTH


def _merged_text(
    target: OrganizationUserNote, source: OrganizationUserNote, record: MembershipRecord
) -> tuple[str, bool, bool]:
    """Target body, a dated separator and the entry's body, capped. Returns (text, truncated, dropped).

    Built from the stored (already sanitized) bodies so the target's text is kept byte-identical. When the
    whole does not fit, the entry's part is cut (never inside an HTML entity), the marker appended and the
    result re-sanitized and re-measured; if no fitting text still carrying the marker is found, the entry's text
    is dropped and the target stays as it was (``dropped``).
    """
    local_date = source.updated_at.astimezone(get_organization_timezone(record.organization)).date()
    head = "\n\n" + str(_("— From register entry ({date}) —")).format(date=local_date.isoformat()) + "\n\n"
    full = target.body + head + source.body
    if _fits(full):
        return full, False, False
    marker = f"{_TRUNCATED_MARKER} {str(_('[truncated]'))}"
    cut = USER_NOTE_MAX_LENGTH - len(html.unescape(target.body)) - len(head) - len(marker)
    cut = min(cut, len(source.body))
    for _attempt in range(20):
        if cut < 0:
            break
        piece = source.body[:cut]
        amp = piece.rfind("&")
        if amp != -1 and ";" not in piece[amp:]:
            piece = piece[:amp]
        lt = piece.rfind("<")  # stored bodies only keep real tags as "<": never leave one half-written
        if lt > piece.rfind(">"):
            piece = piece[:lt]
        candidate = target.body + head + piece + marker
        sanitized = sanitize_markdown(candidate)
        if marker in sanitized and sanitized.startswith(target.body):
            excess = len(html.unescape(sanitized)) - USER_NOTE_MAX_LENGTH
            if excess <= 0:
                return candidate, True, False
            cut -= max(excess, 1)
        else:
            cut -= 1
    return target.body, True, True


def _merge_into(target: OrganizationUserNote, source: OrganizationUserNote, record: MembershipRecord) -> None:
    """Append ``source`` to ``target`` and delete ``source``; the later editor/stamp of the two wins."""
    text, truncated, dropped = _merged_text(target, source, record)
    latest = target if dropped or source.updated_at <= target.updated_at else source
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
    note_id = source.pk
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
            source = OrganizationUserNote.objects.select_for_update(of=("self",)).filter(pk=source.pk).first()
            target = (
                OrganizationUserNote.objects.select_for_update(of=("self",))
                .filter(organization_id=record.organization_id, **target_filter)
                .first()
            )
            if source is None or target is None:
                # The failure was something else: a note must never make the link fail.
                logger.warning("organizer_note_transfer_failed", record_id=str(record.pk), note_id=str(note_id))
                return
    _merge_into(target, source, record)
