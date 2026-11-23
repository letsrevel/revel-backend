"""The email backend guard never sends to an erased account's placeholder address (#1476)."""

import typing as t
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.mail import EmailMessage

from common.email_backend import ErasedRecipientGuardBackend, erased_placeholder_email, is_erased_address
from common.models import EmailLog, SiteSettings
from common.tasks import send_email, to_safe_email_address

pytestmark = pytest.mark.django_db

ERASED = erased_placeholder_email("0" * 32)


@pytest.fixture(autouse=True)
def guarded_locmem(settings: t.Any) -> None:
    """The production backend chain, with the locmem transport so the test can read ``mail.outbox``."""
    settings.EMAIL_BACKEND = "common.email_backend.ErasedRecipientGuardBackend"
    settings.EMAIL_GUARDED_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture(params=[True, False], ids=["live", "catch-all"])
def live_emails(request: pytest.FixtureRequest) -> bool:
    """Both address forms: as typed (live emails) and rewritten to the internal catch-all."""
    site = SiteSettings.get_solo()
    site.live_emails = request.param
    site.save()
    return t.cast(bool, request.param)


def test_production_settings_route_every_email_through_the_guard() -> None:
    from revel.settings import email as email_settings

    assert email_settings.EMAIL_BACKEND == "common.email_backend.ErasedRecipientGuardBackend"
    assert email_settings.EMAIL_GUARDED_BACKEND.endswith(("smtp.EmailBackend", "console.EmailBackend"))


def _rewritten(address: str) -> str:
    """``address`` as the catch-all rewrite sends it when live emails are off."""
    site = SiteSettings.get_solo()
    site.live_emails = False
    return to_safe_email_address(address, site_settings=site)


@pytest.mark.parametrize("address", [ERASED, f"Name <{ERASED}>", ERASED.upper(), "anything@erased.invalid"])
def test_erased_addresses_are_recognised(address: str) -> None:
    assert is_erased_address(address)


def test_the_rewritten_placeholder_at_the_catch_all_address_is_recognised() -> None:
    rewritten = _rewritten(ERASED)
    assert rewritten.endswith("_at_erased_dot_invalid@" + SiteSettings.get_solo().internal_catchall_email.split("@")[1])

    assert is_erased_address(rewritten)
    assert is_erased_address(f"Name <{rewritten.upper()}>")


@pytest.mark.parametrize(
    "address",
    [
        "holder@example.com",
        "erased@invalid.example.com",
        "a@erased.invalid.org",
        "alerts_at_erased_dot_invalid@example.com",  # a real mailbox with the same ending
        "erased-" + "0" * 32 + "_at_erased_dot_invalid@example.com",  # the format, not at the catch-all address
    ],
)
def test_real_addresses_are_not(address: str) -> None:
    assert not is_erased_address(address)


@pytest.mark.parametrize(
    "local_part",
    [
        "alerts_at_erased_dot_invalid",  # not the generated placeholder
        "erased-00_at_erased_dot_invalid",  # not a full account id
        "x-erased-" + "0" * 32 + "_at_erased_dot_invalid",  # more text before the placeholder
    ],
)
def test_other_rewritten_addresses_at_the_catch_all_address_are_not(local_part: str) -> None:
    user, domain = SiteSettings.get_solo().internal_catchall_email.split("@")

    assert not is_erased_address(f"{user}+{local_part}@{domain}")
    assert not is_erased_address(f"other+{'erased-' + '0' * 32}_at_erased_dot_invalid@{domain}")
    # The exact local part of the rewrite, at a domain that is not the catch-all domain: a real mailbox.
    assert not is_erased_address(f"{user}+{'erased-' + '0' * 32}_at_erased_dot_invalid@not-{domain}")


def test_a_real_mailbox_whose_name_ends_like_a_rewritten_placeholder_gets_its_mail(live_emails: bool) -> None:
    callback = {"module": "accounts.tasks", "function": "mark_reminder_sent", "kwargs": {}}

    with patch("common.tasks._execute_email_callback") as run:
        send_email(to="alerts_at_erased_dot_invalid@example.com", subject="s", body="b", callback_data=callback)

    assert len(mail.outbox) == 1
    assert any("alerts_at_erased_dot_invalid" in address for address in mail.outbox[0].recipients())
    assert EmailLog.objects.count() == 1
    run.assert_called_once_with(callback, True, None)


