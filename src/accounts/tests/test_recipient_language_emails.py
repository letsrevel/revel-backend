"""Emails sent from Celery tasks are rendered in the recipient's language, not the worker's.

Celery workers have no active language, so a task that renders an email without its own
``translation.override`` falls back to ``settings.LANGUAGE_CODE``. Locally tasks run eagerly inside the
request and inherit the requester's language, hiding the bug. Every test here simulates the worker by
wrapping the call in ``translation.override("en")`` and gives the recipient another language, deriving
the expected text from the catalog.
"""

import typing as t
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext

from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser, UserBillingProfile, UserDataExport
from accounts.tasks import AccountEmail, send_account_email
from accounts.utils.language import recipient_language
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

LANG = "de"


def _user(django_user_model: type[RevelUser], email: str, language: str = LANG, **kwargs: t.Any) -> RevelUser:
    return django_user_model.objects.create_user(
        username=email, email=email, password="pass", language=language, **kwargs
    )


def _de(msgid: str) -> str:
    """The German catalog entry for ``msgid``; asserts it really differs from English."""
    with translation.override(LANG):
        translated = gettext(msgid)
    assert translated != msgid
    return translated


def _render(base: str, context: dict[str, t.Any], language: str) -> tuple[str, str, str]:
    with translation.override(language):
        subject = str(render_to_string(f"accounts/emails/{base}_subject.txt")).strip()
        body = render_to_string(f"accounts/emails/{base}_body.txt", context)
        html = render_to_string(f"accounts/emails/{base}_body.html", context)
    return subject, body, html


# (email type, template base, required context, token needed)
_ACCOUNT_CASES: list[tuple[AccountEmail, str, dict[str, str], str | None]] = [
    (AccountEmail.VERIFICATION, "email_verification", {}, "/login/confirm-email?token=tok"),
    (AccountEmail.ACTIVATION, "account_activation", {}, "/login/reset-password?token=tok"),
    (AccountEmail.PASSWORD_RESET, "password_reset", {}, "/login/reset-password?token=tok"),
    (AccountEmail.CHANGE_CONFIRMATION, "email_change_confirmation", {}, "/account/confirm-email-change?token=tok"),
    (AccountEmail.CHANGE_NOTICE, "email_change_notice", {"masked_new_email": "n***@example.com"}, None),
    (
        AccountEmail.CHANGE_COMPLETED_OLD,
        "email_change_completed_old",
        {"old_email": "a@example.com", "new_email": "b@example.com"},
        None,
    ),
    (
        AccountEmail.CHANGE_COMPLETED_NEW,
        "email_change_completed_new",
        {"old_email": "a@example.com", "new_email": "b@example.com"},
        None,
    ),
    (AccountEmail.DELETION, "account_delete", {}, "/account/confirm-deletion?token=tok"),
    (AccountEmail.REFERRAL_APPLICATION_RECEIVED, "referral_application_received", {"code": "ABC"}, None),
    (
        AccountEmail.REFERRAL_INVITE,
        "referral_invite",
        {"code": "ABC", "revenue_share_percent": "10", "admin_note": "hi"},
        "/register?referral_invite=tok",
    ),
    (
        AccountEmail.REFERRAL_ENROLLED,
        "referral_enrolled",
        {"code": "ABC", "revenue_share_percent": "10"},
        None,
    ),
    (AccountEmail.REFERRAL_REJECTED, "referral_rejected", {"admin_note": "no"}, None),
]


@pytest.mark.parametrize("via", ["context", "lookup"])
@pytest.mark.parametrize("email_type, base, extra, link", _ACCOUNT_CASES, ids=[c[0] for c in _ACCOUNT_CASES])
def test_account_email_rendered_in_recipient_language(
    django_user_model: type[RevelUser],
    email_type: AccountEmail,
    base: str,
    extra: dict[str, str],
    link: str | None,
    via: str,
) -> None:
    """Subject, text and HTML are German although the worker's language is English.

    ``context``: the address is not an account (email change to a NEW address), so the language rides in
    the task's context. ``lookup``: no language in context, resolved from the account matching ``to``.
    """
    to = "recipient@example.com"
    context = dict(extra)
    if via == "lookup":
        _user(django_user_model, to)
    else:
        context["language"] = LANG
    frontend = SiteSettings.get_solo().frontend_base_url
    expected_ctx = {"frontend_base_url": frontend, **extra}
    if link:
        expected_ctx["action_link"] = frontend + link
    subject_de, body_de, html_de = _render(base, expected_ctx, LANG)
    subject_en, body_en, _html_en = _render(base, expected_ctx, "en")
    assert (subject_de, body_de) != (subject_en, body_en)

    with patch("accounts.tasks.email.send_email") as mock_send, translation.override("en"):
        send_account_email(email_type, to, token="tok" if link else None, context=context)

    kwargs = mock_send.call_args.kwargs
    assert kwargs["subject"] == subject_de
    assert kwargs["body"] == body_de
    assert kwargs["html_body"] == html_de


