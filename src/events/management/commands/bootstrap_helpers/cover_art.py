# src/events/management/commands/bootstrap_helpers/cover_art.py
"""Attach bundled cover art to bootstrap organizations and events.

The images in ``assets/covers/`` are trusted repo assets (brand-palette art
generated for the demo dataset, plus a few free-license Lorem Picsum photos),
already stripped of metadata. They deliberately bypass the upload pipeline:
no file audit, no malware scan, no async Celery tasks.

Files are cached in storage under deterministic ``cover-art/bootstrap/``
paths: if a file (or its thumbnails) already exists from a previous
bootstrap, it is only *linked* on the model via a queryset ``update()`` —
nothing is re-uploaded, re-scanned, or re-encoded on subsequent runs.
"""

import typing as t
from pathlib import Path

import structlog
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.db import models

from common.thumbnails.config import THUMBNAIL_CONFIGS
from common.thumbnails.service import generate_and_save_thumbnails, get_thumbnail_path

from .base import BootstrapState

logger = structlog.get_logger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets" / "covers"
STORAGE_PREFIX = "cover-art/bootstrap"

# state.events key -> asset filename
EVENT_COVERS = {
    "summer_festival": "summer_festival.jpg",
    "wine_tasting": "wine_tasting.jpg",
    "tech_workshop": "tech_workshop.jpg",
    "spring_potluck": "spring_potluck.jpg",
    "tech_conference": "tech_conference.jpg",
    "wellness_retreat": "wellness_retreat.jpg",
    "networking_event": "networking_event.jpg",
    "art_opening": "art_opening.jpg",
    "tech_talk_may": "tech_talk_may.jpg",
    "sold_out_workshop": "sold_out_workshop.jpg",
    "seated_concert": "seated_concert.jpg",
}

# state.orgs key -> asset filename (reuses event art)
ORG_COVERS = {
    "alpha": "summer_festival.jpg",
    "beta": "tech_talk_may.jpg",
}


def _attach_cover(instance: models.Model, asset: str) -> None:
    """Link a bundled asset as ``instance.cover_art``, uploading it only once.

    The file and its thumbnails live at deterministic storage paths; when they
    already exist they are reused and only the model fields are updated
    (via ``queryset.update()``, so no model ``save()`` side effects run).
    """
    if getattr(instance, "cover_art"):
        return  # already set — keep bootstrap idempotent

    target = f"{STORAGE_PREFIX}/{asset}"
    if not default_storage.exists(target):
        with (ASSETS_DIR / asset).open("rb") as fh:
            default_storage.save(target, File(fh))

    config_key = (instance._meta.app_label, t.cast(str, instance._meta.model_name), "cover_art")
    config = THUMBNAIL_CONFIGS[config_key]
    thumbs = {spec.field_name: get_thumbnail_path(target, spec.field_name) for spec in config.specs}
    if not all(default_storage.exists(path) for path in thumbs.values()):
        result = generate_and_save_thumbnails(target, config)
        thumbs = result.thumbnails
        if result.has_failures:
            logger.warning("Cover art thumbnail generation failed", asset=asset, failures=result.failures)

    updates: dict[str, str] = {"cover_art": target, **thumbs}
    type(instance)._default_manager.filter(pk=instance.pk).update(**updates)
    for field_name, path in updates.items():
        setattr(instance, field_name, path)


def attach_cover_art(state: BootstrapState) -> None:
    """Attach bundled cover art to organizations and events."""
    for org_key, asset in ORG_COVERS.items():
        if org := state.orgs.get(org_key):
            _attach_cover(org, asset)
    for event_key, asset in EVENT_COVERS.items():
        if event := state.events.get(event_key):
            _attach_cover(event, asset)
    logger.info("Attached cover art", orgs=len(ORG_COVERS), events=len(EVENT_COVERS))