def test_a_real_address_on_a_longer_domain_still_gets_its_mail(live_emails: bool) -> None:
    """``erased.invalid.org`` only starts like the placeholder domain, also after the catch-all rewrite."""
    send_email(to="a@erased.invalid.org", subject="s", body="b")

    assert len(mail.outbox) == 1


def test_a_message_to_only_an_erased_address_is_not_sent(live_emails: bool) -> None:
    send_email(to=ERASED, subject="s", body="b")

    assert mail.outbox == []


def test_the_erased_recipient_is_removed_and_the_others_still_get_the_mail(live_emails: bool) -> None:
    send_email(to=[ERASED, "holder@example.com"], subject="s", body="b", bcc=[ERASED])

    assert len(mail.outbox) == 1
    recipients = mail.outbox[0].recipients()
    assert not any(is_erased_address(address) for address in recipients)
    assert any("holder" in address for address in recipients)


def test_a_batch_to_only_erased_addresses_is_not_sent_to_the_sender(live_emails: bool) -> None:
    """A batch goes out with the sender in To and the recipients in Bcc: no recipient left means no mail."""
    other = erased_placeholder_email("1" * 32)

    send_email(to=[ERASED, other], subject="s", body="b", bcc=[ERASED])

    assert mail.outbox == []
    assert not EmailLog.objects.exists()


def test_a_batch_keeps_its_real_recipients_and_gets_no_log_row_for_an_erased_one(live_emails: bool) -> None:
    other = erased_placeholder_email("1" * 32)

    send_email(to=[ERASED, "holder@example.com", "second@example.com", other], subject="s", body="b")

    assert len(mail.outbox) == 1
    recipients = mail.outbox[0].recipients()
    assert sum("holder" in address for address in recipients) == 1
    assert sum("second" in address for address in recipients) == 1
    assert not any(is_erased_address(address) for address in recipients)
    assert EmailLog.objects.count() == 2


def test_erased_to_addresses_with_a_real_bcc_still_reach_the_bcc(live_emails: bool) -> None:
    send_email(to=[ERASED, erased_placeholder_email("1" * 32)], subject="s", body="b", bcc=["copy@example.com"])

    assert len(mail.outbox) == 1
    assert any("copy" in address for address in mail.outbox[0].recipients())
    assert not any(is_erased_address(address) for address in mail.outbox[0].recipients())


@pytest.mark.parametrize("to", [ERASED, [ERASED, erased_placeholder_email("1" * 32)]], ids=["one", "batch"])
def test_erased_to_addresses_with_a_real_bcc_go_to_the_bcc_only(to: str | list[str]) -> None:
    """No sender in To: the copy goes out as it did when only the backend guard removed the To address."""
    SiteSettings.objects.filter(pk=SiteSettings.get_solo().pk).update(live_emails=True)
    SiteSettings.clear_cache()

    send_email(to=to, subject="s", body="b", bcc=["copy@example.com"], from_email="sender@example.com")

    assert len(mail.outbox) == 1
    assert (mail.outbox[0].to, mail.outbox[0].bcc) == ([], ["copy@example.com"])


def test_a_message_without_a_to_address_is_sent_as_before() -> None:
    """Only a message that lost every recipient to the erased filter is suppressed."""
    send_email(to=[], subject="s", body="b", bcc=["copy@example.com"], from_email="sender@example.com")
    send_email(to=[], subject="s", body="b", from_email="sender@example.com")

    assert [message.to for message in mail.outbox] == [["sender@example.com"], ["sender@example.com"]]


def test_a_suppressed_message_still_runs_its_callback_as_sent() -> None:
    callback = {"module": "accounts.tasks", "function": "mark_reminder_sent", "kwargs": {}}

    with patch("common.tasks._execute_email_callback") as run:
        send_email(to=[ERASED, erased_placeholder_email("1" * 32)], subject="s", body="b", callback_data=callback)

    run.assert_called_once_with(callback, True, None)
    assert mail.outbox == []


def test_the_guard_covers_a_message_sent_without_the_send_email_task() -> None:
    """Notification, invoice and org mail build their own messages; they all pass the backend."""
    sent = EmailMessage(subject="s", body="b", to=[ERASED], cc=[ERASED], bcc=[ERASED]).send()

    assert sent == 0
    assert mail.outbox == []


def test_the_backend_forwards_to_the_configured_transport() -> None:
    backend = ErasedRecipientGuardBackend()

    assert backend.send_messages([EmailMessage(subject="s", body="b", to=["holder@example.com"])]) == 1
    assert [message.to for message in mail.outbox] == [["holder@example.com"]]
