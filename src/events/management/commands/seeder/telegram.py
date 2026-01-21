"""Telegram user seeding module."""

from events.management.commands.seeder.base import BaseSeeder
from telegram.models import TelegramUser


class TelegramSeeder(BaseSeeder):
    """Seeder for Telegram users."""

    def seed(self) -> None:
        """Seed Telegram user links."""
        self._create_telegram_users()

    def _create_telegram_users(self) -> None:
        """Link some users to fake Telegram accounts."""
        self.log("Creating Telegram user links...")

        tg_users_to_create: list[TelegramUser] = []

        # ~30% of users have Telegram linked
        users_with_telegram = self.random_sample(self.state.users, int(len(self.state.users) * 0.3))

        for user in users_with_telegram:
            # Generate a fake Telegram ID (positive integer)
            telegram_id = self.random_int(100000000, 999999999)

            tg_users_to_create.append(
                TelegramUser(
                    user=user,
                    telegram_id=telegram_id,
                    telegram_username=f"tg_{user.username.split('@')[0]}" if self.random_bool(0.7) else None,
                    blocked_by_user=self.random_bool(0.05),
                    user_is_deactivated=self.random_bool(0.02),
                    was_welcomed=self.random_bool(0.9),
                )
            )

        # Use ignore_conflicts for unique telegram_id constraint
        TelegramUser.objects.bulk_create(tg_users_to_create, ignore_conflicts=True)
        self.log(f"  Created Telegram links (up to {len(tg_users_to_create)})")