def test_account_email_invalid_context_language_falls_back_to_account(django_user_model: type[RevelUser]) -> None:
    _user(django_user_model, "recipient@example.com")
    with patch("accounts.tasks.email.send_email") as mock_send, translation.override("en"):
        send_account_email(
            AccountEmail.REFERRAL_REJECTED,
            "recipient@example.com",
            context={
                "admin_note": "x",
                "language": "xx",
            },
        )
    assert mock_send.call_args.kwargs["subject"] == _render("referral_rejected", {}, LANG)[0]


# ---------------------------------------------------------------------------
# recipient_language
# ---------------------------------------------------------------------------


class TestRecipientLanguage:
    def test_user_given(self, django_user_model: type[RevelUser]) -> None:
        assert recipient_language(user=_user(django_user_model, "a@example.com", "it")) == "it"

    def test_email_lookup_is_case_insensitive(self, django_user_model: type[RevelUser]) -> None:
        _user(django_user_model, "Mixed@Example.com", "fr")
        assert recipient_language("mixed@example.COM") == "fr"

    def test_guest_is_found(self, django_user_model: type[RevelUser]) -> None:
        _user(django_user_model, "guest@example.com", "es", guest=True)
        assert recipient_language("guest@example.com") == "es"

    def test_unknown_email_uses_default(self) -> None:
        assert recipient_language("nobody@example.com") == "en"
        assert recipient_language() == "en"

    def test_invalid_stored_language_uses_default(self, django_user_model: type[RevelUser]) -> None:
        user = _user(django_user_model, "bad@example.com")
        RevelUser.objects.filter(pk=user.pk).update(language="xx")
        assert recipient_language("bad@example.com") == "en"
        user.refresh_from_db()
        assert recipient_language(user=user) == "en"


# ---------------------------------------------------------------------------
# Verification reminders / deactivation notice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reminder_type", ["early", "final_warning"])
def test_verification_reminder_language(django_user_model: type[RevelUser], reminder_type: str) -> None:
    from accounts.tasks.verification_reminders import _send_reminder_email

    user = _user(django_user_model, "unverified@example.com", email_verified=False)
    with patch("accounts.tasks.verification_reminders.send_email") as mock_send, translation.override("en"):
        _send_reminder_email(user, reminder_type)

    base = "email_verification_final_warning" if reminder_type == "final_warning" else "email_verification_reminder"
    kwargs = mock_send.delay.call_args.kwargs
    expected_subject = _render(base, {}, LANG)[0]
    assert expected_subject != _render(base, {}, "en")[0]
    assert kwargs["subject"] == expected_subject
    assert "token=" in kwargs["body"]  # the real (non-mocked) render happened


def test_deactivation_notice_language(django_user_model: type[RevelUser]) -> None:
    from accounts.tasks.verification_reminders import _send_deactivation_email

    user = _user(django_user_model, "unverified@example.com", email_verified=False)
    with patch("accounts.tasks.verification_reminders.send_email") as mock_send, translation.override("en"):
        _send_deactivation_email(user)

    expected_subject = _render("account_deactivated", {}, LANG)[0]
    assert expected_subject != _render("account_deactivated", {}, "en")[0]
    assert mock_send.delay.call_args.kwargs["subject"] == expected_subject


# ---------------------------------------------------------------------------
# GDPR export
# ---------------------------------------------------------------------------


