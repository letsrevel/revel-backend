"""Digest, EVENT_UPDATED and pending-invitation emails render in the recipient's language.

Celery workers (and the editor's request, for signal handlers) run in a language that is not the
recipient's. These tests wrap the call in ``translation.override("en")`` -- the worker's default -- and
give the recipients other languages, deriving expectations from the catalog.
"""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.utils import translation
from django.utils.translation import gettext, ngettext

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, PendingEventInvitation
from notifications.models import Notification
from notifications.service import notification_helpers
from notifications.service.digest import NotificationDigest
from notifications.signals import event as event_signals
from notifications.tasks import send_pending_invitation_email

pytestmark = pytest.mark.django_db


def _tr(language: str, msgid: str) -> str:
    with translation.override(language):
        translated = gettext(msgid)
    assert translated != msgid
    return translated


def test_digest_renders_in_recipient_language_outside_any_override(
    regular_user: RevelUser, digest_notifications: list[Notification]
) -> None:
    """The real send path: no caller-side override, as in a Celery worker."""
    RevelUser.objects.filter(pk=regular_user.pk).update(language="de")
    regular_user.refresh_from_db()
    digest = NotificationDigest(regular_user, Notification.objects.filter(user=regular_user))

    with patch("common.tasks.send_email.delay") as mock_send, translation.override("en"):
        assert digest.send_digest_email()

    kwargs = mock_send.call_args.kwargs
    with translation.override("de"):
        expected_subject = ngettext("%(counter)s new notification", "%(counter)s new notifications", 3) % {"counter": 3}
    assert expected_subject != "3 new notifications"
    assert kwargs["subject"] == expected_subject
    assert _tr("de", "Your Notification Digest") in kwargs["body"]
    assert _tr("de", "Best regards,") in kwargs["html_body"]
    assert "Best regards," not in kwargs["html_body"]


def _eligible(*users: RevelUser) -> MagicMock:
    eligible = MagicMock()
    eligible.__iter__.return_value = iter(users)
    eligible.count.return_value = len(users)
    return eligible


class TestEventUpdatedPerRecipientLanguage:
    def test_each_recipient_gets_their_own_language(self, public_event: Event, django_user_model: t.Any) -> None:
        de_user = django_user_model.objects.create_user(
            username="de@example.com", email="de@example.com", language="de"
        )
        it_user = django_user_model.objects.create_user(
            username="it@example.com", email="it@example.com", language="it"
        )
        old_start = public_event.start - timedelta(days=1)
        changed = ["start", "city"]
        old_values: dict[str, event_signals._DiffValue] = {"start": old_start, "city": None}
        new_values: dict[str, event_signals._DiffValue] = {"start": public_event.start, "city": "Wien"}

        with (
            patch.object(
                event_signals, "get_eligible_users_for_event_notification", return_value=_eligible(de_user, it_user)
            ),
            patch.object(event_signals, "get_event_location_for_user", return_value=("Somewhere", None)),
            patch("notifications.signals.event.notification_requested.send") as send_mock,
            patch("django.db.transaction.on_commit", side_effect=lambda f: f()),
            translation.override("en"),  # the editor's language must not leak
        ):
            event_signals._handle_event_updated(Event, public_event, changed, old_values, new_values)

        sent = {c.kwargs["user"].language: c.kwargs["context"] for c in send_mock.call_args_list}
        assert set(sent) == {"de", "it"}
        for lang in ("de", "it"):
            not_set = _tr(lang, "Not set")
            with translation.override(lang):
                expected_start = notification_helpers.format_event_datetime(public_event.start, public_event)
                expected_old = notification_helpers.format_event_datetime(old_start, public_event)
            ctx = sent[lang]
            assert ctx["old_values"] == {"start": expected_old, "city": not_set}
            assert ctx["new_values"]["start"] == expected_start
            assert ctx["event_start_formatted"] == expected_start
            assert not_set in ctx["update_message"]
            assert "Not set" not in ctx["update_message"]
        assert sent["de"]["event_start_formatted"] != sent["it"]["event_start_formatted"]

    def test_fallback_message_is_translated_per_recipient(self, public_event: Event, django_user_model: t.Any) -> None:
        user = django_user_model.objects.create_user(username="de@example.com", email="de@example.com", language="de")
        with (
            patch.object(event_signals, "get_eligible_users_for_event_notification", return_value=_eligible(user)),
            patch.object(event_signals, "get_event_location_for_user", return_value=("Somewhere", None)),
            patch("notifications.signals.event.notification_requested.send") as send_mock,
            patch("django.db.transaction.on_commit", side_effect=lambda f: f()),
            translation.override("en"),
        ):
            event_signals._handle_event_updated(Event, public_event, [], {}, {})

        assert send_mock.call_args.kwargs["context"]["update_message"] == _tr("de", "Event details have been updated.")


class TestPendingInvitationLanguage:
    @pytest.fixture(autouse=True)
    def _site(self, settings: t.Any) -> None:
        settings.BASE_URL = "https://api.example.test"
        site = SiteSettings.get_solo()
        site.live_emails = True
        site.save()

    def test_uses_language_of_guest_row_with_that_address(self, public_event: Event, django_user_model: t.Any) -> None:
        django_user_model.objects.create_user(
            username="invitee@example.com", email="invitee@example.com", language="de", guest=True
        )
        pending = PendingEventInvitation.objects.create(event=public_event, email="invitee@example.com")

        with translation.override("en"):
            send_pending_invitation_email.apply(args=(str(pending.id),))

        msg = mail.outbox[-1]
        with translation.override("de"):
            expected = gettext("You're invited: %(event_name)s") % {"event_name": public_event.name}
        assert expected != f"You're invited: {public_event.name}"
        assert msg.subject == expected
        html = str(t.cast(EmailMultiAlternatives, msg).alternatives[0][0])
        assert "Stop these emails" not in msg.body
        assert "Stop these emails" not in html

    def test_unknown_address_falls_back_to_default_language(self, public_event: Event) -> None:
        pending = PendingEventInvitation.objects.create(event=public_event, email="stranger@example.com")
        with translation.override("it"):
            send_pending_invitation_email.apply(args=(str(pending.id),))
        assert mail.outbox[-1].subject == f"You're invited: {public_event.name}"
