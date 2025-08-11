import hashlib
from unittest import mock
from unittest.mock import MagicMock

import pytest
from django.core.files.base import ContentFile

from common.models import FileUploadAudit, QuarantinedFile
from common.tasks import notify_malware_detected
from common.utils import safe_save_uploaded_file
from conftest import RevelUserFactory
from events.models import AdditionalResource, Organization

pytestmark = pytest.mark.django_db


@mock.patch("common.tasks.pyclamd.ClamdNetworkSocket")
def test_scan_for_malware(mock_clamd: MagicMock, revel_user_factory: RevelUserFactory) -> None:
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
    audit = FileUploadAudit.objects.first()
    assert audit is not None
    assert audit.instance_pk == instance.pk
    assert audit.status == FileUploadAudit.Status.MALICIOUS

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
        status=FileUploadAudit.Status.MALICIOUS,
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
