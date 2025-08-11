"""Tests for the accounts tasks."""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from accounts.tasks import generate_user_data_export


@pytest.mark.django_db(transaction=True)
def test_generate_user_data_export_sends_failure_email(
    user: RevelUser, staff_user: RevelUser, mailoutbox: list[MagicMock]
) -> None:
    """Test that the failure email is sent when the data export fails."""
    with (
        patch("accounts.service.gdpr.generate_user_data_export", side_effect=Exception),
        patch(
            "common.tasks.to_safe_email_address",
        ) as to_safe_email_address_mock,
    ):
        to_safe_email_address_mock.side_effect = lambda e, site_settings=None: e
        generate_user_data_export(str(user.id))

    assert len(mailoutbox) == 2

    user_email_sent = False
    admin_email_sent = False

    for email in mailoutbox:
        if user.email in email.bcc:
            assert email.subject == "Your Revel Data Export has Failed"
            user_email_sent = True
        if staff_user.email in email.bcc:
            assert email.subject == "User Data Export Failed"
            admin_email_sent = True

    assert user_email_sent
    assert admin_email_sent
