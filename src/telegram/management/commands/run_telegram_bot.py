# src/telegram/management/commands/run_telegram_bot.py

import logging
import typing as t
from argparse import ArgumentParser

from django.conf import settings
from django.core.management.base import BaseCommand

# Import handlers and middlewares
from telegram.bot import get_bot, get_dispatcher, get_storage, run_bot

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the Telegram bot."

    def add_arguments(self, parser: ArgumentParser) -> None:  # noqa: D102
        parser.add_argument(
            "--storage", type=str, help="The type of storage", default=None, choices=["memory", "redis"]
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:  # pragma: no cover
        """Invoke the bot runner."""
        # --- Bot Initialization ---
        bot = get_bot(settings.TELEGRAM_BOT_TOKEN)

        # --- Storage and Dispatcher ---
        storage = get_storage(options["storage"])

        # storage = MemoryStorage()
        dispatcher = get_dispatcher(storage)

        run_bot(bot, dispatcher)
