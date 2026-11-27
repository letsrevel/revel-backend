"""Find, and optionally delete, stored image files that no row names (#1548).

The reference-checked delete after commit (``files.delete_unreferenced_files``) misses some files: a delete that
failed, a queryset delete or a cascade that skips ``delete()``, and files from before that delete existed. This
sweep finds them in the image upload folders.

A file is a candidate when it is older than :data:`MIN_AGE` and no row names it in a source or derived image field.
The age is the file's modification time (its upload), so the grace period keeps an upload in progress safe. It is
not the time since a row last named the file, so it does not protect a backup restore; the delete after commit
does not either. Deletes run under the same path locks as the
delete after commit, and the reference check runs again under those locks.

The first runs are a dry run: they only count. The counts include the inverse check (paths that rows name but
storage lacks): a large number means that the path matching is wrong, and deletes must stay off.

The log has counts only: a path can hold a user id.
"""

import dataclasses
import typing as t
from datetime import timedelta

import structlog
from django.apps import apps
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from .config import THUMBNAIL_CONFIGS
from .files import lock_image_paths, named_paths

logger = structlog.get_logger(__name__)

#: Listed without recursion: subfolders (seed and demo images) are not swept.
FLAT_FOLDERS = ("logos", "cover-art", "oauth-logos")
#: One subfolder per user, each listed without recursion.
PER_USER_FOLDER = "protected/profile-pictures"
#: Measured from the file's modification time (its upload).
MIN_AGE = timedelta(days=7)
#: The most files that one run deletes.
DELETE_BATCH = 500
#: The reference check runs on this many paths at a time.
CHECK_CHUNK = 500


@dataclasses.dataclass
class SweepCounts:
    """What a sweep found. Counts only: no path."""

    files_listed: int = 0
    files_old_enough: int = 0
    files_unreferenced: int = 0
    files_deleted: int = 0
    files_failed: int = 0
    named_paths_missing: int = 0


def _list_files() -> list[str]:
    paths: list[str] = []
    for folder in FLAT_FOLDERS:
        paths += [f"{folder}/{name}" for name in _listdir(folder)[1]]
    for user_folder in _listdir(PER_USER_FOLDER)[0]:
        folder = f"{PER_USER_FOLDER}/{user_folder}"
        paths += [f"{folder}/{name}" for name in _listdir(folder)[1]]
    return sorted(paths)


def _listdir(folder: str) -> tuple[list[str], list[str]]:
    try:
        return default_storage.listdir(folder)
    except FileNotFoundError:  # no upload ever made in this folder
        return [], []


def _older_than(paths: list[str], cutoff: t.Any) -> list[str]:
    old: list[str] = []
    for path in paths:
        try:
            if default_storage.get_modified_time(path) < cutoff:
                old.append(path)
        except FileNotFoundError:  # deleted meanwhile
            continue
    return old


def _chunks(paths: list[str]) -> t.Iterator[list[str]]:
    for start in range(0, len(paths), CHECK_CHUNK):
        yield paths[start : start + CHECK_CHUNK]


def _in_swept_folders(path: str) -> bool:
    folder, _, name = path.rpartition("/")
    return name != "" and (folder in FLAT_FOLDERS or folder.rpartition("/")[0] == PER_USER_FOLDER)


def _count_missing_named_paths() -> int:
    """How many distinct paths in the swept folders that rows name are not in storage (the inverse check)."""
    named: set[str] = set()
    for config in THUMBNAIL_CONFIGS.values():
        names = [config.source_field, *(spec.field_name for spec in config.specs)]
        for row in apps.get_model(config.app_label, config.model_name).objects.values_list(*names):
            named.update(path for path in row if path and _in_swept_folders(path))
    return sum(1 for path in named if not default_storage.exists(path))


def _delete_unreferenced(paths: list[str], counts: SweepCounts) -> None:
    """Delete the files of ``paths`` that no row names, checked again under the path locks."""
    with transaction.atomic():
        lock_image_paths(paths)
        in_use = named_paths(paths)
        for path in paths:
            if path in in_use or counts.files_deleted >= DELETE_BATCH:
                continue
            try:
                default_storage.delete(path)
            except FileNotFoundError:
                continue
            except OSError as e:  # ponytail: FileSystemStorage only; a remote storage needs its own error types
                counts.files_failed += 1
                logger.error("unreferenced_image_file_delete_failed", error_type=type(e).__name__)
                continue
            counts.files_deleted += 1


def sweep_unreferenced_image_files(*, dry_run: bool = True) -> SweepCounts:
    """Count the image files that no row names; with ``dry_run=False``, delete up to :data:`DELETE_BATCH` of them."""
    counts = SweepCounts()
    listed = _list_files()
    counts.files_listed = len(listed)
    old = _older_than(listed, timezone.now() - MIN_AGE)
    counts.files_old_enough = len(old)
    for chunk in _chunks(old):
        in_use = named_paths(chunk)
        unreferenced = [path for path in chunk if path not in in_use]
        counts.files_unreferenced += len(unreferenced)
        if not dry_run and unreferenced and counts.files_deleted < DELETE_BATCH:
            _delete_unreferenced(unreferenced, counts)
    counts.named_paths_missing = _count_missing_named_paths()
    logger.info("unreferenced_image_files_swept", dry_run=dry_run, **dataclasses.asdict(counts))
    return counts
