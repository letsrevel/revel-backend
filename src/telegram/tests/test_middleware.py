# src/telegram/tests/test_middleware.py

import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiogram.types import User as AiogramUser
from aiogram.types.chat import Chat
from django.utils import timezone

from accounts.models import RevelUser
from telegram.middleware import UserMiddleware
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def middleware() -> UserMiddleware:
    """Fixture for the UserMiddleware instance."""
    return UserMiddleware()


@pytest.mark.asyncio
async def test_user_middleware_existing_user(
    middleware: UserMiddleware, django_user: RevelUser, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware correctly fetches an existing user."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "user" in data
    assert data["user"] == django_user  # type: ignore[comparison-overlap]
    assert await TelegramUser.objects.filter(telegram_id=aiogram_user.id, user=django_user).aexists()


@pytest.mark.asyncio
async def test_user_middleware_no_tg_user_error(
    middleware: UserMiddleware, django_user: RevelUser, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware correctly fetches an existing user."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data: dict[str, t.Any] = {}

    with pytest.raises(Exception):
        await middleware(handler, event, data)


@pytest.mark.asyncio
async def test_user_middleware_new_user(middleware: UserMiddleware, aiogram_user: AiogramUser) -> None:
    """Test that the middleware correctly creates a new user."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    assert await RevelUser.objects.acount() == 0

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "user" in data
    assert await RevelUser.objects.acount() == 1
    assert await TelegramUser.objects.acount() == 1
    new_django_user = data["user"]
    assert new_django_user.username == aiogram_user.username
    assert new_django_user.telegram_user.telegram_id == aiogram_user.id  # type: ignore[attr-defined]
    await new_django_user.adelete()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_user_middleware_new_user_existing_username(
    middleware: UserMiddleware, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware correctly creates a new user."""
    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    existing_user = await RevelUser.objects.acreate_user(username=aiogram_user.username)  # type: ignore[arg-type]

    assert await RevelUser.objects.acount() == 1

    await middleware(handler, event, data)

    handler.assert_awaited_once()
    assert "user" in data
    assert await RevelUser.objects.acount() == 2
    assert await TelegramUser.objects.acount() == 1
    new_django_user = data["user"]
    assert new_django_user.username == aiogram_user.username + "_1"  # type: ignore[operator]
    assert new_django_user.telegram_user.telegram_id == aiogram_user.id  # type: ignore[attr-defined]
    await new_django_user.adelete()  # type: ignore[attr-defined]
    await existing_user.adelete()


@pytest.mark.asyncio
async def test_user_middleware_inactive_user_raises_error(
    middleware: UserMiddleware, django_inactive_user: RevelUser, aiogram_user: AiogramUser
) -> None:
    """Test that the middleware raises an exception for an inactive Django user."""

    handler = AsyncMock()
    event = Message(message_id=1, date=timezone.now(), chat=MagicMock(spec=Chat), text="test")
    data = {"event_from_user": aiogram_user}

    with pytest.raises(Exception, match="Django User is inactive."):
        await middleware(handler, event, data)

    handler.assert_not_awaited()
