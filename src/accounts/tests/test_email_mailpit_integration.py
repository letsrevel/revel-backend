"""Integration tests for the consolidated ``send_account_email`` task against a live Mailpit.

These send each account email type through the **real SMTP path** (Django's SMTP backend →
Mailpit on ``localhost:1025``) and assert via Mailpit's REST API (``localhost:8025``) that the
message actually arrived with the right subject, recipient, and rendered ``action_link`` /
context. This is the end-to-end complement to ``test_account_email_task.py`` (which asserts the
in-process render): here a real mailbox receives a real MIME message.

Excluded from normal runs and CI (``addopts = -m 'not integration'``). Run manually with Mailpit
up (``docker compose up -d mailpit``):

    pytest -m integration src/accounts/tests/test_email_mailpit_integration.py -v

Override the Mailpit API base with ``MAILPIT_API_URL`` if not on the default port.
"""

import os
import time
import typing as t
import uuid

import httpx
import pytest
from django.test import override_settings

from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings

MAILPIT_API = os.environ.get("MAILPIT_API_URL", "http://localhost:8025/api/v1")


pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def _require_mailpit() -> None:
    """Skip cleanly when Mailpit isn't running.

    Done as an autouse fixture (not an import-time ``skipif``) so the reachability probe only
    fires when these integration tests actually run — never during normal ``make test`` collection.
    """
    try:
        reachable = httpx.get(f"{MAILPIT_API}/info", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        reachable = False
    if not reachable:
        pytest.skip(f"Mailpit not reachable at {MAILPIT_API}")


# Route Django's mailer at the live Mailpit SMTP listener for the duration of these tests,
# regardless of the ambient .env (EMAIL_DRY_RUN, console backend, etc.).
_smtp_to_mailpit = override_settings(
    EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
    EMAIL_HOST="localhost",
    EMAIL_PORT=1025,
    EMAIL_USE_TLS=False,
    EMAIL_USE_SSL=False,
)


def _wait_for_message(to_address: str, *, attempts: int = 20, delay: float = 0.25) -> dict[str, t.Any]:
    """Poll Mailpit until exactly one message addressed to ``to_address`` shows up; return its detail."""
    for _ in range(attempts):
        resp = httpx.get(f"{MAILPIT_API}/search", params={"query": f"to:{to_address}"}, timeout=5.0)
        resp.raise_for_status()
        messages = resp.json()["messages"]
        if messages:
            detail = httpx.get(f"{MAILPIT_API}/message/{messages[0]['ID']}", timeout=5.0)
            detail.raise_for_status()
            return t.cast(dict[str, t.Any], detail.json())
        time.sleep(delay)
    raise AssertionError(f"no Mailpit message delivered to {to_address} within {attempts * delay:.1f}s")


def _enable_live_emails() -> str:
    """Set the solo SiteSettings so recipients aren't catch-all-rewritten; return frontend_base_url."""
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save(update_fields=["live_emails"])
    return t.cast(str, site.frontend_base_url)


def _unique_recipient() -> str:
    return f"acct-email-it-{uuid.uuid4().hex}@example.com"


_TOKEN = "mailpit-it-token-abc123"  # noqa: S105 — not a real secret


def _link_cases() -> list[t.Any]:
    """Link-bearing emails: (email_type, path that must appear as action_link)."""
    return [
        pytest.param(AccountEmail.VERIFICATION, "/login/confirm-email?token=", id="verification"),
        pytest.param(AccountEmail.ACTIVATION, "/login/reset-password?token=", id="activation"),
        pytest.param(AccountEmail.PASSWORD_RESET, "/login/reset-password?token=", id="password_reset"),
        pytest.param(
            AccountEmail.CHANGE_CONFIRMATION, "/account/confirm-email-change?token=", id="change_confirmation"
        ),
        pytest.param(AccountEmail.DELETION, "/account/confirm-deletion?token=", id="deletion"),
    ]


@_smtp_to_mailpit
@pytest.mark.parametrize("email_type, link_path", _link_cases())
def test_link_email_delivered_to_mailpit(email_type: AccountEmail, link_path: str) -> None:
    base_url = _enable_live_emails()
    to = _unique_recipient()
    expected_link = f"{base_url}{link_path}{_TOKEN}"

    send_account_email(email_type, to, token=_TOKEN)

    msg = _wait_for_message(to)
    assert msg["To"][0]["Address"] == to
    assert msg["Subject"].strip()  # a real, rendered subject line
    # The action link must appear in both the text and HTML parts.
    assert expected_link in msg["Text"]
    assert expected_link in msg["HTML"]


@_smtp_to_mailpit
def test_change_notice_delivered_to_mailpit() -> None:
    base_url = _enable_live_emails()
    to = _unique_recipient()
    masked = "n***@example.com"

    send_account_email(AccountEmail.CHANGE_NOTICE, to, context={"masked_new_email": masked})

    msg = _wait_for_message(to)
    assert msg["To"][0]["Address"] == to
    assert msg["Subject"].strip()
    assert masked in msg["Text"]
    assert masked in msg["HTML"]
    # frontend_base_url is injected into every body; this template renders it.
    assert base_url in msg["Text"]


@_smtp_to_mailpit
@pytest.mark.parametrize(
    "email_type",
    [
        pytest.param(AccountEmail.CHANGE_COMPLETED_OLD, id="completed_old"),
        pytest.param(AccountEmail.CHANGE_COMPLETED_NEW, id="completed_new"),
    ],
)
def test_change_completed_delivered_to_mailpit(email_type: AccountEmail) -> None:
    _enable_live_emails()
    to = _unique_recipient()
    old_email, new_email = "old-addr@example.com", "new-addr@example.com"

    send_account_email(email_type, to, context={"old_email": old_email, "new_email": new_email})

    msg = _wait_for_message(to)
    assert msg["To"][0]["Address"] == to
    assert msg["Subject"].strip()
    # Both addresses are referenced in the completion bodies.
    assert old_email in msg["Text"]
    assert new_email in msg["Text"]
