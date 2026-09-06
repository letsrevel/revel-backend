# src/events/management/commands/demo_video_helpers/artwork.py
"""Attach the bundled demo-video artwork to its organizations and events.

The demo scenarios are recorded on camera, so a bare card with no logo and a
grey cover-art placeholder is exactly what the product videos must not show.
Each organization gets a square logo and each event a 16:9 cover from
``assets/``; the file is found by the row's own slug, so a scenario whose slug
changes fails loudly instead of quietly seeding no artwork (the trap of #663).

This mirrors ``bootstrap_helpers/cover_art.py`` deliberately: the images are
trusted repo assets, already stripped of metadata, and they bypass the upload
pipeline (no file audit, no malware scan, no async Celery tasks). Files are
cached in storage under deterministic ``…/demo-video/`` paths, so a re-run only
*links* what is already there — nothing is re-uploaded or re-encoded.

Sources and licences for every file are listed in ``assets/IMAGE_CREDITS.md``.
"""

import typing as t
from pathlib import Path

import structlog
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.db import models

from common.thumbnails.config import THUMBNAIL_CONFIGS
from common.thumbnails.service import generate_and_save_thumbnails, get_thumbnail_path
from events import models as events_models

logger = structlog.get_logger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_STORAGE_PREFIX = "logos/demo-video"
COVER_STORAGE_PREFIX = "cover-art/demo-video"


def _attach(instance: models.Model, *, field: str, asset: Path, storage_prefix: str) -> None:
    """Link a bundled asset onto ``instance.<field>``, uploading it only once.

    The file and its thumbnails live at deterministic storage paths; when they
    already exist they are reused and only the model fields are updated (via
    ``queryset.update()``, so no model ``save()`` side effects run).
    """
    if getattr(instance, field):
        return  # already set — keep the seed idempotent

    target = f"{storage_prefix}/{asset.name}"
    if not default_storage.exists(target):
        with asset.open("rb") as fh:
            default_storage.save(target, File(fh))

    config = THUMBNAIL_CONFIGS[(instance._meta.app_label, t.cast(str, instance._meta.model_name), field)]
    thumbs = {spec.field_name: get_thumbnail_path(target, spec.field_name) for spec in config.specs}
    if not all(default_storage.exists(path) for path in thumbs.values()):
        result = generate_and_save_thumbnails(target, config)
        thumbs = result.thumbnails
        if result.has_failures:
            logger.warning("Demo artwork thumbnail generation failed", asset=asset.name, failures=result.failures)

    updates: dict[str, str] = {field: target, **thumbs}
    type(instance)._default_manager.filter(pk=instance.pk).update(**updates)
    for field_name, path in updates.items():
        setattr(instance, field_name, path)


def attach_scenario_artwork(organization: events_models.Organization, event: events_models.Event) -> None:
    """Give a scenario's organization its logo and its event its cover art."""
    _attach(
        organization,
        field="logo",
        asset=ASSETS_DIR / "logos" / f"{organization.slug}.jpg",
        storage_prefix=LOGO_STORAGE_PREFIX,
    )
    _attach(
        event,
        field="cover_art",
        asset=ASSETS_DIR / "covers" / f"{event.slug}.jpg",
        storage_prefix=COVER_STORAGE_PREFIX,
    )
