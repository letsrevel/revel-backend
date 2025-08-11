# src/telegram/tests/test_models.py
import pytest

from accounts.models import RevelUser
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db


def test_telegram_user_creation(django_user: RevelUser) -> None:
    """Test that a TelegramUser can be created successfully."""
    tg_user = TelegramUser.objects.get(user=django_user)
    assert tg_user.telegram_id is not None
    assert tg_user.user == django_user


def test_active_users_manager_method() -> None:
    """Test the active_users manager method."""

    # Create an active user
    active_user_obj = RevelUser.objects.create_user(username="active_user", password="password", is_active=True)
    TelegramUser.objects.create(user=active_user_obj, telegram_id=1, blocked_by_user=False, user_is_deactivated=False)

    # Create an inactive user (blocked)
    blocked_user_obj = RevelUser.objects.create_user(username="blocked_user", password="password", is_active=True)
    TelegramUser.objects.create(user=blocked_user_obj, telegram_id=2, blocked_by_user=True)

    # Create an inactive django user
    inactive_django_user = RevelUser.objects.create_user(
        username="inactive_django", password="password", is_active=False
    )
    TelegramUser.objects.create(user=inactive_django_user, telegram_id=3)

    active_users = TelegramUser.objects.active_users()
    assert active_users.count() == 1
    user = active_users.first()
    assert user is not None
