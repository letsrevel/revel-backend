"""Delete stored image files that no row names any more.

Rows can share an image file: a duplicated or recurring event keeps the template's logo, cover art and their
derived images. So a file is deleted only when no row of a model with thumbnails names it, and only after the
commit that removed the reference (a rollback cannot restore a deleted file).

This module has no service imports, so models can use it too.
"""

import typing as t

import structlog
from django.apps import apps
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .config import THUMBNAIL_CONFIGS

logger = structlog.get_logger(__name__)


def delete_thumbnails_for_paths(paths: list[str]) -> None:
    """Delete files from storage, without a reference check. A failed delete is logged.

    Args:
        paths: Storage paths to delete. Empty paths are skipped.
    """
    for path in paths:
        if not path:
            continue
        try:
            if default_storage.exists(path):
                default_storage.delete(path)
                logger.info("thumbnail_deleted", path=path)
        except Exception as e:
            logger.error(
                "thumbnail_delete_failed",
                path=path,
                error=str(e),
            )


def delete_unreferenced_files(paths: list[str]) -> None:
    """Delete the files in ``paths`` that no row names in a source or derived image field.

    Call it after the commit that removed the reference.

    The check and the delete hold an advisory lock on each path, as the derived-path write in
    ``write_paths_if_source_unchanged`` does: a write either commits first (the file is kept) or finds its file
    gone (the task retries; the ``generate_thumbnails --sync`` command reports the row as failed).

    A duplication reads the template's image paths under the template's row lock, which every write of image
    paths (upload, thumbnail write, image delete) also takes, so a copy is committed before the check or reads
    the new paths.
    """
    paths = [path for path in paths if path]
    if not paths:
        return
    with transaction.atomic():
        lock_image_paths(paths)
        in_use = named_paths(paths)
        delete_thumbnails_for_paths([path for path in paths if path not in in_use])


def named_paths(paths: list[str]) -> set[str]:
    """The paths in ``paths`` that a row names in a source or derived image field."""
    in_use: set[str] = set()
    for config in THUMBNAIL_CONFIGS.values():
        names = [config.source_field, *(spec.field_name for spec in config.specs)]
        query = Q()
        for name in names:
            query |= Q(**{f"{name}__in": paths})
        for row in apps.get_model(config.app_label, config.model_name).objects.filter(query).values_list(*names):
            in_use.update(row)
    return in_use & set(paths)


# The first key of the two-key advisory lock: it keeps these locks apart from other advisory-lock users.
_IMAGE_PATH_LOCK_CLASS = 7_314_001


def lock_image_paths(paths: list[str]) -> None:
    """Take a transaction-level advisory lock for each image path.

    The locks are taken in the order of their keys (not of the paths), so two callers cannot deadlock, even
    when two paths share a key. A shared key only makes the callers wait for each other.
    """
    if not paths:
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT hashtext(p) AS h FROM unnest(%s::text[]) AS p ORDER BY h", [sorted(set(paths))])
        for (key,) in cursor.fetchall():
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_IMAGE_PATH_LOCK_CLASS, key])


def delete_unreferenced_files_after_commit(paths: list[str]) -> None:
    """After the current transaction commits, delete the files in ``paths`` that no row names."""
    paths = [path for path in paths if path]
    if not paths:
        return

    def delete_unreferenced_image_files() -> None:
        delete_unreferenced_files(paths)

    # robust: a failed cleanup must not fail the change that committed; the files then stay (harmless).
    transaction.on_commit(delete_unreferenced_image_files, robust=True)


def stored_image_paths(model_class: type, pk: object, source_field: str) -> list[str]:
    """The stored paths of ``source_field`` and of its derived images on the row ``pk`` (empty ones dropped).

    Locks the row: call it inside the transaction that changes the image.
    """
    meta = model_class._meta  # type: ignore[attr-defined]
    config = THUMBNAIL_CONFIGS.get((meta.app_label, meta.model_name, source_field))
    names = [source_field, *(spec.field_name for spec in config.specs)] if config else [source_field]
    # NO KEY: only image columns change, so other transactions' foreign-key checks (KEY SHARE) can pass.
    rows = model_class.objects.select_for_update(no_key=True).filter(pk=pk)  # type: ignore[attr-defined]
    row = rows.values_list(*names).first()
    return [name for name in row or () if name]


# The documented lock order (docs/engineering-notes.md, "Lock order"): RevelUser first, then
# Organization -> EventSeries -> Event, then the rest.
_LOCK_RANK = {"reveluser": -1, "organization": 0, "eventseries": 1, "event": 2}


def detach_image_everywhere(source_path: str) -> list[str]:
    """Clear ``source_path`` and its derived images on every row that names it as a source; return their paths.

    A duplicated event shares its image paths with the template, so an image that must go is removed from every
    row that names it. Only rows that still name the path change: a newer upload stays. Only nullable source
    fields are cleared. Call it inside a transaction, and delete the returned paths after commit
    (:func:`delete_unreferenced_files_after_commit`).
    """
    paths = [source_path]
    configs = sorted(THUMBNAIL_CONFIGS.values(), key=lambda config: _LOCK_RANK.get(config.model_name, 3))
    for config in configs:
        model = apps.get_model(config.app_label, config.model_name)
        if not model._meta.get_field(config.source_field).null:
            continue
        derived = [spec.field_name for spec in config.specs]
        rows = model._default_manager.filter(**{config.source_field: source_path})
        locked = rows.select_for_update(no_key=True).order_by("pk")  # pk order: no deadlock between two of these
        for row in locked.values_list(*derived):
            paths += [name for name in row if name]
        cleared: dict[str, t.Any] = dict.fromkeys([config.source_field, *derived])
        if any(field.name == "updated_at" for field in model._meta.concrete_fields):
            cleared["updated_at"] = timezone.now()  # cached files built from the row (ticket PDFs) are made again
        rows.update(**cleared)
    return paths
