# src/telegram/middlewares.py

import logging
import typing as t

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as AiogramUser
from django.conf import settings

from accounts.models import RevelUser

from .models import TelegramUser

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Gets or creates a Django User based on the Telegram User.

    Injects the Django User object into the handler context data with the key "user".
    """

    async def __call__(
        self,
        handler: t.Callable[[TelegramObject, dict[str, t.Any]], t.Awaitable[t.Any]],
        event: TelegramObject,
        data: dict[str, t.Any],
    ) -> t.Any:
        """Custom middleware to inject User into the context."""
        aiogram_user: AiogramUser | None = data.get("event_from_user")

        if not aiogram_user:
            # Cannot proceed without a Telegram user object
            logger.error("Could not extract Telegram user from event data.")
            raise Exception("Could not extract Telegram user from event data.")

        django_user: RevelUser = await self.get_or_create_user(aiogram_user)

        if not django_user.is_active:
            logger.error(f"Django User is inactive: {aiogram_user.id}")
            raise Exception("Django User is inactive.")

        data["user"] = django_user
        logger.debug(f"Injected user {django_user.username} into handler data.")
        return await handler(event, data)

    async def get_or_create_user(self, aiogram_user: AiogramUser) -> RevelUser:
        """Get or create Django User and TelegramUser profile."""
        try:
            tg_user_profile = await TelegramUser.objects.select_related("user").aget(telegram_id=aiogram_user.id)
            # Optionally update username if it changed
            if (
                aiogram_user.username and tg_user_profile.telegram_username != aiogram_user.username
            ):  # pragma: no branch
                tg_user_profile.telegram_username = aiogram_user.username
                await tg_user_profile.asave(update_fields=["telegram_username", "updated_at"])
            return tg_user_profile.user
        except TelegramUser.DoesNotExist:
            # Create Django User and TelegramUser profile
            username = aiogram_user.username or f"tg_{aiogram_user.id}"
            # Ensure username uniqueness
            base_username = username
            counter = 1
            while await RevelUser.objects.filter(username=username).aexists():
                username = f"{base_username}_{counter}"
                counter += 1

            is_superuser = aiogram_user.id in settings.TELEGRAM_SUPERUSER_IDS
            is_staff = aiogram_user.id in settings.TELEGRAM_STAFF_IDS
            django_user = await RevelUser.objects.acreate_user(
                username=username, is_staff=is_staff, is_superuser=is_superuser
            )
            logger.info(
                f"Created new Django user '{username}' for TG ID {aiogram_user.id} (is_superuser={is_superuser}, is_staff={is_staff})"  # noqa: E501
            )
            _tg_user_profile = await TelegramUser.objects.acreate(
                user=django_user,
                telegram_id=aiogram_user.id,
                telegram_username=aiogram_user.username,
            )
            logger.info(f"Created new Django user '{username}' for TG ID {aiogram_user.id}")
            return django_user
