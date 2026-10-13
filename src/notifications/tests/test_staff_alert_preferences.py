"""Staff alerts have their own notification types and follow the staff member's preferences (#1127).

The mandatory rule (#1030/#1033) binds the person the transaction belongs to (the buyer), and is
decided by notification type alone. Staff alerts use the ``STAFF_*`` types, which are never
mandatory; the ticket ones are transactional, so they still bypass the digest (#506).
"""

from unittest.mock import MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Organization, Ticket
from notifications.enums import MANDATORY_TYPES, TRANSACTIONAL_TYPES, DeliveryChannel, NotificationType
from notifications.models import Notification, NotificationPreference
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.telegram import TelegramChannel
from notifications.service.dispatcher import determine_delivery_channels
from notifications.service.email_policy import may_email
from notifications.service.notification_helpers import notify_org_staff
from notifications.service.templates.registry import get_template
from notifications.tasks import send_notification_digests

pytestmark = pytest.mark.django_db

EMAIL = DeliveryChannel.EMAIL
IN_APP = DeliveryChannel.IN_APP
TICKET_CREATED = NotificationType.TICKET_CREATED
STAFF_TICKET_CREATED = NotificationType.STAFF_TICKET_CREATED
STAFF_TICKET_TYPES = (
    NotificationType.STAFF_TICKET_CREATED,
    NotificationType.STAFF_TICKET_CANCELLED,
    NotificationType.STAFF_TICKET_REFUNDED,
)
STAFF_SERIES_PASS_TYPES = (
    NotificationType.STAFF_SERIES_PASS_PURCHASED,
    NotificationType.STAFF_SERIES_PASS_CANCELLED,
)
STAFF_TYPES = (*STAFF_TICKET_TYPES, *STAFF_SERIES_PASS_TYPES)


def _untick_all(user: RevelUser) -> None:
    prefs = user.notification_preferences
    prefs.enabled_channels = []
    prefs.save()


def _notify(user: RevelUser, notification_type: NotificationType = STAFF_TICKET_CREATED) -> Notification:
    return Notification.objects.create(
        user=user, notification_type=notification_type, context={"k": "v"}, title="t", body="b"
    )


def _with_telegram(user: RevelUser) -> MagicMock:
    manager = MagicMock()
    manager.filter.return_value.first.return_value = MagicMock(telegram_id=123)
    return manager


def test_staff_types_are_never_mandatory() -> None:
    assert MANDATORY_TYPES.isdisjoint(STAFF_TYPES)


def test_staff_ticket_types_are_transactional_but_series_pass_ones_are_not() -> None:
    assert set(STAFF_TICKET_TYPES) <= TRANSACTIONAL_TYPES
    assert TRANSACTIONAL_TYPES.isdisjoint(STAFF_SERIES_PASS_TYPES)


def test_notify_org_staff_sends_context_unchanged(organization: Organization) -> None:
    with patch("notifications.signals.notification_requested.send") as send_mock:
        notify_org_staff(
            organization_id=organization.id,
            notification_type=STAFF_TICKET_CREATED,
            context={"a": 1},
            sender=Ticket,
        )

    assert send_mock.call_args.kwargs["context"] == {"a": 1}


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_unticked_staff_get_nothing_while_buyer_still_gets_receipt(
    organization: Organization, regular_user: RevelUser, staff_type: NotificationType
) -> None:
    owner = organization.owner
    _untick_all(owner)
    _untick_all(regular_user)

    assert determine_delivery_channels(owner, staff_type) == []
    assert determine_delivery_channels(regular_user, TICKET_CREATED) == [IN_APP, EMAIL]


def test_may_email_staff_type(organization: Organization) -> None:
    owner = organization.owner
    prefs = owner.notification_preferences
    prefs.disable_channel(EMAIL)
    prefs.save()

    assert may_email(owner, STAFF_TICKET_CREATED) is False
    assert may_email(owner, TICKET_CREATED) is True


