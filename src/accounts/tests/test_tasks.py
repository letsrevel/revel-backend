"""Tests for the accounts tasks."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser, UserDataExport
from accounts.tasks import DATA_EXPORT_URL_EXPIRES_IN, cleanup_expired_data_exports, generate_user_data_export


@pytest.mark.django_db
def test_cleanup_expired_data_exports_deletes_old_files(user: RevelUser) -> None:
    """Test that files from expired exports are deleted."""
    export = UserDataExport.objects.create(
        user=user,
        status=UserDataExport.UserDataExportStatus.READY,
        completed_at=timezone.now() - timedelta(seconds=DATA_EXPORT_URL_EXPIRES_IN + 1),
    )
    export.file.save("test_export.zip", ContentFile(b"test content"), save=True)
    assert export.file

    result = cleanup_expired_data_exports()

    export.refresh_from_db()
    assert not export.file
    assert result == {"files_deleted": 1}


@pytest.mark.django_db
def test_cleanup_expired_data_exports_ignores_recent_exports(user: RevelUser) -> None:
    """Test that recent exports are not touched."""
    export = UserDataExport.objects.create(
        user=user,
        status=UserDataExport.UserDataExportStatus.READY,
        completed_at=timezone.now() - timedelta(days=1),
    )
    export.file.save("test_export.zip", ContentFile(b"test content"), save=True)
    assert export.file

    result = cleanup_expired_data_exports()

    export.refresh_from_db()
    assert export.file
    assert result == {"files_deleted": 0}


@pytest.mark.django_db(transaction=True)
def test_generate_user_data_export_sends_failure_email(
    user: RevelUser, staff_user: RevelUser, mailoutbox: list[MagicMock]
) -> None:
    """Test that the failure email is sent when the data export fails, then exception is re-raised."""
    with (
        patch("accounts.service.gdpr.generate_user_data_export", side_effect=Exception("Export failed")),
        patch(
            "common.tasks.to_safe_email_address",
        ) as to_safe_email_address_mock,
        pytest.raises(Exception, match="Export failed"),
    ):
        to_safe_email_address_mock.side_effect = lambda e, site_settings=None: e
        generate_user_data_export(str(user.id))

    # Emails should have been sent before the exception was re-raised
    assert len(mailoutbox) == 2

    user_email_sent = False
    admin_email_sent = False

    for email in mailoutbox:
        # Single recipients go to 'to', multiple recipients use 'bcc'
        recipients = email.to + email.bcc
        if user.email in recipients:
            assert email.subject == "Your Revel Data Export has Failed"
            user_email_sent = True
        if staff_user.email in recipients:
            assert email.subject == "User Data Export Failed"
            admin_email_sent = True

    assert user_email_sent
    assert admin_email_sent
