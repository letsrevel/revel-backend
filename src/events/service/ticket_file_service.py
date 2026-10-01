"""On-demand ticket file generation with DB-backed caching.

Generates and caches PDF and Apple Wallet (.pkpass) files for tickets.
Files are persisted via ProtectedFileField and served via signed URLs.
A content hash based on updated_at timestamps detects staleness.

Critical: Uses QuerySet.update() instead of model.save() for cache writes
to avoid triggering auto_now on updated_at, which would immediately
invalidate the hash we just stored.
"""

import hashlib
import typing as t
from functools import lru_cache

import structlog
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from events.compliance.enforcement import ticket_fields
from events.models import Ticket

if t.TYPE_CHECKING:
    from wallet.apple.generator import ApplePassGenerator

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_apple_pass_generator() -> "ApplePassGenerator":
    """Get a cached ApplePassGenerator instance.

    Cached because the constructor loads certificates from disk.
    The cache lives for the process lifetime; certificate rotation
    requires a process restart to take effect.
    """
    from wallet.apple.generator import ApplePassGenerator

    return ApplePassGenerator()


def compute_content_hash(ticket: Ticket) -> str:
    """Compute a SHA-256 hash of everything that affects ticket file content.

    Callers must ensure ``ticket.event`` (and ``ticket.tier`` when present)
    are prefetched via ``select_related`` to avoid N+1 queries.

    Args:
        ticket: The ticket to compute the hash for.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    parts = [
        ticket.updated_at.isoformat(),
        ticket.event.updated_at.isoformat(),
    ]
    tier = ticket.tier
    if tier:
        parts.append(tier.updated_at.isoformat())
    # Status and the printed compliance lines (number, issue time, organizer identity, price,
    # country notices) change without bumping any timestamp above — a bulk_update numbering
    # pass, an org billing edit, a price_paid stamp — so they are hashed by value.
    parts.append(ticket.status)
    parts.append("|".join(f"{field.key}={field.value}" for field in ticket_fields(ticket)))
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def is_cache_valid(ticket: Ticket) -> bool:
    """Check whether the cached files are still fresh.

    Args:
        ticket: The ticket to check.

    Returns:
        True if cached files exist and the content hash matches.
    """
    if not ticket.file_content_hash:
        return False
    return ticket.file_content_hash == compute_content_hash(ticket)


def get_or_generate_pdf(ticket: Ticket) -> bytes:
    """Return cached PDF bytes or generate and cache a new one.

    Args:
        ticket: The ticket to get/generate a PDF for.

    Returns:
        PDF file content as bytes.
    """
    if ticket.pdf_file and is_cache_valid(ticket):
        try:
            with ticket.pdf_file.open("rb") as f:
                data: bytes = f.read()
                return data
        except Exception:
            logger.warning("failed_to_read_cached_pdf", ticket_id=str(ticket.id))

    from events.utils import create_ticket_pdf

    pdf_bytes = create_ticket_pdf(ticket)
    _persist_and_update(ticket, pdf_bytes=pdf_bytes)
    return pdf_bytes


def get_or_generate_pkpass(ticket: Ticket) -> bytes:
    """Return cached pkpass bytes or generate and cache a new one.

    Args:
        ticket: The ticket to get/generate a pkpass for.

    Returns:
        pkpass file content as bytes.
    """
    if ticket.pkpass_file and is_cache_valid(ticket):
        try:
            with ticket.pkpass_file.open("rb") as f:
                data: bytes = f.read()
                return data
        except Exception:
            logger.warning("failed_to_read_cached_pkpass", ticket_id=str(ticket.id))

    generator = get_apple_pass_generator()
    pkpass_bytes = generator.generate_pass(ticket)
    _persist_and_update(ticket, pkpass_bytes=pkpass_bytes)
    return pkpass_bytes


def cache_files(ticket: Ticket, pdf_bytes: bytes | None = None, pkpass_bytes: bytes | None = None) -> None:
    """Save pre-generated file bytes to the ticket's cache fields.

    Called from notification templates after generating attachments,
    so subsequent downloads can serve cached files.
    Empty bytes are treated as None (no-op) since persisting an empty
    file would create a corrupt, unopenable artifact.

    Args:
        ticket: The ticket to cache files for.
        pdf_bytes: PDF content to cache (or None/empty to skip).
        pkpass_bytes: pkpass content to cache (or None/empty to skip).
    """
    # Treat empty bytes the same as None — an empty file is not a valid PDF/pkpass.
    pdf_bytes = pdf_bytes or None
    pkpass_bytes = pkpass_bytes or None
    if pdf_bytes is None and pkpass_bytes is None:
        return
    _persist_and_update(ticket, pdf_bytes=pdf_bytes, pkpass_bytes=pkpass_bytes)


def _drop_stale_siblings(
    ticket: Ticket, update_fields: dict[str, object], pdf_bytes: bytes | None, pkpass_bytes: bytes | None
) -> list[str]:
    """Clear the cached format(s) not just rendered, when the shared content hash changes.

    PDF and pkpass share one ``file_content_hash``: a format we didn't just render was
    rendered for other content, so it is dropped (DB via ``update_fields``, and the
    in-memory ticket) instead of letting the new hash vouch for it.

    Returns:
        Storage names of the dropped files, for the caller's best-effort cleanup.
    """
    dropped: list[str] = []
    for field_name, written in (("pdf_file", pdf_bytes), ("pkpass_file", pkpass_bytes)):
        sibling = getattr(ticket, field_name)
        if written is None and sibling:
            dropped.append(sibling.name)
            update_fields[field_name] = None
            setattr(ticket, field_name, None)
    return dropped


def _persist_and_update(
    ticket: Ticket,
    pdf_bytes: bytes | None = None,
    pkpass_bytes: bytes | None = None,
) -> None:
    """Save files to storage and update the DB via QuerySet.update().

    Uses a write-then-swap strategy: new files are saved first, then old
    files are deleted. This avoids partial state where the DB references a
    deleted file if saving the second file fails.
    Uses QuerySet.update() to bypass auto_now on updated_at.

    Note: concurrent requests may both generate and persist files. This is
    a best-effort cache — the worst case is an orphaned file on disk,
    cleaned up by the daily cleanup task.

    Args:
        ticket: The ticket to update.
        pdf_bytes: PDF content to save (or None to skip).
        pkpass_bytes: pkpass content to save (or None to skip).
    """
    update_fields: dict[str, object] = {}
    old_files: list[str] = []
    ticket_id_short = str(ticket.id).split("-")[0]

    try:
        # Phase 1: Save new files (keep old files intact until all writes succeed)
        if pdf_bytes is not None:
            old_pdf_name = ticket.pdf_file.name if ticket.pdf_file else None
            filename = f"ticket_{ticket_id_short}.pdf"
            ticket.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)
            update_fields["pdf_file"] = ticket.pdf_file.name
            if old_pdf_name:
                old_files.append(old_pdf_name)

        if pkpass_bytes is not None:
            old_pkpass_name = ticket.pkpass_file.name if ticket.pkpass_file else None
            filename = f"ticket_{ticket_id_short}.pkpass"
            ticket.pkpass_file.save(filename, ContentFile(pkpass_bytes), save=False)
            update_fields["pkpass_file"] = ticket.pkpass_file.name
            if old_pkpass_name:
                old_files.append(old_pkpass_name)

        if update_fields:
            content_hash = compute_content_hash(ticket)
            if content_hash != ticket.file_content_hash:
                old_files += _drop_stale_siblings(ticket, update_fields, pdf_bytes, pkpass_bytes)
            update_fields["file_content_hash"] = content_hash
            Ticket.objects.filter(pk=ticket.pk).update(**update_fields)
            ticket.file_content_hash = content_hash

        # Phase 2: Clean up old files (best-effort, orphans cleaned by daily task)
        for old_name in old_files:
            try:
                default_storage.delete(old_name)
            except OSError:
                logger.debug("could_not_delete_old_file", file_name=old_name, exc_info=True)
    except OSError:
        logger.warning("failed_to_persist_cached_files", ticket_id=str(ticket.id), exc_info=True)
