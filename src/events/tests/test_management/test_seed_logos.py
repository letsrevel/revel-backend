"""Tests for the deterministic placeholder logos seeded organizations get (frontend #782).

The frontend renders an organization's ``logo_thumbnail`` (falling back to
``logo``) or nothing at all, so every seeded organization needs both fields
populated for the branding accents to show up in dev / e2e / demo.
"""

import io
import typing as t
from uuid import uuid4

import pytest
from django.core.files.storage import default_storage
from PIL import Image

from accounts.models import RevelUser
from events.management.commands.bootstrap_helpers.base import BootstrapState
from events.management.commands.bootstrap_helpers.logos import (
    CANVAS,
    INK,
    PALETTE,
    PAPER,
    STORAGE_PREFIX,
    _contrast_ratio,
    _readable_on,
    attach_logo,
    attach_org_logos,
    generate_logo_png,
)
from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.organizations import OrganizationSeeder
from events.management.commands.seeder.state import SeederState
from events.models import Organization
from events.schema import MinimalOrganizationSchema

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_media(settings: t.Any, tmp_path: t.Any) -> None:
    """Keep generated logos out of the repo's media directory."""
    settings.MEDIA_ROOT = tmp_path


def _org(slug: str = "seeded-org", name: str = "Seeded Org") -> Organization:
    """An organization standing in for one the seeders created."""
    handle = f"{slug}.owner.{uuid4().hex[:8]}"
    owner = RevelUser.objects.create_user(username=handle, email=f"{handle}@example.com", password="x")
    return Organization.objects.create(name=name, slug=slug, owner=owner)


def _stored_logos() -> list[str]:
    """The generated logo files (thumbnails land in the same directory)."""
    _, files = default_storage.listdir(STORAGE_PREFIX)
    return sorted(name for name in files if name.endswith(".png"))


def test_generated_logo_is_a_valid_square_png() -> None:
    """The mark is a Pillow-readable PNG at the documented canvas size."""
    with Image.open(io.BytesIO(generate_logo_png("seeded-org", "Seeded Org"))) as image:
        assert image.format == "PNG"
        assert image.size == (CANVAS, CANVAS)


def test_generated_logo_is_deterministic_per_key() -> None:
    """A reseed must produce byte-identical files, and distinct orgs distinct marks."""
    assert generate_logo_png("seeded-org", "Seeded Org") == generate_logo_png("seeded-org", "Seeded Org")
    assert generate_logo_png("seeded-org", "Seeded Org") != generate_logo_png("other-org", "Other Org")


def test_generated_logo_uses_only_palette_and_neutral_colors() -> None:
    """No hue outside the poster palette (plus ink/paper) may leak into the mark.

    Anti-aliasing blends the shape into the field, so this checks the *dominant*
    colors rather than every pixel.
    """
    allowed = {tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in (*PALETTE, INK, PAPER)}
    with Image.open(io.BytesIO(generate_logo_png("seeded-org", "Seeded Org"))) as image:
        counted = image.convert("RGB").getcolors(maxcolors=1 << 20)
    assert counted is not None, "the mark has more distinct colors than getcolors() would count"
    dominant = {color for count, color in counted if count > 1000}
    assert dominant, "expected at least one dominant color"
    assert dominant <= allowed


def test_initials_contrast_against_every_palette_color() -> None:
    """The initials must stay legible on whichever palette color is drawn.

    3:1 is WCAG's large-text / non-text threshold, and these initials are set at
    140-200px. (Light Crimson tops out at 4.31:1 against paper, the palette's
    worst pairing.)
    """
    for color in PALETTE:
        assert _contrast_ratio(_readable_on(color), color) >= 3.0


def test_attach_logo_populates_logo_and_thumbnail() -> None:
    """Both fields the frontend reads are set, and the files really exist."""
    org = _org()

    attach_logo(org, key=org.slug, name=org.name)

    org.refresh_from_db()
    assert org.logo.name == f"{STORAGE_PREFIX}/{org.slug}.png"
    assert org.logo_thumbnail.name
    assert default_storage.exists(org.logo.name)
    assert default_storage.exists(org.logo_thumbnail.name)


def test_attached_logo_reaches_the_field_the_frontend_reads() -> None:
    """``logo_thumbnail_url`` is what the frontend's LogoChip actually consumes."""
    org = _org()

    attach_logo(org, key=org.slug, name=org.name)

    serialized = MinimalOrganizationSchema.from_orm(org)
    assert serialized.logo_thumbnail_url is not None


def test_attach_logo_is_idempotent() -> None:
    """Reseeding must not error or pile up duplicate files."""
    org = _org()
    attach_logo(org, key=org.slug, name=org.name)
    original_logo, original_thumb = org.logo.name, org.logo_thumbnail.name

    attach_logo(org, key=org.slug, name=org.name)

    org.refresh_from_db()
    assert (org.logo.name, org.logo_thumbnail.name) == (original_logo, original_thumb)
    assert _stored_logos() == [f"{org.slug}.png"]


def test_attach_logo_reuses_an_existing_file_for_a_fresh_org() -> None:
    """A wiped-and-reseeded org relinks the cached file instead of writing a new one."""
    attach_logo(_org(), key="seeded-org", name="Seeded Org")
    Organization.objects.all().delete()

    attach_logo(_org(), key="seeded-org", name="Seeded Org")

    assert _stored_logos() == ["seeded-org.png"]


def test_attach_org_logos_covers_every_bootstrap_org() -> None:
    """``bootstrap_events`` orgs — Org Alpha included — all come out with a logo."""
    state = BootstrapState()
    state.orgs["alpha"] = _org(slug="revel-events-collective", name="Revel Events Collective")
    state.orgs["beta"] = _org(slug="tech-innovators-network", name="Tech Innovators Network")

    attach_org_logos(state)

    for org in Organization.objects.all():
        assert org.logo, f"{org.slug} has no logo"
        assert org.logo_thumbnail, f"{org.slug} has no logo thumbnail"


def test_organization_seeder_gives_every_org_a_logo() -> None:
    """The bulk ``seed`` command's organizations get logos too."""
    state = SeederState()
    state.organizations = [_org(slug=f"org-{i}", name=f"Seeded Org {i}") for i in range(3)]
    seeder = OrganizationSeeder(SeederConfig(seed=999), state, io.StringIO())

    seeder._attach_logos()

    for org in Organization.objects.all():
        assert org.logo, f"{org.slug} has no logo"
        assert org.logo_thumbnail, f"{org.slug} has no logo thumbnail"
