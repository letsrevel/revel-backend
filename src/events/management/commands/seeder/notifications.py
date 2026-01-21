"""Notification preference seeding module.

Note: Actual notifications are created via signals, so we only seed preferences here.
"""

from events.management.commands.seeder.base import BaseSeeder
from notifications.models import NotificationPreference


class NotificationSeeder(BaseSeeder):
    """Seeder for notification preferences."""

    def seed(self) -> None:
        """Seed notification preferences for users."""
        self._create_notification_preferences()

    def _create_notification_preferences(self) -> None:
        """Create notification preferences for all users.

        Note: Actual notifications are created via signals when events occur,
        so we only create preferences here.
        """
        self.log("Creating notification preferences...")

        prefs_to_create: list[NotificationPreference] = []

        for user in self.state.users:
            # Build enabled channels list
            channels: list[str] = []
            if self.random_bool(0.95):  # Almost everyone has in_app
                channels.append("in_app")
            if self.random_bool(0.7):  # 70% have email
                channels.append("email")
            if self.random_bool(0.3):  # 30% have telegram
                channels.append("telegram")

            # Digest frequency distribution
            digest_freq = self.weighted_choice(
                {
                    "immediate": 0.5,
                    "hourly": 0.1,
                    "daily": 0.3,
                    "weekly": 0.1,
                }
            )

            prefs_to_create.append(
                NotificationPreference(
                    user=user,
                    silence_all_notifications=self.random_bool(0.05),
                    enabled_channels=channels,
                    digest_frequency=digest_freq,
                    event_reminders_enabled=self.random_bool(0.8),
                )
            )

        self.batch_create(
            NotificationPreference,
            prefs_to_create,
            desc="Creating notification preferences",
        )
        self.log(f"  Created {len(prefs_to_create)} notification preferences")
