"""Tests for the OrganizationContactMessage post_save signal handler."""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Organization, OrganizationContactMessage, OrganizationStaff, PermissionMap, PermissionsSchema
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import get_default_notification_type_settings

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def org_in_form_mode(organization: Organization) -> Organization:
    """Org wired up to receive form submissions."""
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()
    return organization


def test_default_channels_are_in_app_and_telegram() -> None:
    """ORG_CONTACT_MESSAGE_RECEIVED defaults to IN_APP + TELEGRAM (no email)."""
    defaults = get_default_notification_type_settings()
    setting = defaults.get(NotificationType.ORG_CONTACT_MESSAGE_RECEIVED)
    assert setting is not None
    assert setting["enabled"] is True
    channels = setting["channels"]
    assert DeliveryChannel.IN_APP in channels
    assert DeliveryChannel.TELEGRAM in channels
    assert DeliveryChannel.EMAIL not in channels


@patch("events.tasks.send_organization_contact_message_email.delay")
@patch("notifications.signals.contact.notification_requested.send")
def test_signal_dispatches_to_owner_and_edit_org_staff(
    mock_signal: MagicMock,
    mock_email: MagicMock,
    org_in_form_mode: Organization,
    organization_owner_user: RevelUser,
    organization_staff_user: RevelUser,
) -> None:
    """A new contact message dispatches notifications to org owner + staff with edit_organization."""
    OrganizationStaff.objects.create(
        organization=org_in_form_mode,
        user=organization_staff_user,
        permissions=PermissionsSchema(default=PermissionMap(edit_organization=True)).model_dump(mode="json"),
    )
    other_staff = RevelUser.objects.create_user(username="no_perm_staff", email="noperm@example.com", password="pass")
    OrganizationStaff.objects.create(
        organization=org_in_form_mode,
        user=other_staff,
        permissions=PermissionsSchema(default=PermissionMap(edit_organization=False)).model_dump(mode="json"),
    )

    sender = RevelUser.objects.create_user(username="contactsender", email="contactsender@example.com", password="pass")
    OrganizationContactMessage.objects.create(
        organization=org_in_form_mode,
        sender=sender,
        sender_email_snapshot=sender.email,
        subject="Question",
        message="Hello there",
    )

    # Filter to ORG_CONTACT_MESSAGE_RECEIVED dispatches only
    calls = [
        call
        for call in mock_signal.call_args_list
        if call.kwargs.get("notification_type") == NotificationType.ORG_CONTACT_MESSAGE_RECEIVED
    ]

    notified_user_ids = {call.kwargs["user"].id for call in calls}
    assert organization_owner_user.id in notified_user_ids
    assert organization_staff_user.id in notified_user_ids
    assert other_staff.id not in notified_user_ids

    # Email task fired exactly once for the message
    assert mock_email.called


@patch("events.tasks.send_organization_contact_message_email.delay")
@patch("notifications.signals.contact.notification_requested.send")
def test_signal_does_not_fire_on_update(
    mock_signal: MagicMock,
    mock_email: MagicMock,
    org_in_form_mode: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Only post_save with created=True triggers dispatch."""
    msg = OrganizationContactMessage.objects.create(
        organization=org_in_form_mode,
        sender=organization_owner_user,
        sender_email_snapshot=organization_owner_user.email,
        message="Body",
    )
    mock_signal.reset_mock()
    mock_email.reset_mock()

    msg.subject = "Changed"
    msg.save(update_fields=["subject"])

    contact_calls = [
        call
        for call in mock_signal.call_args_list
        if call.kwargs.get("notification_type") == NotificationType.ORG_CONTACT_MESSAGE_RECEIVED
    ]
    assert contact_calls == []
    assert not mock_email.called
