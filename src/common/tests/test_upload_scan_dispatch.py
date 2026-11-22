"""Malware-scan dispatch survives broker failures, and the sweep queues pending scans again (#1446)."""

import hashlib
import typing as t
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from common import tasks
from common.models import FileUploadAudit
from common.service import upload_service
from common.thumbnails import tasks as thumbnail_tasks
from questionnaires.models import QuestionnaireFile
from questionnaires.service import file_service

pytestmark = pytest.mark.django_db

Status = FileUploadAudit.FileUploadAuditStatus


def _questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    qf = QuestionnaireFile(uploader=user, original_filename="a.txt", file_hash=uuid.uuid4().hex, mime_type="text/plain")
    qf.file.save("a.txt", ContentFile(b"hello"), save=False)
    qf.file_size = 5
    qf.save()
    return qf


def _audit(
    qf: QuestionnaireFile, *, age: timedelta, status: str = Status.PENDING, file_hash: str = "h"
) -> FileUploadAudit:
    audit = FileUploadAudit.objects.create(
        app="questionnaires",
        model="questionnairefile",
        instance_pk=qf.pk,
        field="file",
        file_hash=file_hash,
        uploader="u@example.com",
        status=status,
    )
    FileUploadAudit.objects.filter(pk=audit.pk).update(created_at=timezone.now() - age)
    return audit


# --- dispatch ---


def test_broker_failure_does_not_fail_upload(settings: t.Any, django_capture_on_commit_callbacks: t.Any) -> None:
    """A failing ``delay`` after commit is logged; the audit stays pending and later callbacks still run."""
    settings.FEATURE_MALWARE_SCAN = True
    later = mock.Mock()
    with mock.patch.object(tasks.scan_for_malware, "delay", side_effect=ConnectionError("broker down")) as delay:
        with django_capture_on_commit_callbacks(execute=True):
            upload_service.create_file_audit_and_scan(
                app="events",
                model="organization",
                instance_pk=uuid.uuid4(),
                field="logo",
                file_hash="broker",
                uploader_email="u@example.com",
            )
            transaction.on_commit(later)

    delay.assert_called_once()
    later.assert_called_once()
    assert FileUploadAudit.objects.get(file_hash="broker").status == Status.PENDING


def test_dispatch_queues_scan_with_upload_arguments(settings: t.Any, django_capture_on_commit_callbacks: t.Any) -> None:
    settings.FEATURE_MALWARE_SCAN = True
    pk = uuid.uuid4()
    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            upload_service.create_file_audit_and_scan(
                app="events", model="organization", instance_pk=pk, field="logo", file_hash="x", uploader_email="u@x.io"
            )
    delay.assert_called_once_with(app="events", model="organization", pk=str(pk), field="logo")


def _broker_down() -> t.Any:
    """Patch every upload dispatch to raise, as when the broker is not reachable."""
    error = ConnectionError("broker down")
    return (
        mock.patch.object(tasks.scan_for_malware, "delay", side_effect=error),
        mock.patch.object(thumbnail_tasks.generate_thumbnails_task, "delay", side_effect=error),
        mock.patch.object(thumbnail_tasks.delete_orphaned_thumbnails_task, "delay", side_effect=error),
    )


def test_image_replace_with_broker_down_keeps_the_upload(
    settings: t.Any, django_capture_on_commit_callbacks: t.Any, user: RevelUser, png_bytes: bytes
) -> None:
    """``safe_save_uploaded_file`` with thumbnails: no dispatch failure reaches the caller."""
    settings.FEATURE_MALWARE_SCAN = True
    upload_service.safe_save_uploaded_file(
        instance=user, field="profile_picture", file=SimpleUploadedFile("a.png", png_bytes), uploader=user
    )
    # An old thumbnail makes the replace queue its deletion first, before the scan callback.
    RevelUser.objects.filter(pk=user.pk).update(profile_picture_thumbnail="thumbs/old.png")
    user.refresh_from_db()
    scan, thumbs, orphans = _broker_down()
    with scan as scan_delay, thumbs as thumbs_delay, orphans as orphans_delay:
        with django_capture_on_commit_callbacks(execute=True):
            upload_service.safe_save_uploaded_file(
                instance=user, field="profile_picture", file=SimpleUploadedFile("b.png", png_bytes), uploader=user
            )

    orphans_delay.assert_called_once()
    scan_delay.assert_called_once()
    thumbs_delay.assert_called_once()
    user.refresh_from_db()
    assert user.profile_picture
    assert FileUploadAudit.objects.filter(instance_pk=user.pk, status=Status.PENDING).exists()


