"""Guest confirmation emails render in the guest's language (#1214)."""

import typing as t

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import translation

from accounts.models import RevelUser
from events.models import Event
from events.service.guest import get_or_create_guest_user
from events.tasks import send_guest_rsvp_confirmation, send_guest_ticket_confirmation

pytestmark = pytest.mark.django_db


def _subject(msgid: str, language: str, **params: str) -> str:
    with translation.override(language):
        return translation.gettext(msgid) % params


def _assert_bodies_contain(sent: mail.EmailMessage, text: str) -> None:
    """Both the plain-text and the HTML body carry ``text`` (the German body, not only the subject)."""
    assert "click" not in text  # really translated, not the English msgid
    assert text in sent.body
    html = t.cast(mail.EmailMultiAlternatives, sent).alternatives[0][0]
    assert text in str(html)


def test_guest_user_stores_request_language() -> None:
    """A guest created under a German request gets language 'de'."""
    with translation.override("de"):
        user = get_or_create_guest_user("lang@example.com")

    assert user.language == "de"


def test_guest_user_language_falls_back_for_unsupported() -> None:
    """An unsupported active language falls back to the platform default."""
    with translation.override("ja"):
        user = get_or_create_guest_user("lang@example.com")

    assert user.language == "en"


def test_existing_guest_language_not_overwritten_nor_regular_account(django_user_model: type[RevelUser]) -> None:
    """A later anonymous request can't change an existing guest's language; a real account is rejected, untouched."""
    from events.exceptions import GuestAccountExistsError

    with translation.override("de"):
        get_or_create_guest_user("lang@example.com")
    with translation.override("fr"):
        again = get_or_create_guest_user("LANG@example.com")
    assert again.language == "de"
    again.refresh_from_db()
    assert again.language == "de"

    django_user_model.objects.create_user(username="real@example.com", email="real@example.com", password="x")
    with translation.override("de"), pytest.raises(GuestAccountExistsError):
        get_or_create_guest_user("real@example.com")
    assert django_user_model.objects.get(email="real@example.com").language == "en"


def test_guest_rsvp_endpoint_stores_accept_language(guest_event: Event) -> None:
    """The RSVP endpoint picks the language up from Accept-Language."""
    url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})

    response = Client().post(
        url,
        data={"email": "de-guest@example.com", "first_name": "A", "last_name": "B"},
        content_type="application/json",
        headers={"accept-language": "de"},
    )

    assert response.status_code == 200
    assert RevelUser.objects.get(email="de-guest@example.com").language == "de"


def test_rsvp_confirmation_email_is_german() -> None:
    """send_guest_rsvp_confirmation renders subject and body in the guest's language."""
    with translation.override("de"):
        get_or_create_guest_user("de@example.com")

    send_guest_rsvp_confirmation("de@example.com", "tok", "Party")

    sent = mail.outbox[0]
    expected = _subject("Confirm your RSVP to %(event_name)s", "de", event_name="Party")
    assert sent.subject == expected
    assert expected != "Confirm your RSVP to Party"
    _assert_bodies_contain(sent, _subject("To confirm your RSVP, please click the link below:", "de"))


def test_ticket_confirmation_email_is_german() -> None:
    """send_guest_ticket_confirmation renders subject and body in the guest's language."""
    with translation.override("de"):
        get_or_create_guest_user("de@example.com")

    send_guest_ticket_confirmation("de@example.com", "tok", "Party", "GA")

    sent = mail.outbox[0]
    expected = _subject("Confirm your ticket for %(event_name)s", "de", event_name="Party")
    assert sent.subject == expected
    assert expected != "Confirm your ticket for Party"
    _assert_bodies_contain(sent, _subject("To confirm your ticket purchase, please click the link below:", "de"))


def test_confirmation_email_unknown_guest_stays_english() -> None:
    """No user row (older in-flight message): default language, no crash."""
    send_guest_rsvp_confirmation("nobody@example.com", "tok", "Party")

    assert mail.outbox[0].subject == "Confirm your RSVP to Party"
