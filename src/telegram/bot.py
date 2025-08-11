import asyncio
import logging
import signal
import typing as t

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from django.conf import settings
from redis.asyncio import Redis

from telegram import commands
from telegram.middleware import UserMiddleware

# Import handlers and middlewares
from telegram.routers import admin, common, events, preferences

logger = logging.getLogger(__name__)


def get_bot(token: str | None = None) -> Bot:
    """Create a telegram bot instance."""
    return Bot(
        token=token or settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def get_dispatcher(storage: RedisStorage | MemoryStorage) -> Dispatcher:
    """Instantiate a telegram bot dispatcher."""
    dp = Dispatcher(storage=storage)
    dp.update.middleware(UserMiddleware())

    # --- Routers ---
    dp.include_router(common.router)
    dp.include_router(preferences.router)
    dp.include_router(admin.router)
    dp.include_router(events.router)

    return dp


def get_storage(storage: t.Literal["memory", "redis"] | None = None) -> RedisStorage | MemoryStorage:
    """Gets the storage for FSM."""
    if storage is None:
        return MemoryStorage() if settings.DEBUG else _get_redis_storage()
    if storage == "memory":
        return MemoryStorage()
    return _get_redis_storage()


def _get_redis_storage() -> RedisStorage:
    redis = Redis.from_url(settings.AIOGRAM_REDIS_URL)
    return RedisStorage(redis=redis)


def run_bot(bot: Bot, dispatcher: Dispatcher) -> None:
    """Run the bot."""

    async def run() -> None:
        """Run the bot loop."""
        # Set the commands in the telegram menu
        logger.debug("Setting Bot commands.")
        await commands.set_commands(bot)

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        # Start polling
        logger.debug("Ensure no webhook is set.")
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            logger.debug("Starting bot.")
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            if settings.DEBUG and isinstance(dispatcher.storage, RedisStorage):
                logger.debug("Stopping flushing Redis Storage.")
                await dispatcher.storage.redis.flushall()  # Clear Redis storage when in DEBUG
            logger.debug("Stopping bot.")
            await dispatcher.storage.close()
            await bot.session.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.exception(f"An error occurred: {e}")
