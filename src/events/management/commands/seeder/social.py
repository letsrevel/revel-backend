"""Social features seeding module - tags, follows."""

from django.contrib.contenttypes.models import ContentType

from common.models import Tag, TagAssignment
from events.management.commands.seeder.base import BaseSeeder
from events.models import Event, EventSeriesFollow, Organization, OrganizationFollow

# Tag names
TAG_NAMES = [
    "Tech",
    "Music",
    "Art",
    "Food",
    "Sports",
    "Gaming",
    "Networking",
    "Workshop",
    "Conference",
    "Social",
    "Outdoor",
    "Virtual",
    "Family-Friendly",
    "21+",
    "Free",
    "Premium",
    "Community",
    "Professional",
    "Educational",
    "Entertainment",
]


class SocialSeeder(BaseSeeder):
    """Seeder for social features."""

    def seed(self) -> None:
        """Seed social features."""
        self._create_tags()
        self._create_tag_assignments()
        self._create_organization_follows()
        self._create_event_series_follows()

    def _create_tags(self) -> None:
        """Create reusable tags."""
        self.log("Creating tags...")

        # Check for existing tags
        existing_names = set(Tag.objects.values_list("name", flat=True))

        tags_to_create = [
            Tag(
                name=name,
                description=f"Events and organizations related to {name.lower()}",
                color=f"#{self.rand.randint(0, 0xFFFFFF):06x}",
            )
            for name in TAG_NAMES
            if name not in existing_names
        ]

        if tags_to_create:
            created = self.batch_create(Tag, tags_to_create, desc="Creating tags")
            self.state.tags = list(Tag.objects.all())
            self.log(f"  Created {len(created)} tags")
        else:
            self.state.tags = list(Tag.objects.all())
            self.log(f"  Using existing {len(self.state.tags)} tags")

    def _create_tag_assignments(self) -> None:
        """Assign tags to events and organizations."""
        self.log("Creating tag assignments...")

        if not self.state.tags:
            self.log("  No tags available, skipping")
            return

        assignments_to_create: list[TagAssignment] = []

        # Get content types
        event_ct = ContentType.objects.get_for_model(Event)
        org_ct = ContentType.objects.get_for_model(Organization)

        # Tag ~60% of events with 1-4 tags each
        events_to_tag = self.random_sample(self.state.events, int(len(self.state.events) * 0.6))

        for event in events_to_tag:
            num_tags = self.random_int(1, 4)
            event_tags = self.random_sample(self.state.tags, num_tags)

            for tag in event_tags:
                assignments_to_create.append(
                    TagAssignment(
                        tag=tag,
                        content_type=event_ct,
                        object_id=event.id,
                    )
                )

        # Tag ~80% of organizations with 2-5 tags each
        orgs_to_tag = self.random_sample(self.state.organizations, int(len(self.state.organizations) * 0.8))

        for org in orgs_to_tag:
            num_tags = self.random_int(2, 5)
            org_tags = self.random_sample(self.state.tags, num_tags)

            for tag in org_tags:
                assignments_to_create.append(
                    TagAssignment(
                        tag=tag,
                        content_type=org_ct,
                        object_id=org.id,
                    )
                )

        # Use ignore_conflicts to handle potential duplicates
        TagAssignment.objects.bulk_create(assignments_to_create, ignore_conflicts=True)
        self.log(f"  Created tag assignments (up to {len(assignments_to_create)})")

    def _create_organization_follows(self) -> None:
        """Create organization follows."""
        self.log("Creating organization follows...")

        follows_to_create: list[OrganizationFollow] = []
        user_org_pairs: set[tuple[str, str]] = set()

        for org in self.state.organizations:
            # 10-50 followers per org
            num_followers = self.random_int(10, 50)
            available_users = [u for u in self.state.users if (str(u.id), str(org.id)) not in user_org_pairs]

            if not available_users:
                continue

            followers = self.random_sample(available_users, min(num_followers, len(available_users)))

            for user in followers:
                user_org_pairs.add((str(user.id), str(org.id)))

                follows_to_create.append(
                    OrganizationFollow(
                        user=user,
                        organization=org,
                        notify_new_events=self.random_bool(0.7),
                        notify_announcements=self.random_bool(0.6),
                        is_public=self.random_bool(0.5),
                        is_archived=self.random_bool(0.1),
                    )
                )

        self.batch_create(OrganizationFollow, follows_to_create, desc="Creating org follows")
        self.log(f"  Created {len(follows_to_create)} organization follows")

    def _create_event_series_follows(self) -> None:
        """Create event series follows."""
        self.log("Creating event series follows...")

        follows_to_create: list[EventSeriesFollow] = []
        user_series_pairs: set[tuple[str, str]] = set()

        for series in self.state.event_series:
            # 5-25 followers per series
            num_followers = self.random_int(5, 25)
            available_users = [u for u in self.state.users if (str(u.id), str(series.id)) not in user_series_pairs]

            if not available_users:
                continue

            followers = self.random_sample(available_users, min(num_followers, len(available_users)))

            for user in followers:
                user_series_pairs.add((str(user.id), str(series.id)))

                follows_to_create.append(
                    EventSeriesFollow(
                        user=user,
                        event_series=series,
                        notify_new_events=self.random_bool(0.8),
                        is_public=self.random_bool(0.5),
                        is_archived=self.random_bool(0.05),
                    )
                )

        self.batch_create(EventSeriesFollow, follows_to_create, desc="Creating series follows")
        self.log(f"  Created {len(follows_to_create)} event series follows")
