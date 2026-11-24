"""A flagged image goes from every row that names it, with its derived images, after commit."""

import hashlib
import io
import typing as t
from datetime import timedelta
from unittest import mock
from unittest.mock import MagicMock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from PIL import Image

from common.models import FileUploadAudit
from common.tasks import scan_for_malware
from conftest import RevelUserFactory
from events.models import Event, Organization
from events.service.duplication import duplicate_event

pytestmark = pytest.mark.django_db

FIELDS = ("logo", "logo_thumbnail")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def flagged(settings: t.Any, tmp_path: t.Any, revel_user_factory: RevelUserFactory) -> tuple[Event, Event, list[str]]:
    """An event with a logo and its thumbnail, a duplicate that shares them, and a pending upload audit."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.FEATURE_MALWARE_SCAN = True
    payload = _png()
    owner = revel_user_factory()
    org = Organization.objects.create(name="Org", owner=owner)
    event = Event.objects.create(organization=org, name="Event", start=timezone.now())
    logo = default_storage.save(f"logos/{event.pk}/a.png", ContentFile(payload))
    thumbnail = default_storage.save(f"logos/{event.pk}/a_png_thumbnail.jpg", ContentFile(b"thumb"))
    Event.objects.filter(pk=event.pk).update(logo=logo, logo_thumbnail=thumbnail)
    event.refresh_from_db()
    copy = duplicate_event(event, "Copy", event.start + timedelta(days=7))
    FileUploadAudit.objects.create(
        app="events",
        model="Event",
        instance_pk=event.pk,
        field="logo",
        file_hash=hashlib.sha256(payload).hexdigest(),
        uploader=owner.email,
    )
    return event, copy, [logo, thumbnail]


def _scan(event: Event, mock_clamd: MagicMock) -> None:
    clamd = mock_clamd.return_value
    clamd.ping.return_value = True
    clamd.scan_stream.return_value = {"stream": ("FOUND", "Eicar-Test-Signature")}
    scan_for_malware(app="events", model="Event", pk=str(event.pk), field="logo")


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_a_flagged_image_is_removed_from_every_row_that_names_it(
    mock_clamd: MagicMock,
    mock_notify: MagicMock,
    flagged: tuple[Event, Event, list[str]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    event, copy, paths = flagged

    with django_capture_on_commit_callbacks() as callbacks:
        _scan(event, mock_clamd)
    for row in (event, copy):
        assert not any(Event.objects.values_list(*FIELDS).get(pk=row.pk))
    assert all(default_storage.exists(path) for path in paths)  # not before the commit

    for callback in callbacks:
        callback()

    assert not any(default_storage.exists(path) for path in paths)
    mock_notify.assert_called_once()


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_a_row_that_got_a_new_image_meanwhile_keeps_it(
    mock_clamd: MagicMock,
    mock_notify: MagicMock,
    flagged: tuple[Event, Event, list[str]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    event, copy, _paths = flagged
    newer = default_storage.save(f"logos/{copy.pk}/b.png", ContentFile(_png()))
    Event.objects.filter(pk=copy.pk).update(logo=newer, logo_thumbnail=None)

    with django_capture_on_commit_callbacks(execute=True):
        _scan(event, mock_clamd)

    assert Event.objects.get(pk=copy.pk).logo.name == newer
    assert default_storage.exists(newer)
