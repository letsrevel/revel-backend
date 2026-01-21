"""User preferences seeding module."""

from events.management.commands.seeder.base import BaseSeeder
from events.models import AttendeeVisibilityFlag, GeneralUserPreferences
from geo.models import City


class PreferencesSeeder(BaseSeeder):
    """Seeder for user preferences."""

    def seed(self) -> None:
        """Seed user preferences."""
        self._create_general_preferences()
        self._create_attendee_visibility_flags()

    def _create_general_preferences(self) -> None:
        """Create GeneralUserPreferences for users."""
        self.log("Creating general user preferences...")

        prefs_to_create: list[GeneralUserPreferences] = []

        # Get cities for location preferences
        cities = list(City.objects.all()[:100])

        # ~60% of users have preferences set
        users_with_prefs = self.random_sample(self.state.users, int(len(self.state.users) * 0.6))

        visibility_choices = [
            "always",
            "never",
            "to_members",
            "to_invitees",
            "to_both",
        ]

        for user in users_with_prefs:
            prefs_to_create.append(
                GeneralUserPreferences(
                    user=user,
                    city=self.random_choice(cities) if cities else None,
                    show_me_on_attendee_list=self.random_choice(visibility_choices),
                )
            )

        self.batch_create(
            GeneralUserPreferences,
            prefs_to_create,
            desc="Creating general preferences",
        )
        self.log(f"  Created {len(prefs_to_create)} general preferences")

    def _create_attendee_visibility_flags(self) -> None:
        """Create AttendeeVisibilityFlags for event attendees.

        These flags control which attendees a user can see at specific events.
        """
        self.log("Creating attendee visibility flags...")

        flags_to_create: list[AttendeeVisibilityFlag] = []

        # Sample of events for visibility flags
        sample_events = self.random_sample(self.state.events, min(100, len(self.state.events)))

        for event in sample_events:
            # Get some users for this event
            num_viewers = self.random_int(3, 10)
            num_targets = self.random_int(3, 10)

            viewers = self.random_sample(self.state.users, num_viewers)
            targets = self.random_sample(self.state.users, num_targets)

            for viewer in viewers:
                for target in targets:
                    if viewer.id == target.id:
                        continue

                    flags_to_create.append(
                        AttendeeVisibilityFlag(
                            user=viewer,
                            event=event,
                            target=target,
                            is_visible=self.random_bool(0.7),
                        )
                    )

        # Use ignore_conflicts for unique constraint
        AttendeeVisibilityFlag.objects.bulk_create(flags_to_create, ignore_conflicts=True)
        self.log(f"  Created visibility flags (up to {len(flags_to_create)})")
