"""New image uploads get random file names, not the client's file name (#1547)."""

import io
import re
import typing as t

import pytest
from django.core.files.base import ContentFile
from PIL import Image

from accounts.models import RevelUser, profile_picture_upload_path
from common.fields import random_image_name
from events.models import Organization
from events.models.mixins import cover_art_upload_to, logo_upload_to
from oauth.models import oauth_logo_upload_to

RANDOM = r"[0-9a-f]{32}"


@pytest.mark.parametrize(
    ("client_name", "extension"),
    [("Jane Doe.PNG", ".png"), ("photo.jpeg", ".jpeg"), ("IMG_0001.HEIC", ".heic"), ("x.webp", ".webp")],
)
def test_the_name_is_random_with_the_lower_cased_extension(client_name: str, extension: str) -> None:
    assert re.fullmatch(RANDOM + re.escape(extension), random_image_name(client_name))


@pytest.mark.parametrize("client_name", ["no-extension", "report.pdf", "archive.tar.gz", "../../etc/passwd"])
def test_a_suffix_that_is_not_an_image_extension_is_dropped(client_name: str) -> None:
    """The extension validator refuses such a file before the save; the name never takes the client's suffix."""
    assert re.fullmatch(RANDOM, random_image_name(client_name))


def test_two_uploads_with_the_same_client_name_get_different_names() -> None:
    assert random_image_name("logo.png") != random_image_name("logo.png")


@pytest.mark.parametrize(
    ("upload_to", "folder"),
    [(logo_upload_to, "logos"), (cover_art_upload_to, "cover-art"), (oauth_logo_upload_to, "oauth-logos")],
)
def test_the_folders_stay_the_same(upload_to: t.Callable[[t.Any, str], str], folder: str) -> None:
    assert re.fullmatch(rf"{folder}/{RANDOM}\.png", upload_to(None, "Jane Doe.png"))


@pytest.mark.django_db
def test_a_profile_picture_stays_in_the_user_folder(user: RevelUser) -> None:
    assert re.fullmatch(rf"profile-pictures/{user.id}/{RANDOM}\.jpg", profile_picture_upload_path(user, "Jane.jpg"))


@pytest.mark.django_db
def test_a_stored_logo_does_not_carry_the_client_name(
    organization: Organization, settings: t.Any, tmp_path: t.Any
) -> None:
    settings.MEDIA_ROOT = str(tmp_path)
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buffer, format="PNG")

    organization.logo.save("Jane Doe passport.png", ContentFile(buffer.getvalue()), save=True)

    organization.refresh_from_db()
    assert re.fullmatch(rf"logos/{RANDOM}\.png", str(organization.logo.name))
