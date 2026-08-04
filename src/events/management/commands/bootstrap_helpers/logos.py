# src/events/management/commands/bootstrap_helpers/logos.py
"""Deterministic placeholder logos for seeded organizations (frontend #782).

The frontend renders an organization's ``logo_thumbnail`` (falling back to
``logo``) as a branding accent, or *nothing at all* when neither is set. No
seeded organization had a logo, so dev / e2e / demo environments always took the
empty branch.

Rather than committing an image per fixture organization, the mark is drawn at
seed time with Pillow: a filled geometric shape in one of the brand's poster
palette colours, carrying the organization's initials in whichever of ink/paper
reads better on it. Shape and colour are derived from a BLAKE2b digest of the
storage key, so a given organization always gets the same mark and a reseed
writes byte-identical files to the same path — nothing accumulates. No Revel
mark, wordmark, or external asset is involved.

Lives next to ``cover_art.py`` because it does the same job: attach a trusted,
locally-produced image plus its thumbnails straight onto the model, deliberately
bypassing the upload pipeline (no file audit, no malware scan, no Celery). It is
shared by ``bootstrap_events``, ``bootstrap_test_events`` and the ``seed``
command.
"""

import hashlib
import typing as t
from io import BytesIO

import structlog
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import models
from PIL import Image, ImageDraw, ImageFont

from common.thumbnails.config import THUMBNAIL_CONFIGS
from common.thumbnails.service import generate_and_save_thumbnails, get_thumbnail_path
from events.utils import _get_logo_initials  # reused: same initials the PDF/wallet fallbacks use

from .base import BootstrapState

logger = structlog.get_logger(__name__)

STORAGE_PREFIX = "logos/seed"

CANVAS = 512
MARGIN = 56
CORNER_RADIUS = 96

# ACIDHAIRS poster palette (Hearty Purple, Light Crimson, Lavender, Periwinkle, Amber, Ink).
PALETTE: tuple[str, ...] = ("#8C3CDD", "#E6332A", "#AB82DB", "#9AB2FF", "#F9B233", "#0D1E1C")
SHAPES: tuple[str, ...] = ("circle", "rounded_square", "triangle")

INK = "#0D1E1C"
PAPER = "#FFFFFF"

FONT_PATH = "NataSans-SemiBold.ttf"


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour."""
    srgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours."""
    luminances = sorted((_relative_luminance(first), _relative_luminance(second)))
    return (luminances[1] + 0.05) / (luminances[0] + 0.05)


def _readable_on(background: str) -> str:
    """Return whichever of paper/ink reads better on ``background``."""
    return PAPER if _contrast_ratio(PAPER, background) >= _contrast_ratio(INK, background) else INK


def generate_logo_png(key: str, name: str) -> bytes:
    """Draw a deterministic geometric placeholder logo.

    Args:
        key: Stable identifier (an organization slug) driving shape and colour.
        name: Display name the initials are taken from.

    Returns:
        PNG bytes of a ``CANVAS``-square logo.
    """
    digest = int.from_bytes(hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), "big")
    color = PALETTE[digest % len(PALETTE)]
    shape = SHAPES[digest // len(PALETTE) % len(SHAPES)]

    image = Image.new("RGB", (CANVAS, CANVAS), PAPER)
    draw = ImageDraw.Draw(image)
    box = (MARGIN, MARGIN, CANVAS - MARGIN, CANVAS - MARGIN)

    center = (CANVAS // 2, CANVAS // 2)
    font_size = 200
    if shape == "circle":
        draw.ellipse(box, fill=color)
    elif shape == "rounded_square":
        draw.rounded_rectangle(box, radius=CORNER_RADIUS, fill=color)
    else:
        # A triangle's optical centre sits low, and its width there is tighter.
        draw.polygon(
            [(CANVAS // 2, MARGIN), (CANVAS - MARGIN, CANVAS - MARGIN), (MARGIN, CANVAS - MARGIN)],
            fill=color,
        )
        center = (CANVAS // 2, int(CANVAS * 0.62))
        font_size = 140

    font = ImageFont.truetype(str(settings.BASE_DIR / "fonts" / FONT_PATH), font_size)
    draw.text(center, _get_logo_initials(name), font=font, fill=_readable_on(color), anchor="mm")

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def attach_logo(instance: models.Model, *, key: str, name: str) -> None:
    """Link a generated placeholder logo (and its thumbnails) onto ``instance``.

    The image and its thumbnails live at deterministic storage paths; when they
    already exist they are reused and only the model fields are updated (via
    ``queryset.update()``, so no model ``save()`` side effects run).
    """
    if getattr(instance, "logo"):
        return  # already set — keep reseeds idempotent

    target = f"{STORAGE_PREFIX}/{key}.png"
    if not default_storage.exists(target):
        default_storage.save(target, ContentFile(generate_logo_png(key, name)))

    config_key = (instance._meta.app_label, t.cast(str, instance._meta.model_name), "logo")
    config = THUMBNAIL_CONFIGS[config_key]
    thumbs = {spec.field_name: get_thumbnail_path(target, spec.field_name) for spec in config.specs}
    if not all(default_storage.exists(path) for path in thumbs.values()):
        result = generate_and_save_thumbnails(target, config)
        thumbs = result.thumbnails
        if result.has_failures:
            logger.warning("Placeholder logo thumbnail generation failed", key=key, failures=result.failures)

    updates: dict[str, str] = {"logo": target, **thumbs}
    type(instance)._default_manager.filter(pk=instance.pk).update(**updates)
    for field_name, path in updates.items():
        setattr(instance, field_name, path)


def attach_org_logos(state: BootstrapState) -> None:
    """Give every bootstrap organization a deterministic placeholder logo."""
    for org in state.orgs.values():
        attach_logo(org, key=org.slug, name=org.name)
    logger.info("Attached placeholder logos", orgs=len(state.orgs))
