# src/telegram/middleware.py

import logging
import typing as t

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as AiogramUser

from accounts.models import RevelUser
from telegram.models import TelegramUser
from telegram.utils import get_or_create_tg_user

logger = logging.getLogger(__name__)


class TelegramUserMiddleware(BaseMiddleware):
    """Outer middleware: Creates/fetches TelegramUser for every update.

    Injects 'tg_user' into handler data with prefetched user relationship.
    This middleware runs on every incoming update before filters are evaluated.
    """

    async def __call__(
        self,
        handler: t.Callable[[TelegramObject, t.Dict[str, t.Any]], t.Awaitable[t.Any]],
        event: TelegramObject,
        data: t.Dict[str, t.Any],
    ) -> t.Any:
        """Inject TelegramUser into context."""
        aiogram_user: AiogramUser | None = data.get("event_from_user")

        if not aiogram_user:
            logger.error("Could not extract Telegram user from event data.")
            return None  # Stop processing

        tg_user: TelegramUser = await get_or_create_tg_user(aiogram_user)

        # Mark if bot was unblocked or user was reactivated
        if tg_user.blocked_by_user or tg_user.user_is_deactivated:
            tg_user.blocked_by_user = False
            tg_user.user_is_deactivated = False
            await tg_user.asave(update_fields=["blocked_by_user", "user_is_deactivated", "updated_at"])

        data["tg_user"] = tg_user
        return await handler(event, data)


class AuthorizationMiddleware(BaseMiddleware):
    """Inner middleware: Enforces authorization flags.

    Checks handler flags for:
    - requires_linked_user: Injects 'user' if linked, sends error if not
    - requires_superuser: Ensures user is superuser

    This middleware runs after filters pass, before the handler is invoked.
    """

    async def __call__(
        self,
        handler: t.Callable[[TelegramObject, t.Dict[str, t.Any]], t.Awaitable[t.Any]],
        event: TelegramObject,
        data: t.Dict[str, t.Any],
    ) -> t.Any:
        """Enforce authorization based on handler flags."""
        from aiogram.dispatcher.flags import get_flag

        tg_user: TelegramUser | None = data.get("tg_user")

        if not tg_user:
            logger.error("TelegramUser not found in context - check middleware order!")
            return None

        # Access flags from data dict as per aiogram documentation
        requires_linked = get_flag(data, "requires_linked_user", default=False)
        requires_superuser = get_flag(data, "requires_superuser", default=False)

        # If handler requires linked user or superuser
        if requires_linked or requires_superuser:
            if not tg_user.user_id:
                # Send helpful message
                if isinstance(event, Message):
                    await event.answer("⚠️ Please link your Revel account first using /connect command.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Please link your account first using /connect", show_alert=True)
                return None  # Stop processing

            # Inject user into data (already prefetched by get_or_create_tg_user)
            user: RevelUser = t.cast(RevelUser, tg_user.user)

            # Check superuser requirement
            if requires_superuser and not user.is_superuser:
                if isinstance(event, Message):
                    await event.answer("⚠️ This command is for administrators only.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Administrators only", show_alert=True)
                return None

            data["user"] = user

        return await handler(event, data)
