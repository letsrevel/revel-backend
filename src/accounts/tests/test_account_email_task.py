"""Byte-for-byte render coverage for the consolidated ``send_account_email`` task (issue #608).

Each case independently reconstructs the subject/body/html the old per-message wrapper produced
and asserts the consolidated task renders exactly the same — locking the link paths and template
context so the consolidation is behaviour-preserving.
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
    subject = str(render_to_string(f"accounts/emails/{base}_subject.txt"))
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
