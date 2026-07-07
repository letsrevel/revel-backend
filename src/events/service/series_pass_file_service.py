"""On-demand series pass file generation with DB-backed caching.

Generates and caches PDF and Apple Wallet (.pkpass) files for series passes.
Mirrors ``ticket_file_service``'s get-or-generate + content-hash caching
contract exactly (same write-then-swap persistence, same ``QuerySet.update()``
trick to avoid invalidating the hash via ``auto_now``).

QR/barcode payload is ``f"series:{held_pass.id}"`` — this is the check-in
resolution contract (see ``events.service.ticket_service.resolve_check_in_ticket_id``),
not just a naming convention.
"""

import hashlib

import structlog
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from events.models import HeldSeriesPass
from events.service.ticket_file_service import get_apple_pass_generator

logger = structlog.get_logger(__name__)


def compute_content_hash(held_pass: HeldSeriesPass) -> str:
    """Compute a SHA-256 hash of timestamps that affect series pass file content.

    Callers must ensure ``held_pass.series_pass`` is prefetched via
    ``select_related`` to avoid N+1 queries. Includes every covered event's
    ``updated_at`` so adding/removing a covered event (via ``SeriesPassTierLink``)
    invalidates the cache too, not just edits to the pass or held-pass rows.

    Args:
        held_pass: The series pass to compute the hash for.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    parts = [
        held_pass.updated_at.isoformat(),
        held_pass.series_pass.updated_at.isoformat(),
    ]
    links = held_pass.series_pass.tier_links.select_related("event").order_by("event_id")
    for link in links:
        parts.append(f"{link.event_id}:{link.event.updated_at.isoformat()}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def is_cache_valid(held_pass: HeldSeriesPass) -> bool:
    """Check whether the cached files are still fresh.

    Args:
        held_pass: The series pass to check.

    Returns:
        True if cached files exist and the content hash matches.
    """
    if not held_pass.file_content_hash:
        return False
    return held_pass.file_content_hash == compute_content_hash(held_pass)


def get_or_generate_pass_pdf(held_pass: HeldSeriesPass) -> bytes:
    """Return cached PDF bytes or generate and cache a new one.

    Args:
        held_pass: The series pass to get/generate a PDF for.

    Returns:
        PDF file content as bytes.
    """
    if held_pass.pdf_file and is_cache_valid(held_pass):
        try:
            with held_pass.pdf_file.open("rb") as f:
                data: bytes = f.read()
                return data
        except Exception:
            logger.warning("failed_to_read_cached_series_pass_pdf", held_pass_id=str(held_pass.id))

    from events.utils import create_series_pass_pdf

    pdf_bytes = create_series_pass_pdf(held_pass)
    _persist_and_update(held_pass, pdf_bytes=pdf_bytes)
    return pdf_bytes


def get_or_generate_pass_pkpass(held_pass: HeldSeriesPass) -> bytes:
    """Return cached pkpass bytes or generate and cache a new one.

    Args:
        held_pass: The series pass to get/generate a pkpass for.

    Returns:
        pkpass file content as bytes.
    """
    if held_pass.pkpass_file and is_cache_valid(held_pass):
        try:
            with held_pass.pkpass_file.open("rb") as f:
                data: bytes = f.read()
                return data
        except Exception:
            logger.warning("failed_to_read_cached_series_pass_pkpass", held_pass_id=str(held_pass.id))

    generator = get_apple_pass_generator()
    pkpass_bytes = generator.generate_series_pass(held_pass)
    _persist_and_update(held_pass, pkpass_bytes=pkpass_bytes)
    return pkpass_bytes


def _persist_and_update(
    held_pass: HeldSeriesPass,
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
        held_pass: The series pass to update.
        pdf_bytes: PDF content to save (or None to skip).
        pkpass_bytes: pkpass content to save (or None to skip).
    """
    update_fields: dict[str, object] = {}
    old_files: list[str] = []
    held_pass_id_short = str(held_pass.id).split("-")[0]

    try:
        # Phase 1: Save new files (keep old files intact until all writes succeed)
        if pdf_bytes is not None:
            old_pdf_name = held_pass.pdf_file.name if held_pass.pdf_file else None
            filename = f"series_pass_{held_pass_id_short}.pdf"
            held_pass.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)
            update_fields["pdf_file"] = held_pass.pdf_file.name
            if old_pdf_name:
                old_files.append(old_pdf_name)

        if pkpass_bytes is not None:
            old_pkpass_name = held_pass.pkpass_file.name if held_pass.pkpass_file else None
            filename = f"series_pass_{held_pass_id_short}.pkpass"
            held_pass.pkpass_file.save(filename, ContentFile(pkpass_bytes), save=False)
            update_fields["pkpass_file"] = held_pass.pkpass_file.name
            if old_pkpass_name:
                old_files.append(old_pkpass_name)

        if update_fields:
            content_hash = compute_content_hash(held_pass)
            update_fields["file_content_hash"] = content_hash
            HeldSeriesPass.objects.filter(pk=held_pass.pk).update(**update_fields)

        # Phase 2: Clean up old files (best-effort, orphans cleaned by daily task)
        for old_name in old_files:
            try:
                default_storage.delete(old_name)
            except OSError:
                logger.debug("could_not_delete_old_file", file_name=old_name, exc_info=True)
    except OSError:
        logger.warning("failed_to_persist_cached_series_pass_files", held_pass_id=str(held_pass.id), exc_info=True)
