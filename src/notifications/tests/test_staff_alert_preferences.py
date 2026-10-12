"""Staff alerts for transactional types respect the staff member's preferences.

The mandatory rule (#1030/#1033) binds the person the transaction belongs to (the buyer). The
same types sent to org staff via ``notify_org_staff`` carry ``staff_alert`` and follow the staff
member's own channel settings, while still bypassing the digest (#506).
"""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Organization, Ticket
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification, NotificationPreference
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.telegram import TelegramChannel
from notifications.service.dispatcher import determine_delivery_channels
from notifications.service.email_policy import may_email
from notifications.service.notification_helpers import notify_org_staff
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

EMAIL = DeliveryChannel.EMAIL
IN_APP = DeliveryChannel.IN_APP
TICKET_CREATED = NotificationType.TICKET_CREATED


def _untick_all(user: RevelUser) -> None:
    prefs = user.notification_preferences
    prefs.enabled_channels = []
    prefs.save()


def _notify(user: RevelUser, *, staff_alert: bool) -> Notification:
    context = {"staff_alert": True} if staff_alert else {"k": "v"}
    return Notification.objects.create(
        user=user, notification_type=TICKET_CREATED, context=context, title="t", body="b"
    )


def test_notify_org_staff_marks_context(organization: Organization) -> None:
    with patch("notifications.signals.notification_requested.send") as send_mock:
        notify_org_staff(
            organization_id=organization.id, notification_type=TICKET_CREATED, context={"a": 1}, sender=Ticket
        )

    assert send_mock.call_args.kwargs["context"] == {"a": 1, "staff_alert": True}


def test_staff_alert_respects_unticked_channels_but_buyer_still_gets_receipt(
    organization: Organization, regular_user: RevelUser
) -> None:
    owner = organization.owner
    _untick_all(owner)
    _untick_all(regular_user)

    assert determine_delivery_channels(owner, TICKET_CREATED, staff_alert=True) == []
    assert determine_delivery_channels(regular_user, TICKET_CREATED) == [IN_APP, EMAIL]


def test_may_email_staff_alert(organization: Organization) -> None:
    owner = organization.owner
    prefs = owner.notification_preferences
    prefs.disable_channel(EMAIL)
    prefs.save()

    assert may_email(owner, TICKET_CREATED, staff_alert=True) is False
    assert may_email(owner, TICKET_CREATED) is True


def test_email_channel_refuses_staff_alert_with_email_unticked(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.disable_channel(EMAIL)
    prefs.save()

    assert EmailChannel().can_deliver(_notify(regular_user, staff_alert=True)) is False
    assert EmailChannel().can_deliver(_notify(regular_user, staff_alert=False)) is True


def test_telegram_refuses_staff_alert_with_telegram_disabled(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.enabled_channels = [IN_APP, EMAIL]
    prefs.save()
    manager = MagicMock()
    manager.filter.return_value.first.return_value = MagicMock(telegram_id=123)

    with patch.object(RevelUser, "telegram_users", new_callable=lambda: property(lambda self: manager)):
        assert TelegramChannel().can_deliver(_notify(regular_user, staff_alert=True)) is False


def test_telegram_global_switch_vetoes_per_type_override_for_staff_alert(regular_user: RevelUser) -> None:
    """The global Telegram switch is a send-time veto for non-mandatory mail, per-type overrides included.

    Seeded overrides (e.g. ORG_CONTACT_MESSAGE_RECEIVED → IN_APP + TELEGRAM) aren't stripped when
    Telegram is unticked globally, so honouring them here would message users who opted out.
    """
    prefs = regular_user.notification_preferences
    prefs.enabled_channels = [IN_APP]
    prefs.notification_type_settings[TICKET_CREATED] = {"enabled": True, "channels": [DeliveryChannel.TELEGRAM]}
    prefs.save()
    manager = MagicMock()
    manager.filter.return_value.first.return_value = MagicMock(telegram_id=123)

    with patch.object(RevelUser, "telegram_users", new_callable=lambda: property(lambda self: manager)):
        assert TelegramChannel().can_deliver(_notify(regular_user, staff_alert=True)) is False


def test_is_mandatory_and_footer_context(regular_user: RevelUser) -> None:
    buyer = _notify(regular_user, staff_alert=False)
    staff = _notify(regular_user, staff_alert=True)

    assert buyer.is_mandatory is True
    assert staff.is_mandatory is False
    assert get_template(TICKET_CREATED)._get_template_context(buyer)["context"]["is_mandatory_notification"] is True
    assert get_template(TICKET_CREATED)._get_template_context(staff)["context"]["is_mandatory_notification"] is False


def test_staff_alert_still_bypasses_digest(organization: Organization) -> None:
    owner = organization.owner
    prefs = owner.notification_preferences
    prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
    prefs.save()

    assert EMAIL in determine_delivery_channels(owner, TICKET_CREATED, staff_alert=True)
