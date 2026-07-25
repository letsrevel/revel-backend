"""Telegram user seeding module."""

from events.management.commands.seeder.base import BaseSeeder


class TelegramSeeder(BaseSeeder):
    """Seeder for Telegram users.

    Intentionally a no-op: a randomly generated telegram_id never corresponds to a
    real Telegram chat, so every notification dispatch to it fails with an
    AiogramError — pure noise in logs and load tests. Seeded users are left
    unlinked; real accounts get linked through the actual bot /start flow.
    """

    def seed(self) -> None:
        """Skip creating fake Telegram user links (see class docstring)."""
        self.log("Skipping Telegram user links (seeded users are never linked to real chats)")