def test_questionnaire_image_with_broker_down_keeps_the_upload(
    settings: t.Any, django_capture_on_commit_callbacks: t.Any, user: RevelUser, png_bytes: bytes
) -> None:
    settings.FEATURE_MALWARE_SCAN = True
    scan, thumbs, _ = _broker_down()
    # file_service imports the same task object, so this patch covers it.
    with scan as scan_delay, thumbs as thumbs_delay:
        with django_capture_on_commit_callbacks(execute=True):
            qf = file_service.upload_questionnaire_file(user, SimpleUploadedFile("a.png", png_bytes))  # type: ignore[arg-type]

    scan_delay.assert_called_once()
    thumbs_delay.assert_called_once()
    assert QuestionnaireFile.objects.filter(pk=qf.pk).exists()


# --- sweep ---


@pytest.fixture
def scan_on(settings: t.Any) -> None:
    settings.FEATURE_MALWARE_SCAN = True


@pytest.mark.usefixtures("scan_on")
def test_sweep_queues_stale_pending_audit(user: RevelUser) -> None:
    qf = _questionnaire_file(user)
    _audit(qf, age=timedelta(hours=1))

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 1

    delay.assert_called_once_with(app="questionnaires", model="questionnairefile", pk=str(qf.pk), field="file")


@pytest.mark.usefixtures("scan_on")
def test_sweep_queues_one_scan_per_field(user: RevelUser) -> None:
    """Two pending audits of one field with the same ``created_at`` give one scan.

    Neither audit is strictly newer than the other, so only ``distinct()`` collapses them.
    """
    qf = _questionnaire_file(user)
    first = _audit(qf, age=timedelta(hours=1))
    second = _audit(qf, age=timedelta(hours=1))
    first.refresh_from_db()
    FileUploadAudit.objects.filter(pk=second.pk).update(created_at=first.created_at)

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 1
    delay.assert_called_once()


@pytest.mark.usefixtures("scan_on")
@pytest.mark.parametrize(
    "age",
    [
        pytest.param(timedelta(minutes=5), id="inside-grace"),
        pytest.param(timedelta(days=8), id="beyond-lookback"),
    ],
)
def test_sweep_skips_audits_outside_the_window(user: RevelUser, age: timedelta) -> None:
    _audit(_questionnaire_file(user), age=age)

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
@pytest.mark.parametrize("status", [Status.CLEAN, Status.MALICIOUS])
def test_sweep_skips_scanned_audits(user: RevelUser, status: str) -> None:
    _audit(_questionnaire_file(user), age=timedelta(hours=1), status=status)

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
@pytest.mark.parametrize("newer_status", [Status.CLEAN, Status.MALICIOUS])
def test_sweep_skips_audit_superseded_by_a_newer_upload(user: RevelUser, newer_status: str) -> None:
    """The file was replaced and the new one was scanned: the old pending audit is not re-queued."""
    qf = _questionnaire_file(user)
    _audit(qf, age=timedelta(hours=2), file_hash="old")
    _audit(qf, age=timedelta(hours=1), file_hash="new", status=newer_status)

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
def test_sweep_skips_deleted_instance(user: RevelUser) -> None:
    qf = _questionnaire_file(user)
    _audit(qf, age=timedelta(hours=1))
    QuestionnaireFile.objects.filter(pk=qf.pk).delete()

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
def test_sweep_skips_instance_without_file(user: RevelUser) -> None:
    qf = _questionnaire_file(user)
    _audit(qf, age=timedelta(hours=1))
    QuestionnaireFile.objects.filter(pk=qf.pk).update(file="")

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
@pytest.mark.parametrize("column", ["model", "field"])
def test_sweep_skips_unknown_model_or_field_and_continues(user: RevelUser, column: str) -> None:
    """A stale row (renamed model or field) is skipped; the other candidates are still queued."""
    stale = _audit(_questionnaire_file(user), age=timedelta(hours=1))
    FileUploadAudit.objects.filter(pk=stale.pk).update(**{column: "gone"})
    good = _questionnaire_file(user)
    _audit(good, age=timedelta(hours=1))

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 1
    delay.assert_called_once_with(app="questionnaires", model="questionnairefile", pk=str(good.pk), field="file")


def test_sweep_does_nothing_when_scan_disabled(settings: t.Any, user: RevelUser) -> None:
    settings.FEATURE_MALWARE_SCAN = False
    _audit(_questionnaire_file(user), age=timedelta(hours=1))

    with mock.patch.object(tasks.scan_for_malware, "delay") as delay:
        assert tasks.rescan_pending_uploads() == 0
    delay.assert_not_called()


@pytest.mark.usefixtures("scan_on")
def test_sweep_end_to_end_marks_audit_clean(user: RevelUser) -> None:
    """With the eager worker and the mocked ClamAV, the re-queued scan resolves the audit."""
    qf = _questionnaire_file(user)
    qf.file.open()

    file_hash = hashlib.sha256(qf.file.read()).hexdigest()
    qf.file.close()
    audit = _audit(qf, age=timedelta(hours=1), file_hash=file_hash)

    assert tasks.rescan_pending_uploads() == 1

    audit.refresh_from_db()
    assert audit.status == Status.CLEAN