def test_email_channel_refuses_staff_type_with_email_unticked(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.disable_channel(EMAIL)
    prefs.save()

    assert EmailChannel().can_deliver(_notify(regular_user)) is False
    assert EmailChannel().can_deliver(_notify(regular_user, TICKET_CREATED)) is True


def test_telegram_refuses_staff_type_with_telegram_disabled(regular_user: RevelUser) -> None:
    prefs = regular_user.notification_preferences
    prefs.enabled_channels = [IN_APP, EMAIL]
    prefs.save()
    manager = _with_telegram(regular_user)

    with patch.object(RevelUser, "telegram_users", new_callable=lambda: property(lambda self: manager)):
        assert TelegramChannel().can_deliver(_notify(regular_user)) is False


def test_telegram_global_switch_vetoes_per_type_override_for_staff_type(regular_user: RevelUser) -> None:
    """The global Telegram switch is a send-time veto for non-mandatory mail, per-type overrides included.

    Seeded overrides (e.g. ORG_CONTACT_MESSAGE_RECEIVED → IN_APP + TELEGRAM) aren't stripped when
    Telegram is unticked globally, so honouring them here would message users who opted out.
    """
    prefs = regular_user.notification_preferences
    prefs.enabled_channels = [IN_APP]
    prefs.notification_type_settings[STAFF_TICKET_CREATED] = {"enabled": True, "channels": [DeliveryChannel.TELEGRAM]}
    prefs.save()
    manager = _with_telegram(regular_user)

    with patch.object(RevelUser, "telegram_users", new_callable=lambda: property(lambda self: manager)):
        assert TelegramChannel().can_deliver(_notify(regular_user)) is False


def test_is_mandatory_and_footer_context(regular_user: RevelUser) -> None:
    buyer = _notify(regular_user, TICKET_CREATED)
    staff = _notify(regular_user)

    assert buyer.is_mandatory is True
    assert staff.is_mandatory is False
    assert get_template(TICKET_CREATED)._get_template_context(buyer)["context"]["is_mandatory_notification"] is True
    staff_ctx = get_template(STAFF_TICKET_CREATED)._get_template_context(staff)
    assert staff_ctx["context"]["is_mandatory_notification"] is False


def _daily_digest(user: RevelUser) -> None:
    prefs = user.notification_preferences
    prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
    prefs.save()


@pytest.mark.parametrize("staff_type", STAFF_TICKET_TYPES)
def test_staff_ticket_alert_bypasses_digest(organization: Organization, staff_type: NotificationType) -> None:
    owner = organization.owner
    _daily_digest(owner)

    assert EMAIL in determine_delivery_channels(owner, staff_type)


@pytest.mark.parametrize("staff_type", STAFF_SERIES_PASS_TYPES)
def test_staff_series_pass_alert_does_not_bypass_digest(
    organization: Organization, staff_type: NotificationType
) -> None:
    owner = organization.owner
    _daily_digest(owner)

    assert determine_delivery_channels(owner, staff_type) == [IN_APP]


@patch("notifications.service.digest.should_send_digest_now", return_value=True)
@patch("common.tasks.send_email.delay")
class TestDigestSweepExcludesOptedOutStaff:
    """The sweep must not email a staff alert to someone who disabled that type (the #1127 leak)."""

    def test_opted_out_staff_get_no_digest(self, send: MagicMock, _now: MagicMock, organization: Organization) -> None:
        owner = organization.owner
        _daily_digest(owner)
        prefs = owner.notification_preferences
        prefs.notification_type_settings[NotificationType.STAFF_SERIES_PASS_PURCHASED] = {"enabled": False}
        prefs.save()
        _notify(owner, NotificationType.STAFF_SERIES_PASS_PURCHASED)

        assert send_notification_digests()["digests_sent"] == 0
        send.assert_not_called()

    def test_opted_in_staff_get_the_digest(self, send: MagicMock, _now: MagicMock, organization: Organization) -> None:
        owner = organization.owner
        _daily_digest(owner)
        _notify(owner, NotificationType.STAFF_SERIES_PASS_PURCHASED)

        assert send_notification_digests()["digests_sent"] == 1
