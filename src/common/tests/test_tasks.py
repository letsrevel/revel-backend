import hashlib
import typing as t
from unittest import mock
from unittest.mock import MagicMock

import pytest
from django.core.files.base import ContentFile

from common.models import FileUploadAudit, QuarantinedFile
from common.tasks import notify_malware_detected, scan_for_malware
from common.utils import safe_save_uploaded_file
from conftest import RevelUserFactory
from events.models import AdditionalResource, Organization

pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_scan_for_malware(mock_clamd: MagicMock, revel_user_factory: RevelUserFactory) -> None:
    """Uses ``transaction=True`` because ``safe_save_uploaded_file`` schedules the
    malware scan via ``transaction.on_commit``; in default pytest-django mode the
    rolled-back transaction suppresses the callback and the scan never runs.
    """
    eicar_payload = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    uploader = revel_user_factory()
    org = Organization.objects.create(name="Test Organization", owner=uploader)
    additional_resource = AdditionalResource.objects.create(
        organization=org, resource_type=AdditionalResource.ResourceTypes.FILE
    )

    mock_clamd_instance = mock_clamd.return_value
    mock_clamd_instance.ping.return_value = True
    mock_clamd_instance.scan_stream.return_value = {"stream": ("FOUND", "Eicar-Test-Signature")}

    instance = safe_save_uploaded_file(
        instance=additional_resource, field="file", file=ContentFile(eicar_payload, name="eicar.txt"), uploader=uploader
    )

    # With CELERY_TASK_ALWAYS_EAGER=True, the scan task runs synchronously
    audit = FileUploadAudit.objects.first()
    assert audit is not None
    assert audit.instance_pk == instance.pk
    assert audit.status == FileUploadAudit.FileUploadAuditStatus.MALICIOUS

    instance.refresh_from_db()
    assert not instance.file

    quarantined_file = QuarantinedFile.objects.first()
    assert quarantined_file is not None
    assert quarantined_file.file.read() == eicar_payload
    assert quarantined_file.findings


@mock.patch("common.tasks.send_email.delay")
def test_notify_malware_detected(mock_send_email: MagicMock, revel_user_factory: RevelUserFactory) -> None:
    """Test that malware detection notifications are sent correctly."""

    # Create test data
    uploader = revel_user_factory()
    owner = revel_user_factory()
    org = Organization.objects.create(name="Test Organization", owner=owner)

    revel_user_factory(is_staff=True)

    file_content = b"test content"
    file_hash = hashlib.sha256(file_content).hexdigest()

    # Create file upload audit
    audit = FileUploadAudit.objects.create(
        app="events",
        model="Organization",
        instance_pk=org.pk,
        field="logo",
        file_hash=file_hash,
        uploader=uploader.email,
        status=FileUploadAudit.FileUploadAuditStatus.MALICIOUS,
    )

    # Create quarantined file
    QuarantinedFile.objects.create(
        audit=audit,
        file=ContentFile(file_content, name="test.jpg"),
        findings={"stream": ("FOUND", "Test-Virus")},
    )

    # Call the notification task
    notify_malware_detected(
        app="events",
        model="Organization",
        pk=str(org.pk),
        field="logo",
        file_hash=file_hash,
        findings={"stream": ("FOUND", "Test-Virus")},
    )

    # Verify that emails were sent
    assert mock_send_email.call_count == 3  # uploader + org owner + staff/superuser emails


@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_scan_for_malware_rescan_skips_already_quarantined_audit(
    mock_clamd: MagicMock, revel_user_factory: RevelUserFactory, settings: t.Any
) -> None:
    """Identical bytes re-uploaded to the same instance: only the audit without a QuarantinedFile
    is quarantined. The already-linked audit must not be handed to bulk_create at all — its
    ``ContentFile`` would be written to storage before the conflicting INSERT is ignored."""
    settings.FEATURE_MALWARE_SCAN = True
    findings = {"stream": ("FOUND", "Eicar-Test-Signature")}
    clamd = mock_clamd.return_value
    clamd.ping.return_value = True
    clamd.scan_stream.return_value = findings

    uploader = revel_user_factory()
    org = Organization.objects.create(name="Test Organization", owner=uploader)
    payload = b"malicious bytes"
    resource = AdditionalResource.objects.create(
        organization=org,
        resource_type=AdditionalResource.ResourceTypes.FILE,
        file=ContentFile(payload, name="evil.txt"),
    )
    file_hash = hashlib.sha256(payload).hexdigest()
    audit_kwargs: dict[str, t.Any] = {
        "app": "events",
        "model": "AdditionalResource",
        "instance_pk": resource.pk,
        "field": "file",
        "uploader": uploader.email,
    }
    first = FileUploadAudit.objects.create(
        file_hash=file_hash, status=FileUploadAudit.FileUploadAuditStatus.MALICIOUS, **audit_kwargs
    )
    QuarantinedFile.objects.create(audit=first, file=ContentFile(payload, name="evil.txt"), findings=findings)
    second = FileUploadAudit.objects.create(file_hash=file_hash, **audit_kwargs)

    with mock.patch.object(
        QuarantinedFile.objects, "bulk_create", wraps=QuarantinedFile.objects.bulk_create
    ) as bulk_create:
        scan_for_malware(app="events", model="AdditionalResource", pk=str(resource.pk), field="file")

    (created,) = bulk_create.call_args.args
    assert [q.audit_id for q in created] == [second.pk]
    second.refresh_from_db()
    assert second.status == FileUploadAudit.FileUploadAuditStatus.MALICIOUS
    assert QuarantinedFile.objects.filter(audit=second).exists()
    assert QuarantinedFile.objects.count() == 2


@mock.patch("common.tasks.send_email.delay")
def test_notify_malware_detected_is_scoped_to_the_instance(
    mock_send_email: MagicMock, revel_user_factory: RevelUserFactory
) -> None:
    """Same bytes on two instances: notifying for one must not consume the other's audit."""
    uploader = revel_user_factory()
    owner = revel_user_factory()
    org_a = Organization.objects.create(name="Org A", owner=owner)
    org_b = Organization.objects.create(name="Org B", owner=owner)
    findings = {"stream": ("FOUND", "Test-Virus")}
    file_hash = hashlib.sha256(b"test content").hexdigest()
    audit_kwargs: dict[str, t.Any] = {
        "app": "events",
        "model": "Organization",
        "field": "logo",
        "file_hash": file_hash,
        "uploader": uploader.email,
        "status": FileUploadAudit.FileUploadAuditStatus.MALICIOUS,
    }
    FileUploadAudit.objects.create(instance_pk=org_a.pk, **audit_kwargs)
    audit_b = FileUploadAudit.objects.create(instance_pk=org_b.pk, **audit_kwargs)
    QuarantinedFile.objects.create(audit=audit_b, file=ContentFile(b"test content", name="t.jpg"), findings=findings)

    notify_malware_detected(
        app="events", model="Organization", pk=str(org_a.pk), field="logo", file_hash=file_hash, findings=findings
    )

    audit_b.refresh_from_db()
    assert audit_b.notified is False
    mock_send_email.assert_not_called()  # org_a's own audit has no quarantine row to notify from
