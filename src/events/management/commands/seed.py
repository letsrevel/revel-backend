# src/common/management/commands/seed.py

import random
import re
import time
import typing as t
from datetime import timedelta

from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify  # <-- IMPORT THE FIX
from faker import Faker
from tqdm import tqdm, trange

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    EventSeries,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    Ticket,
)

# --- Configuration ---
NUM_ORGANIZATIONS = 100
NUM_EVENTS_PER_ORG = 1000
NUM_USERS = 1_000_000
MEMBER_RATIO = 0.5
MAX_STAFF_PER_ORG = 10
MAX_SERIES_PER_ORG = 10
MAX_MEMBERSHIPS_PER_USER = 3
PASSWORD = "password"

ModelType = t.TypeVar("ModelType", bound=models.Model)


class Command(BaseCommand):
    help = "Seeds the database with a large, realistic set of reproducible data."

    def add_arguments(self, parser: t.Any) -> None:
        """Add arguments."""
        parser.add_argument(
            "--seed",
            type=int,
            help="The random seed to use for generating data.",
            required=True,
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Handle."""
        seed = options["seed"]
        self.stdout.write(self.style.WARNING(f"Using random seed: {seed}"))
        self.rand = random.Random(seed)
        self.faker = Faker()
        self.faker.seed_instance(seed)

        confirmation = input(
            self.style.ERROR(
                "This command will DELETE ALL existing data for Users, Events, Orgs, etc. "
                "Are you sure you want to continue? (yes/no): "
            )
        )
        if confirmation.lower() != "yes":
            raise CommandError("Seeding cancelled by user.")

        start_time = time.time()

        with transaction.atomic():
            self._clear_data()
            self._create_superuser()
            users = self._create_users()
            organizations = self._create_organizations(users)
            self._link_staff_and_members(organizations, users)
            event_series = self._create_event_series(organizations)
            events = self._create_events(organizations, event_series)
            self._create_interactions(events, users)

        end_time = time.time()
        self.stdout.write(self.style.SUCCESS(f"Database seeding complete in {end_time - start_time:.2f} seconds."))

    def _clear_data(self) -> None:
        self.stdout.write("Deleting existing data...")
        # Delete in an order that respects foreign key constraints
        Ticket.objects.all().delete()
        EventRSVP.objects.all().delete()
        EventInvitation.objects.all().delete()
        Event.objects.all().delete()
        EventSeries.objects.all().delete()
        OrganizationStaff.objects.all().delete()
        OrganizationMember.objects.all().delete()
        Organization.objects.all().delete()
        RevelUser.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Data deleted."))

    def _create_superuser(self) -> None:
        default_username, default_password, default_email = "admin@revel.io", "password", "admin@revel.io"
        username = config("DEFAULT_SUPERUSER_USERNAME", default=default_username)
        password = config("DEFAULT_SUPERUSER_PASSWORD", default=default_password)
        email = config("DEFAULT_SUPERUSER_EMAIL", default=default_email)

        if RevelUser.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f"Superuser '{username}' already exists."))
        else:
            RevelUser.objects.create_superuser(username=username, password=password, email=email)
            self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' created successfully."))

            if password == default_password:
                self.stdout.write(
                    self.style.WARNING("The default password is being used. Please change it immediately.")
                )

    def _batch_create(self, model: t.Any, objects: list[ModelType]) -> list[ModelType]:
        """Helper to bulk_create in batches to save memory."""
        BATCH_SIZE = 500
        created_objects = []
        for i in trange(0, len(objects), BATCH_SIZE, desc=f"Batch creating {model.__name__}s"):
            batch = objects[i : i + BATCH_SIZE]
            created_objects.extend(model.objects.bulk_create(batch, BATCH_SIZE))
        return created_objects

    def _create_users(self) -> list[RevelUser]:
        self.stdout.write(f"Preparing {NUM_USERS} users...")

        self.stdout.write("Pre-hashing the common password...")
        temp_user = RevelUser()
        temp_user.set_password(PASSWORD)
        hashed_password = temp_user.password
        self.stdout.write(self.style.SUCCESS("Password hashed."))

        users_to_create = []

        # Owners
        for i in trange(NUM_ORGANIZATIONS, desc="Preparing organization owners"):
            users_to_create.append(
                RevelUser(
                    username=f"owner_org_{i + 1}@example.com",
                    email=f"owner_org_{i + 1}@example.com",
                    password=hashed_password,
                )
            )

        # Staff - create a variable number per org
        for i in trange(NUM_ORGANIZATIONS, desc="Preparing organization staff"):
            num_staff = self.rand.randint(0, MAX_STAFF_PER_ORG)
            for j in range(num_staff):
                users_to_create.append(
                    RevelUser(
                        username=f"staff_{j + 1}_org_{i + 1}@example.com",
                        email=f"staff_{j + 1}_org_{i + 1}@example.com",
                        password=hashed_password,
                    )
                )

        # Members and Random users
        num_other_users = NUM_USERS - len(users_to_create)
        num_members = int(num_other_users * MEMBER_RATIO)
        for i in trange(num_other_users, desc="Preparing other users"):
            email = f"member_{i + 1}@example.com" if i < num_members else f"random_{i + 1 - num_members}@example.com"
            users_to_create.append(RevelUser(username=email, email=email, password=hashed_password))

        self.stdout.write("Creating users in database...")
        return self._batch_create(RevelUser, users_to_create)

    def _create_organizations(self, users: list[RevelUser]) -> list[Organization]:
        self.stdout.write(f"Creating {NUM_ORGANIZATIONS} organizations...")
        owner_users = sorted([u for u in users if u.username.startswith("owner_")], key=lambda u: u.username)
        orgs_to_create = [
            Organization(
                name=f"Org {i + 1}",
                slug=f"org-{i + 1}",
                owner=owner_users[i],
                visibility=self.rand.choice(list(Organization.Visibility)),
                description=self.faker.bs(),
            )
            for i in range(NUM_ORGANIZATIONS)
        ]
        return Organization.objects.bulk_create(orgs_to_create)

    def _link_staff_and_members(self, organizations: list[Organization], users: list[RevelUser]) -> None:
        self.stdout.write("Linking staff and members to organizations...")
        all_staff_users = {u.username: u for u in users if u.username.startswith("staff_")}
        member_users = [u for u in users if u.username.startswith("member_")]

        staff_links = []
        member_links = []

        staff_pattern = re.compile(r"staff_(\d+)_org_(\d+)@example\.com")
        for staff_username, staff_user in tqdm(all_staff_users.items(), desc="Linking staff to their orgs"):
            match = staff_pattern.match(staff_username)
            if match:
                org_index = int(match.group(2)) - 1
                if 0 <= org_index < len(organizations):
                    org = organizations[org_index]
                    perms = {field: self.rand.choice([True, False]) for field in PermissionMap.model_fields}
                    staff_links.append(
                        OrganizationStaff(
                            organization=org,
                            user=staff_user,
                            permissions=PermissionsSchema(default=PermissionMap(**perms)).model_dump(mode="json"),
                        )
                    )

        for user in tqdm(member_users, desc="Linking members"):
            num_memberships = self.rand.randint(1, MAX_MEMBERSHIPS_PER_USER)
            member_orgs = self.rand.sample(organizations, num_memberships)
            for org in member_orgs:
                member_links.append(OrganizationMember(organization=org, user=user))

        self._batch_create(OrganizationStaff, staff_links)
        self._batch_create(OrganizationMember, member_links)

    def _create_event_series(self, organizations: list[Organization]) -> list[EventSeries]:
        self.stdout.write("Creating event series...")
        all_series = []
        for org in tqdm(organizations, desc="Preparing event series"):
            for i in range(self.rand.randint(0, MAX_SERIES_PER_ORG)):
                all_series.append(
                    EventSeries(
                        organization=org,
                        name=f"{org.name} Series {i + 1}",
                        slug=f"{org.slug}-series-{i + 1}",
                        description=self.faker.catch_phrase(),
                    )
                )
        return self._batch_create(EventSeries, all_series)

    def _create_events(self, organizations: list[Organization], all_series: list[EventSeries]) -> list[Event]:
        self.stdout.write(f"Creating {NUM_ORGANIZATIONS * NUM_EVENTS_PER_ORG} events...")
        events_to_create = []
        now = timezone.now()
        event_counter = 0

        for org in tqdm(organizations, desc="Preparing events"):
            org_series = [s for s in all_series if s.organization_id == org.id]
            for _ in range(NUM_EVENTS_PER_ORG):
                event_counter += 1

                event_type = self.rand.choice(list(Event.EventType))
                visibility = self.rand.choice(list(Event.Visibility))
                status = self.rand.choice(list(Event.EventStatus))
                requires_ticket = self.rand.choice([True, False])
                event_date = now + timedelta(days=self.rand.randint(-10, 90))

                meaningful_name = (
                    f"{self.faker.company()} {event_counter} "
                    f"("
                    f"Type={event_type.value}, "
                    f"Visibility={visibility.value}, "
                    f"Status={status.value}, "
                    f"RequiresTicket={requires_ticket}"
                    f")"
                )

                generated_slug = slugify(meaningful_name)

                events_to_create.append(
                    Event(
                        organization=org,
                        name=meaningful_name,
                        slug=generated_slug,  # <-- EXPLICITLY SET THE SLUG
                        event_type=event_type,
                        visibility=visibility,
                        status=status,
                        requires_ticket=requires_ticket,
                        start=event_date,
                        address=self.faker.address(),
                        max_attendees=self.rand.choice([0, 50, 100, 200, 500]),
                        event_series=self.rand.choice(org_series) if org_series else None,
                    )
                )
        return self._batch_create(Event, events_to_create)

    def _create_interactions(self, events: list[Event], users: list[RevelUser]) -> None:
        self.stdout.write("Creating invitations, tickets, and RSVPs...")
        invitations = []
        tickets = []
        rsvps = []
        random_users_sample = self.rand.sample(users, k=min(len(users), 20000))

        for event in tqdm(events, desc="Preparing interactions"):
            if self.rand.random() > 0.1:
                continue

            event_users = self.rand.sample(random_users_sample, k=self.rand.randint(5, 50))

            for user in event_users:
                if event.visibility == Event.Visibility.PRIVATE:
                    invitations.append(
                        EventInvitation(
                            event=event,
                            user=user,
                            waives_questionnaire=self.rand.choice([True, False]),
                            waives_purchase=self.rand.choice([True, False]),
                            overrides_max_attendees=self.rand.choice([True, False]),
                        )
                    )

                if event.requires_ticket:
                    if self.rand.random() > 0.5:
                        tickets.append(
                            Ticket(
                                event=event,
                                user=user,
                                status=self.rand.choice(list(Ticket.TicketStatus)),
                                guest_name=user.get_display_name(),
                            )
                        )
                else:
                    if self.rand.random() > 0.5:
                        rsvps.append(
                            EventRSVP(event=event, user=user, status=self.rand.choice(list(EventRSVP.RsvpStatus)))
                        )

        self._batch_create(EventInvitation, invitations)
        self._batch_create(Ticket, tickets)
        self._batch_create(EventRSVP, rsvps)