def test_export_ready_email_language(django_user_model: type[RevelUser]) -> None:
    from accounts.tasks.gdpr import _notify_user_data_export_ready

    user = _user(django_user_model, "export@example.com")
    export = UserDataExport.objects.create(user=user)
    export.file.name = "exports/x.zip"
    with (
        patch("accounts.tasks.gdpr.send_email") as mock_send,
        patch("accounts.tasks.gdpr.generate_signed_url", return_value="/signed"),
        translation.override("en"),
    ):
        _notify_user_data_export_ready(export)

    kwargs = mock_send.call_args.kwargs
    assert kwargs["subject"] == _de("Your Revel Data Export is Ready")
    assert kwargs["subject"] != "Your Revel Data Export is Ready"
    with translation.override(LANG):
        body_de = render_to_string("accounts/emails/data_export_ready_body.txt", {"download_url": "x", "user": user})
    with translation.override("en"):
        body_en = render_to_string("accounts/emails/data_export_ready_body.txt", {"download_url": "x", "user": user})
    assert body_de != body_en
    only_de = next(line for line in body_de.splitlines() if line.strip() and line not in body_en)
    only_en = next(line for line in body_en.splitlines() if line.strip() and line not in body_de)
    assert only_de in kwargs["body"]
    assert only_en not in kwargs["body"]


def test_export_failed_user_and_admins_each_get_own_language(django_user_model: type[RevelUser]) -> None:
    from accounts.tasks.gdpr import _notify_data_export_failed

    user = _user(django_user_model, "export@example.com", "de")
    _user(django_user_model, "admin-it@example.com", "it", is_staff=True)
    with patch("accounts.tasks.gdpr.send_email") as mock_send, translation.override("en"):
        _notify_data_export_failed(user, "boom")

    by_to = {c.kwargs["to"]: c.kwargs["subject"] for c in mock_send.call_args_list}
    assert by_to["export@example.com"] == _render("data_export_failed", {}, "de")[0]
    assert by_to["admin-it@example.com"] == _render("data_export_failed_admin", {}, "it")[0]
    assert by_to["admin-it@example.com"] != _render("data_export_failed_admin", {}, "en")[0]


# ---------------------------------------------------------------------------
# Referral payout statement and forfeiture notice
# ---------------------------------------------------------------------------


def test_payout_statement_uses_referrer_language_even_for_billing_email(django_user_model: type[RevelUser]) -> None:
    from accounts.tasks.payouts import _send_payout_statement_email

    referrer = _user(django_user_model, "referrer@example.com")
    UserBillingProfile.objects.create(user=referrer, billing_name="R", billing_email="billing@elsewhere.at")
    code = ReferralCode.objects.create(user=referrer, code="LANGREF")
    referred = _user(django_user_model, "referred@example.com", "en")
    referral = Referral.objects.create(referral_code=code, referred_user=referred, revenue_share_percent=Decimal("10"))
    payout = ReferralPayout.objects.create(
        referral=referral,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        net_platform_fees=Decimal("100"),
        payout_amount=Decimal("10"),
        currency="EUR",
        status=ReferralPayout.ReferralPayoutStatus.PAID,
    )
    statement = MagicMock(document_number="DOC-1")
    statement.pdf_file.name = "x.pdf"

    with (
        patch("accounts.service.payout_statement_service.ensure_payout_statement_pdf_exists"),
        patch("accounts.tasks.payouts.send_email") as mock_send,
        translation.override("en"),
    ):
        _send_payout_statement_email(payout, statement, referrer)

    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == "billing@elsewhere.at"
    with translation.override(LANG):
        expected = gettext("Referral payout statement %(document_number)s (%(currency)s)") % {
            "document_number": "DOC-1",
            "currency": "EUR",
        }
    assert expected != "Referral payout statement DOC-1 (EUR)"
    assert kwargs["subject"] == expected


def test_forfeiture_notice_per_admin_language(django_user_model: type[RevelUser]) -> None:
    from accounts.service.referral_cleanup import ReferralForfeitureSummary, _notify_admins_of_forfeiture

    _user(django_user_model, "admin-de@example.com", "de", is_staff=True)
    _user(django_user_model, "admin-it@example.com", "it", is_staff=True)
    leaving = _user(django_user_model, "leaving@example.com", "en")
    summary = ReferralForfeitureSummary(
        items=[], unpaid_count=0, unpaid_total=Decimal("0"), failed_total=Decimal("0"), currency="EUR"
    )
    ctx = {"user_email": leaving.email, "user_id": str(leaving.id), "items": [], "unpaid_count": 0}
    with patch("accounts.service.referral_cleanup.send_email") as mock_send, translation.override("en"):
        _notify_admins_of_forfeiture(leaving, summary)

    by_to = {c.kwargs["to"]: c.kwargs["subject"] for c in mock_send.call_args_list}
    assert by_to["admin-de@example.com"] == _render("referral_forfeiture_admin", ctx, "de")[0]
    assert by_to["admin-it@example.com"] == _render("referral_forfeiture_admin", ctx, "it")[0]
    assert by_to["admin-de@example.com"] != by_to["admin-it@example.com"]
