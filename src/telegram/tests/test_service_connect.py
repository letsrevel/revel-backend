# src/telegram/tests/test_service_connect.py
"""Tests for telegram.service.connect_accounts."""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from telegram import service
from telegram.models import AccountOTP, TelegramUser

pytestmark = pytest.mark.django_db


def test_connect_accounts_escapes_display_name_in_welcome_message(
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The HTML-mode welcome message must not carry raw markup from the user's name."""
    user = RevelUser.objects.create_user(
        username="evil", email="evil@example.com", password="pw", first_name="<b>X</b>"
    )
    tg_user = TelegramUser.objects.create(telegram_id=987654321, telegram_username="evil_tg")
    otp = AccountOTP.objects.create(tg_user=tg_user)

    with patch.object(service, "send_message_task") as task:
        with django_capture_on_commit_callbacks(execute=True):
            service.connect_accounts(user, otp.otp)

    message = task.delay.call_args.kwargs["message"]
    assert "&lt;b&gt;X&lt;/b&gt;" in message
    assert "<b>X</b>" not in message
