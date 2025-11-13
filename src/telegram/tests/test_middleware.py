# src/telegram/tests/test_middleware.py

import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiogram.types import User as AiogramUser
from aiogram.types.chat import Chat
from django.utils import timezone

from accounts.models import RevelUser
from telegram.middleware import TelegramUserMiddleware
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def middleware() -> TelegramUserMiddleware:
    """Fixture for the TelegramUserMiddleware instance."""
    return TelegramUserMiddleware()


@pytest.mark.asyncio
async def test_user_middleware_existing_user(
    middleware: TelegramUserMiddleware, django_user: RevelUser, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware correctly fetches an existing TelegramUser."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "tg_user" in data
    tg_user: TelegramUser = data["tg_user"]  # type: ignore[assignment]
    assert tg_user.user == django_user
    assert await TelegramUser.objects.filter(telegram_id=aiogram_user.id, user=django_user).aexists()


@pytest.mark.asyncio
async def test_user_middleware_no_event_from_user(middleware: TelegramUserMiddleware) -> None:
    """Test that middleware returns None and doesn't call handler when event_from_user is missing."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data: dict[str, t.Any] = {}

    result = await middleware(handler, event, data)

    assert result is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_middleware_new_user(middleware: TelegramUserMiddleware, aiogram_user: AiogramUser) -> None:
    """Test that the middleware creates a new TelegramUser (without linked RevelUser)."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    assert await TelegramUser.objects.acount() == 0

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "tg_user" in data
    tg_user: TelegramUser = data["tg_user"]  # type: ignore[assignment]
    assert await TelegramUser.objects.acount() == 1
    assert tg_user.user is None  # No RevelUser is created automatically
    assert tg_user.telegram_id == aiogram_user.id
    assert tg_user.telegram_username == aiogram_user.username


@pytest.mark.asyncio
async def test_user_middleware_updates_username(
    middleware: TelegramUserMiddleware, django_user: RevelUser, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware updates telegram_username if it changed."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")

    # Create TelegramUser with old username
    tg_user = await TelegramUser.objects.aget(user=django_user)
    tg_user.telegram_username = "old_username"
    await tg_user.asave()

    # Create aiogram user with new username (must create new object since username is read-only)
    new_aiogram_user = AiogramUser(
        id=aiogram_user.id, is_bot=False, first_name=aiogram_user.first_name, username="new_username"
    )
    data = {"event_from_user": new_aiogram_user}

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "tg_user" in data
    updated_tg_user: TelegramUser = data["tg_user"]  # type: ignore[assignment]
    assert updated_tg_user.telegram_username == "new_username"
