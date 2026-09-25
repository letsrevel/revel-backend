"""Render coverage for the consolidated ``send_account_email`` task (issue #608).

Each case independently re-renders the message's templates with the context the task is expected
to build, then asserts the task produced exactly that subject/body/html — locking the link paths,
the ``action_link`` key, and the per-type context so the task and its templates can't drift apart.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.template.loader import render_to_string

from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

_TOKEN = "test-token-123"  # noqa: S105 — not a real secret


def _render(base: str, context: dict[str, str]) -> tuple[str, str, str]:
    """Render the (subject, body, html_body) triad for an ``accounts/emails`` template base."""
    subject = str(render_to_string(f"accounts/emails/{base}_subject.txt")).strip()
    body = render_to_string(f"accounts/emails/{base}_body.txt", context)
    html_body = render_to_string(f"accounts/emails/{base}_body.html", context)
    return subject, body, html_body


# (email_type, recipient, template_base, link_path)
_LINK_CASES = [
    (AccountEmail.VERIFICATION, "u@example.com", "email_verification", "/login/confirm-email?token={token}"),
    (AccountEmail.ACTIVATION, "u@example.com", "account_activation", "/login/reset-password?token={token}"),
    (AccountEmail.PASSWORD_RESET, "u@example.com", "password_reset", "/login/reset-password?token={token}"),
    (
        AccountEmail.CHANGE_CONFIRMATION,
        "new@example.com",
        "email_change_confirmation",
        "/account/confirm-email-change?token={token}",
    ),
    (AccountEmail.DELETION, "u@example.com", "account_delete", "/account/confirm-deletion?token={token}"),
]


@pytest.mark.parametrize("email_type, to, base, link_path", _LINK_CASES)
@patch("accounts.tasks.email.send_email")
def test_link_email_renders_byte_for_byte(
    mock_send: MagicMock,
    email_type: AccountEmail,
    to: str,
    base: str,
    link_path: str,
) -> None:
    site_settings = SiteSettings.get_solo()
    full_link = site_settings.frontend_base_url + link_path.format(token=_TOKEN)
    # Every body gets frontend_base_url; every link-bearing template reads the same action_link key.
    expected_context = {"frontend_base_url": site_settings.frontend_base_url, "action_link": full_link}
    exp_subject, exp_body, exp_html = _render(base, expected_context)

    send_account_email(email_type, to, token=_TOKEN)

    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == to
    assert kwargs["subject"] == exp_subject
    assert kwargs["body"] == exp_body
    assert kwargs["html_body"] == exp_html
    # Lock the link path: the built action link must actually appear in the rendered email.
    assert full_link in (exp_body + exp_html)


# (email_type, recipient, template_base, extra_context) — informational emails, no token.
_CONTEXT_CASES = [
    (AccountEmail.CHANGE_NOTICE, "cur@example.com", "email_change_notice", {"masked_new_email": "n***@example.com"}),
    (
        AccountEmail.CHANGE_COMPLETED_OLD,
        "old@example.com",
        "email_change_completed_old",
        {"old_email": "old@example.com", "new_email": "new@example.com"},
    ),
    (
        AccountEmail.CHANGE_COMPLETED_NEW,
        "new@example.com",
        "email_change_completed_new",
        {"old_email": "old@example.com", "new_email": "new@example.com"},
    ),
]


@pytest.mark.parametrize("email_type, to, base, context", _CONTEXT_CASES)
@patch("accounts.tasks.email.send_email")
def test_context_email_renders_byte_for_byte(
    mock_send: MagicMock,
    email_type: AccountEmail,
    to: str,
    base: str,
    context: dict[str, str],
) -> None:
    site_settings = SiteSettings.get_solo()
    expected_context = {**context, "frontend_base_url": site_settings.frontend_base_url}
    exp_subject, exp_body, exp_html = _render(base, expected_context)

    send_account_email(email_type, to, context=context)

    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == to
    assert kwargs["subject"] == exp_subject
    assert kwargs["body"] == exp_body
    assert kwargs["html_body"] == exp_html


@patch("accounts.tasks.email.send_email")
def test_link_email_without_token_raises(mock_send: MagicMock) -> None:
    """A link-bearing email dispatched without a token is a programming error, not a silent no-op."""
    with pytest.raises(ValueError, match="requires a token"):
        send_account_email(AccountEmail.VERIFICATION, "u@example.com")
    mock_send.assert_not_called()


@patch("accounts.tasks.email.send_email")
def test_missing_required_context_raises(mock_send: MagicMock) -> None:
    """A message dispatched without its required context keys fails fast, not with a partial render."""
    with pytest.raises(ValueError, match="requires context keys: old_email, new_email"):
        send_account_email(AccountEmail.CHANGE_COMPLETED_OLD, "old@example.com")
    mock_send.assert_not_called()


def test_email_type_accepts_raw_string_value() -> None:
    """Celery serialises the StrEnum to its value; the task must accept the round-tripped string."""
    assert AccountEmail("verification") is AccountEmail.VERIFICATION


@patch("accounts.tasks.email.send_email")
def test_referral_invite_email_links_to_register_with_invite_id(mock_send: MagicMock) -> None:
    site = SiteSettings.get_solo()
    context = {"code": "biagio", "revenue_share_percent": "15.00", "admin_note": "Welcome aboard"}
    send_account_email(AccountEmail.REFERRAL_INVITE, "invitee@example.com", token="abc-123", context=context)

    expected = _render(
        "referral_invite",
        {
            "frontend_base_url": site.frontend_base_url,
            **context,
            "action_link": f"{site.frontend_base_url}/register?referral_invite=abc-123",
        },
    )
    mock_send.assert_called_once_with(
        to="invitee@example.com", subject=expected[0], body=expected[1], html_body=expected[2]
    )
    assert "Welcome aboard" in expected[1]
    assert "/register?referral_invite=abc-123" in expected[2]


@patch("accounts.tasks.email.send_email")
def test_referral_enrolled_email_links_to_account_referral_page(mock_send: MagicMock) -> None:
    site = SiteSettings.get_solo()
    context = {"code": "biagio", "revenue_share_percent": "20.00"}
    send_account_email(AccountEmail.REFERRAL_ENROLLED, "u@example.com", context=context)

    _, kwargs = mock_send.call_args
    assert f"{site.frontend_base_url}/account/referral" in kwargs["html_body"]
    assert "biagio" in kwargs["body"]


@patch("accounts.tasks.email.send_email")
def test_referral_rejected_email_includes_note_only_when_present(mock_send: MagicMock) -> None:
    send_account_email(AccountEmail.REFERRAL_REJECTED, "u@example.com", context={"admin_note": ""})
    assert "Note from the team" not in mock_send.call_args.kwargs["body"]

    mock_send.reset_mock()
    send_account_email(AccountEmail.REFERRAL_REJECTED, "u@example.com", context={"admin_note": "Try again next year"})
    assert "Try again next year" in mock_send.call_args.kwargs["body"]


@patch("accounts.tasks.email.send_email")
def test_referral_application_received_requires_code(mock_send: MagicMock) -> None:
    with pytest.raises(ValueError, match="code"):
        send_account_email(AccountEmail.REFERRAL_APPLICATION_RECEIVED, "u@example.com")
    send_account_email(AccountEmail.REFERRAL_APPLICATION_RECEIVED, "u@example.com", context={"code": "biagio"})
    assert "biagio" in mock_send.call_args.kwargs["body"]


_REFERRAL_CASES = [
    (AccountEmail.REFERRAL_APPLICATION_RECEIVED, {"code": "biagio"}),
    (AccountEmail.REFERRAL_INVITE, {"code": "biagio", "revenue_share_percent": "15.00", "admin_note": ""}),
    (AccountEmail.REFERRAL_ENROLLED, {"code": "biagio", "revenue_share_percent": "15.00"}),
    (AccountEmail.REFERRAL_REJECTED, {"admin_note": ""}),
]


@pytest.mark.parametrize("email_type, context", _REFERRAL_CASES)
@patch("accounts.tasks.email.send_email")
def test_subject_is_a_single_header_line(
    mock_send: MagicMock, email_type: AccountEmail, context: dict[str, str]
) -> None:
    """A trailing newline in a subject template must not reach the header (SMTP raises BadHeaderError)."""
    send_account_email(email_type, "u@example.com", token=_TOKEN, context=context)

    subject = mock_send.call_args.kwargs["subject"]
    assert subject
    assert "\n" not in subject
    assert subject == subject.strip()
