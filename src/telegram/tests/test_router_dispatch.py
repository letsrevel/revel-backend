# src/telegram/tests/test_router_dispatch.py
"""Dispatcher-level tests: feed real updates so router middleware wiring is exercised (#1000)."""

import datetime
import typing as t
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as AiogramUser

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel
from notifications.models import NotificationPreference
from telegram.middleware import AuthorizationMiddleware

pytestmark = pytest.mark.django_db

AUTHORIZATION_FLAGS = {"requires_linked_user", "requires_superuser", "staff_permission"}


def _command_update(aiogram_user: AiogramUser, chat: Chat, text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.datetime.now(tz=datetime.UTC),
            chat=chat,
            from_user=aiogram_user,
            text=text,
        ),
    )


async def _feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> list[str]:
    """Feed an update through the dispatcher and return the texts the bot sent."""
    with patch.object(Bot, "__call__", new_callable=AsyncMock) as bot_call:
        await dispatcher.feed_update(bot, update)
    methods = [arg for call in bot_call.await_args_list for arg in call.args]
    return [m.text for m in methods if isinstance(m, SendMessage)]


class TestUnsubscribeThroughDispatcher:
    @pytest.mark.asyncio
    async def test_linked_user_is_unsubscribed(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        aiogram_user: AiogramUser,
        chat: Chat,
        django_user: RevelUser,
    ) -> None:
        prefs = await NotificationPreference.objects.aget(user=django_user)
        prefs.enabled_channels = [DeliveryChannel.EMAIL, DeliveryChannel.TELEGRAM]
        await prefs.asave(update_fields=["enabled_channels"])

        sent = await _feed(dispatcher, bot, _command_update(aiogram_user, chat, "/unsubscribe"))

        assert len(sent) == 1
        assert "unsubscribed" in sent[0].lower()
        await prefs.arefresh_from_db()
        assert DeliveryChannel.TELEGRAM not in prefs.enabled_channels
        assert DeliveryChannel.EMAIL in prefs.enabled_channels

    @pytest.mark.asyncio
    async def test_unlinked_user_is_asked_to_link(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        aiogram_user: AiogramUser,
        chat: Chat,
    ) -> None:
        sent = await _feed(dispatcher, bot, _command_update(aiogram_user, chat, "/unsubscribe"))

        assert len(sent) == 1
        assert "/connect" in sent[0]


class TestUnflaggedCommonCommandsPassThrough:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/start", "/connect", "/toc", "/privacy", "/cancel"])
    async def test_unlinked_user_reaches_handler(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        aiogram_user: AiogramUser,
        chat: Chat,
        command: str,
    ) -> None:
        sent = await _feed(dispatcher, bot, _command_update(aiogram_user, chat, command))

        assert len(sent) == 1
        assert "link your Revel account first" not in sent[0]


def _iter_routers(router: Router) -> t.Iterator[Router]:
    yield router
    for sub in router.sub_routers:
        yield from _iter_routers(sub)


def test_every_flagged_handler_has_authorization_middleware(dispatcher: Dispatcher) -> None:
    """Authorization flags are only honoured by AuthorizationMiddleware; without it they are silently ignored."""
    missing: list[str] = []
    for router in _iter_routers(dispatcher):
        for event_name, observer in router.observers.items():
            flagged = [h for h in observer.handlers if AUTHORIZATION_FLAGS & h.flags.keys()]
            if not flagged:
                continue
            middlewares = [
                m for r in router.chain_head if (obs := r.observers.get(event_name)) is not None for m in obs.middleware
            ]
            if not any(isinstance(m, AuthorizationMiddleware) for m in middlewares):
                missing.extend(f"{router.name}.{event_name}:{h.callback.__name__}" for h in flagged)

    assert not missing, f"Flagged handlers without AuthorizationMiddleware: {missing}"
