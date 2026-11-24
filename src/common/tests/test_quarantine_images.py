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
        file_path=logo,
    )
    return event, copy, [logo, thumbnail]


def _scan(event: Event, mock_clamd: MagicMock, flagged_bytes: bytes | None = None) -> None:
    """Scan the upload of ``event``; with ``flagged_bytes``, only those bytes are found malicious."""
    clamd = mock_clamd.return_value
    clamd.ping.return_value = True
    found = {"stream": ("FOUND", "Eicar-Test-Signature")}
    clamd.scan_stream.side_effect = lambda data: found if flagged_bytes is None or data == flagged_bytes else None
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


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_the_audited_file_is_scanned_after_the_original_got_a_new_image(
    mock_clamd: MagicMock,
    mock_notify: MagicMock,
    flagged: tuple[Event, Event, list[str]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The copy still names the uploaded file; the original's new image must not hide it from the scan."""
    event, copy, paths = flagged
    flagged_bytes = default_storage.open(paths[0], "rb").read()
    clean = default_storage.save(f"logos/{event.pk}/clean.png", ContentFile(_png() + b"clean"))
    Event.objects.filter(pk=event.pk).update(logo=clean, logo_thumbnail=None)

    with django_capture_on_commit_callbacks(execute=True):
        _scan(event, mock_clamd, flagged_bytes)

    assert not any(Event.objects.values_list(*FIELDS).get(pk=copy.pk))
    assert Event.objects.get(pk=event.pk).logo.name == clean
    assert not any(default_storage.exists(path) for path in paths)
    assert default_storage.exists(clean)


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_the_audited_file_is_scanned_after_the_original_was_deleted(
    mock_clamd: MagicMock,
    mock_notify: MagicMock,
    flagged: tuple[Event, Event, list[str]],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    event, copy, paths = flagged
    with django_capture_on_commit_callbacks(execute=True):
        Event.objects.get(pk=event.pk).delete()  # the copy still names the files, so they stay

    with django_capture_on_commit_callbacks(execute=True):
        _scan(event, mock_clamd)

    assert not any(Event.objects.values_list(*FIELDS).get(pk=copy.pk))
    assert not any(default_storage.exists(path) for path in paths)


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_an_audit_whose_file_is_gone_is_closed(
    mock_clamd: MagicMock, mock_notify: MagicMock, flagged: tuple[Event, Event, list[str]]
) -> None:
    """A replaced upload's file was deleted: the sweep must not queue its audit again and again."""
    event, _copy, paths = flagged
    Event.objects.filter(pk__in=[event.pk, _copy.pk]).update(logo=None, logo_thumbnail=None)
    default_storage.delete(paths[0])

    _scan(event, mock_clamd)

    audit = FileUploadAudit.objects.get(instance_pk=event.pk)
    assert audit.status == FileUploadAudit.FileUploadAuditStatus.CLEAN
    mock_notify.assert_not_called()


def test_the_sweep_queues_a_superseded_audit_that_has_its_own_path(
    flagged: tuple[Event, Event, list[str]],
) -> None:
    from common.tasks import RESCAN_GRACE, rescan_pending_uploads

    event, _copy, _paths = flagged
    old = timezone.now() - RESCAN_GRACE - timedelta(minutes=5)
    FileUploadAudit.objects.filter(instance_pk=event.pk).update(created_at=old)
    FileUploadAudit.objects.create(  # a newer upload to the same field
        app="events", model="Event", instance_pk=event.pk, field="logo", file_hash="b" * 64, uploader="x@example.com"
    )
    FileUploadAudit.objects.filter(file_hash="b" * 64).update(
        created_at=old + timedelta(minutes=1), status=FileUploadAudit.FileUploadAuditStatus.CLEAN
    )

    with mock.patch("common.tasks.scan_for_malware.delay") as scan:
        rescan_pending_uploads()

    scan.assert_called_once_with(app="events", model="Event", pk=str(event.pk), field="logo")


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_a_flagged_file_without_derived_images_is_deleted_after_commit(
    mock_clamd: MagicMock,
    mock_notify: MagicMock,
    settings: t.Any,
    tmp_path: t.Any,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    from events.models import AdditionalResource

    settings.MEDIA_ROOT = str(tmp_path)
    settings.FEATURE_MALWARE_SCAN = True
    owner = revel_user_factory()
    org = Organization.objects.create(name="Org", owner=owner)
    resource = AdditionalResource.objects.create(
        organization=org,
        resource_type=AdditionalResource.ResourceTypes.FILE,
        file=ContentFile(b"bad", name="a.txt"),
    )
    name = str(resource.file.name)
    FileUploadAudit.objects.create(
        app="events",
        model="AdditionalResource",
        instance_pk=resource.pk,
        field="file",
        file_hash=hashlib.sha256(b"bad").hexdigest(),
        uploader=owner.email,
        file_path=name,
    )
    clamd = mock_clamd.return_value
    clamd.ping.return_value = True
    clamd.scan_stream.return_value = {"stream": ("FOUND", "Eicar-Test-Signature")}

    with django_capture_on_commit_callbacks() as callbacks:
        scan_for_malware(app="events", model="AdditionalResource", pk=str(resource.pk), field="file")
    assert default_storage.exists(name)  # not before the commit

    for callback in callbacks:
        callback()

    assert not AdditionalResource.objects.get(pk=resource.pk).file
    assert not default_storage.exists(name)


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_a_reused_path_with_other_bytes_closes_the_audit_and_leaves_the_file(
    mock_clamd: MagicMock, mock_notify: MagicMock, flagged: tuple[Event, Event, list[str]]
) -> None:
    """A freed storage name was given to another upload: those bytes belong to that upload's own scan."""
    event, copy, paths = flagged
    Event.objects.filter(pk__in=[event.pk, copy.pk]).update(logo=None, logo_thumbnail=None)
    default_storage.delete(paths[0])
    reused = default_storage.save(paths[0], ContentFile(_png() + b"another upload"))
    assert reused == paths[0]

    _scan(event, mock_clamd)  # would flag any bytes

    assert FileUploadAudit.objects.get(instance_pk=event.pk).status == FileUploadAudit.FileUploadAuditStatus.CLEAN
    assert default_storage.exists(reused)
    mock_clamd.return_value.scan_stream.assert_not_called()
    mock_notify.assert_not_called()


@mock.patch("common.tasks.notify_malware_detected.delay")
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_a_missing_file_that_a_row_still_names_keeps_the_audit_pending(
    mock_clamd: MagicMock, mock_notify: MagicMock, flagged: tuple[Event, Event, list[str]]
) -> None:
    """A storage fault must not mark an upload clean: the scan fails and the sweep tries again."""
    event, _copy, paths = flagged
    default_storage.delete(paths[0])

    with pytest.raises(FileNotFoundError):
        _scan(event, mock_clamd)

    assert FileUploadAudit.objects.get(instance_pk=event.pk).status == FileUploadAudit.FileUploadAuditStatus.PENDING
