"""Interaction seeding module - invitations, RSVPs, waitlist, potluck, blacklist."""

from events.management.commands.seeder.base import BaseSeeder
from events.models import (
    Blacklist,
    EventInvitation,
    EventInvitationRequest,
    EventRSVP,
    EventToken,
    EventWaitList,
    PendingEventInvitation,
    PotluckItem,
    WhitelistRequest,
)


class InteractionSeeder(BaseSeeder):
    """Seeder for event interactions."""

    def seed(self) -> None:
        """Seed all interaction types."""
        self._create_invitations()
        self._create_pending_invitations()
        self._create_invitation_requests()
        self._create_rsvps()
        self._create_waitlist()
        self._create_potluck_items()
        self._create_event_tokens()
        self._create_blacklist()
        self._create_whitelist_requests()

    def _create_invitations(self) -> None:
        """Create EventInvitations for private events."""
        self.log("Creating event invitations...")

        invitations_to_create: list[EventInvitation] = []
        user_event_pairs: set[tuple[str, str]] = set()

        for event in self.state.private_events:
            # 5-30 invitations per private event
            num_invites = self.random_int(5, 30)
            available_users = [u for u in self.state.users if (str(u.id), str(event.id)) not in user_event_pairs]

            if not available_users:
                continue

            invited_users = self.random_sample(available_users, min(num_invites, len(available_users)))

            event_tiers = self.state.ticket_tiers.get(event.id, [])

            for user in invited_users:
                user_event_pairs.add((str(user.id), str(event.id)))

                invitations_to_create.append(
                    EventInvitation(
                        event=event,
                        user=user,
                        waives_questionnaire=self.random_bool(0.3),
                        waives_purchase=self.random_bool(0.2),
                        overrides_max_attendees=self.random_bool(0.1),
                        waives_membership_required=self.random_bool(0.2),
                        waives_rsvp_deadline=self.random_bool(0.1),
                        waives_apply_deadline=self.random_bool(0.1),
                        custom_message=self.faker.sentence() if self.random_bool(0.3) else None,
                        tier=self.random_choice(event_tiers) if event_tiers else None,
                    )
                )

        self.batch_create(EventInvitation, invitations_to_create, desc="Creating invitations")
        self.log(f"  Created {len(invitations_to_create)} invitations")

    def _create_pending_invitations(self) -> None:
        """Create PendingEventInvitations for unregistered emails."""
        self.log("Creating pending invitations...")

        pending_to_create: list[PendingEventInvitation] = []
        email_event_pairs: set[tuple[str, str]] = set()

        for event in self.state.private_events[:50]:  # Limit for performance
            # 2-10 pending invitations per event
            num_pending = self.random_int(2, 10)

            for _ in range(num_pending):
                email = self.faker.email()

                if (email, str(event.id)) in email_event_pairs:
                    continue

                email_event_pairs.add((email, str(event.id)))

                pending_to_create.append(
                    PendingEventInvitation(
                        event=event,
                        email=email,
                        waives_questionnaire=self.random_bool(0.3),
                        waives_purchase=self.random_bool(0.2),
                        custom_message=self.faker.sentence() if self.random_bool(0.3) else None,
                    )
                )

        self.batch_create(
            PendingEventInvitation,
            pending_to_create,
            desc="Creating pending invitations",
        )
        self.log(f"  Created {len(pending_to_create)} pending invitations")

    def _create_invitation_requests(self) -> None:
        """Create EventInvitationRequests for events that accept them."""
        self.log("Creating invitation requests...")

        requests_to_create: list[EventInvitationRequest] = []
        user_event_pairs: set[tuple[str, str]] = set()

        accepting_events = [e for e in self.state.events if e.accept_invitation_requests]

        for event in accepting_events:
            # 3-15 requests per event
            num_requests = self.random_int(3, 15)
            available_users = [
                u for u in self.state.regular_users if (str(u.id), str(event.id)) not in user_event_pairs
            ]

            if not available_users:
                continue

            requesting_users = self.random_sample(available_users, min(num_requests, len(available_users)))

            for user in requesting_users:
                user_event_pairs.add((str(user.id), str(event.id)))

                status = self.weighted_choice(
                    {
                        "pending": 0.5,
                        "approved": 0.35,
                        "rejected": 0.15,
                    }
                )

                requests_to_create.append(
                    EventInvitationRequest(
                        event=event,
                        user=user,
                        status=status,
                        message=self.faker.sentence() if self.random_bool(0.6) else "",
                    )
                )

        self.batch_create(
            EventInvitationRequest,
            requests_to_create,
            desc="Creating invitation requests",
        )
        self.log(f"  Created {len(requests_to_create)} invitation requests")

    def _create_rsvps(self) -> None:
        """Create RSVPs for non-ticketed events."""
        self.log("Creating RSVPs...")

        rsvps_to_create: list[EventRSVP] = []
        user_event_pairs: set[tuple[str, str]] = set()

        for event in self.state.non_ticketed_events:
            # 5-50 RSVPs per event
            num_rsvps = self.random_int(5, 50)
            available_users = [u for u in self.state.users if (str(u.id), str(event.id)) not in user_event_pairs]

            if not available_users:
                continue

            rsvp_users = self.random_sample(available_users, min(num_rsvps, len(available_users)))

            for user in rsvp_users:
                user_event_pairs.add((str(user.id), str(event.id)))

                status = self.weighted_choice(
                    {
                        "yes": 0.7,
                        "maybe": 0.15,
                        "no": 0.15,
                    }
                )

                rsvps_to_create.append(
                    EventRSVP(
                        event=event,
                        user=user,
                        status=status,
                    )
                )

        self.batch_create(EventRSVP, rsvps_to_create, desc="Creating RSVPs")
        self.log(f"  Created {len(rsvps_to_create)} RSVPs")

    def _create_waitlist(self) -> None:
        """Create waitlist entries for waitlist-enabled events."""
        self.log("Creating waitlist entries...")

        waitlist_to_create: list[EventWaitList] = []
        user_event_pairs: set[tuple[str, str]] = set()

        for event in self.state.waitlist_events:
            # 3-20 waitlist entries per event
            num_waitlist = self.random_int(3, 20)
            available_users = [u for u in self.state.users if (str(u.id), str(event.id)) not in user_event_pairs]

            if not available_users:
                continue

            waitlist_users = self.random_sample(available_users, min(num_waitlist, len(available_users)))

            for user in waitlist_users:
                user_event_pairs.add((str(user.id), str(event.id)))

                waitlist_to_create.append(
                    EventWaitList(
                        event=event,
                        user=user,
                    )
                )

        self.batch_create(EventWaitList, waitlist_to_create, desc="Creating waitlist entries")
        self.log(f"  Created {len(waitlist_to_create)} waitlist entries")

    def _create_potluck_items(self) -> None:
        """Create potluck items for potluck-enabled events."""
        self.log("Creating potluck items...")

        items_to_create: list[PotluckItem] = []

        item_types = list(PotluckItem.ItemTypes.values)

        for event in self.state.potluck_events:
            # 5-20 items per potluck event
            num_items = self.random_int(5, 20)

            for _ in range(num_items):
                creator = self.random_choice(self.state.users)
                assignee = self.random_choice(self.state.users) if self.random_bool(0.7) else None

                items_to_create.append(
                    PotluckItem(
                        event=event,
                        created_by=creator,
                        assignee=assignee,
                        name=self.faker.word().capitalize(),
                        quantity=self.random_int(1, 10),
                        item_type=self.random_choice(item_types),
                        note=self.faker.sentence() if self.random_bool(0.3) else "",
                        is_suggested=self.random_bool(0.4),
                    )
                )

        self.batch_create(PotluckItem, items_to_create, desc="Creating potluck items")
        self.log(f"  Created {len(items_to_create)} potluck items")

    def _create_event_tokens(self) -> None:
        """Create event tokens."""
        self.log("Creating event tokens...")

        tokens_to_create: list[EventToken] = []

        # ~30% of events get tokens
        events_with_tokens = self.random_sample(self.state.events, int(len(self.state.events) * 0.3))

        for event in events_with_tokens:
            # 1-3 tokens per event
            num_tokens = self.random_int(1, 3)
            event_tiers = self.state.ticket_tiers.get(event.id, [])

            for _ in range(num_tokens):
                tokens_to_create.append(
                    EventToken(
                        event=event,
                        issuer=event.organization.owner,
                        grants_invitation=self.random_bool(0.8),
                        ticket_tier=self.random_choice(event_tiers) if event_tiers and self.random_bool(0.5) else None,
                    )
                )

        self.batch_create(EventToken, tokens_to_create, desc="Creating event tokens")
        self.log(f"  Created {len(tokens_to_create)} event tokens")

    def _create_blacklist(self) -> None:
        """Create blacklist entries with various match types."""
        self.log("Creating blacklist entries...")

        entries_to_create: list[Blacklist] = []

        for org in self.state.organizations:
            # 5-15 blacklist entries per org
            num_entries = self.random_int(5, 15)

            for _ in range(num_entries):
                entry_type = self.random_choice(["user", "email", "name", "phone"])

                if entry_type == "user":
                    user = self.random_choice(self.state.regular_users)
                    entries_to_create.append(
                        Blacklist(
                            organization=org,
                            user=user,
                            email=user.email,
                            reason=self.faker.sentence(),
                            created_by=org.owner,
                        )
                    )
                elif entry_type == "email":
                    entries_to_create.append(
                        Blacklist(
                            organization=org,
                            email=self.faker.email(),
                            reason=self.faker.sentence(),
                            created_by=org.owner,
                        )
                    )
                elif entry_type == "name":
                    entries_to_create.append(
                        Blacklist(
                            organization=org,
                            first_name=self.faker.first_name(),
                            last_name=self.faker.last_name(),
                            reason=self.faker.sentence(),
                            created_by=org.owner,
                        )
                    )
                else:  # phone
                    entries_to_create.append(
                        Blacklist(
                            organization=org,
                            phone_number=self.faker.phone_number()[:20],
                            reason=self.faker.sentence(),
                            created_by=org.owner,
                        )
                    )

        self.batch_create(Blacklist, entries_to_create, desc="Creating blacklist")
        self.log(f"  Created {len(entries_to_create)} blacklist entries")

    def _create_whitelist_requests(self) -> None:
        """Create whitelist requests."""
        self.log("Creating whitelist requests...")

        requests_to_create: list[WhitelistRequest] = []

        # Get blacklist entries with fuzzy matches (name-based)
        name_blacklist_entries = Blacklist.objects.filter(first_name__isnull=False).select_related("organization")[:100]

        for entry in name_blacklist_entries:
            if not self.random_bool(0.3):
                continue

            # Random user requests to be whitelisted
            user = self.random_choice(self.state.regular_users)

            status = self.weighted_choice(
                {
                    "pending": 0.5,
                    "approved": 0.3,
                    "rejected": 0.2,
                }
            )

            requests_to_create.append(
                WhitelistRequest(
                    organization=entry.organization,
                    user=user,
                    status=status,
                    message=self.faker.sentence() if self.random_bool(0.6) else "",
                )
            )

        # Use ignore_conflicts to handle duplicates
        WhitelistRequest.objects.bulk_create(requests_to_create, ignore_conflicts=True)
        self.log(f"  Created whitelist requests (up to {len(requests_to_create)})")
