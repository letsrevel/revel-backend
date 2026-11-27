"""The daily sweep of image files that no row names (#1548)."""

import os
import time
import typing as t
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from accounts.models import RevelUser
from common.thumbnails import sweep
from events.models import Event, Organization
from oauth.models import OAuthApplication

pytestmark = pytest.mark.django_db

EIGHT_DAYS = 8 * 24 * 3600


def _store(path: str, *, old: bool = True) -> str:
    name = default_storage.save(path, ContentFile(b"image"))
    if old:
        stamp = time.time() - EIGHT_DAYS
        os.utime(default_storage.path(name), (stamp, stamp))
    return name


@pytest.fixture
def media(settings: t.Any, tmp_path: t.Any, organization: Organization, user: RevelUser) -> dict[str, str]:
    """Old unreferenced files in each swept folder, and files that the sweep must keep."""
    settings.MEDIA_ROOT = str(tmp_path)
    files = {
        "logo": _store("logos/orphan.png"),
        "cover": _store("cover-art/orphan.png"),
        "oauth": _store("oauth-logos/orphan.png"),
        "profile": _store(f"protected/profile-pictures/{user.id}/orphan.jpg"),
        "referenced": _store("logos/in-use.png"),
        "referenced_derived": _store("logos/in-use_png_thumb.jpg"),
        "fresh": _store("logos/fresh.png", old=False),
        "seed": _store("logos/seed/demo.png"),
    }
    Organization.objects.filter(pk=organization.pk).update(
        logo=files["referenced"], logo_thumbnail=files["referenced_derived"]
    )
    return files


SWEPT = ("logo", "cover", "oauth", "profile")
KEPT = ("referenced", "referenced_derived", "fresh", "seed")


def test_a_dry_run_counts_and_deletes_nothing(media: dict[str, str]) -> None:
    counts = sweep.sweep_unreferenced_image_files()

    assert counts.files_unreferenced == len(SWEPT)
    assert counts.files_deleted == 0
    assert all(default_storage.exists(path) for path in media.values())


def test_a_delete_run_removes_only_old_unreferenced_files_in_the_swept_folders(media: dict[str, str]) -> None:
    counts = sweep.sweep_unreferenced_image_files(dry_run=False)

    assert counts.files_deleted == len(SWEPT)
    assert not any(default_storage.exists(media[key]) for key in SWEPT)
    assert all(default_storage.exists(media[key]) for key in KEPT)


def test_the_inverse_check_counts_named_paths_that_storage_lacks(
    media: dict[str, str], organization: Organization
) -> None:
    Organization.objects.filter(pk=organization.pk).update(cover_art="cover-art/gone.png")

    assert sweep.sweep_unreferenced_image_files().named_paths_missing == 1


def test_a_reference_written_after_the_count_keeps_the_file(media: dict[str, str], organization: Organization) -> None:
    """The delete checks the references again under the path locks."""
    Organization.objects.filter(pk=organization.pk).update(cover_art=media["cover"])
    counts = sweep.SweepCounts()

    sweep._delete_unreferenced([media["cover"], media["logo"]], counts)

    assert default_storage.exists(media["cover"])
    assert not default_storage.exists(media["logo"])
    assert counts.files_deleted == 1


def test_one_run_deletes_at_most_the_batch(media: dict[str, str]) -> None:
    with patch.object(sweep, "DELETE_BATCH", 2):
        counts = sweep.sweep_unreferenced_image_files(dry_run=False)

    assert counts.files_deleted == 2
    assert sum(not default_storage.exists(media[key]) for key in SWEPT) == 2


def test_the_task_is_a_dry_run_by_default(media: dict[str, str]) -> None:
    from common.thumbnails.tasks import sweep_unreferenced_image_files_task

    result = sweep_unreferenced_image_files_task.delay().get()

    assert result["files_unreferenced"] == len(SWEPT)
    assert result["files_deleted"] == 0


def test_files_that_users_events_and_apps_name_are_kept(
    media: dict[str, str], user: RevelUser, organization: Organization
) -> None:
    """Every model with image fields counts, the protected profile-picture paths included."""
    event = Event.objects.create(organization=organization, name="E", slug="e-1548", start=timezone.now())
    oauth_app = OAuthApplication.objects.create(
        user=user,
        name="App",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        redirect_uris="https://app.example/cb",
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    picture = _store(f"protected/profile-pictures/{user.id}/mine.jpg")
    picture_thumb = _store(f"protected/profile-pictures/{user.id}/mine_jpg_thumb.jpg")
    event_cover = _store("cover-art/event.png")
    app_logo = _store("oauth-logos/app.png")
    RevelUser.objects.filter(pk=user.pk).update(profile_picture=picture, profile_picture_thumbnail=picture_thumb)
    Event.objects.filter(pk=event.pk).update(cover_art=event_cover)
    OAuthApplication.objects.filter(pk=oauth_app.pk).update(logo=app_logo)

    sweep.sweep_unreferenced_image_files(dry_run=False)

    assert all(default_storage.exists(path) for path in (picture, picture_thumb, event_cover, app_logo))
    assert not default_storage.exists(media["profile"])


def test_the_inverse_check_ignores_paths_outside_the_swept_folders(
    media: dict[str, str], organization: Organization
) -> None:
    Organization.objects.filter(pk=organization.pk).update(cover_art="cover-art/seed/gone.png")

    assert sweep.sweep_unreferenced_image_files().named_paths_missing == 0
