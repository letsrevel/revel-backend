# src/events/management/commands/bootstrap_helpers/users.py
"""User creation for bootstrap process."""

import random

import structlog

from accounts.models import RevelUser

from .base import BootstrapState

logger = structlog.get_logger(__name__)

# Pronouns list - None means no pronouns specified
PRONOUNS_OPTIONS: list[str | None] = [
    "he/him",
    "she/her",
    "they/them",
    "he/they",
    "she/they",
    "any pronouns",
    None,  # No pronouns specified
    None,  # Weighted to have more users without pronouns
]


def create_users(state: BootstrapState) -> None:
    """Create a diverse pool of users with different roles."""
    logger.info("Creating users...")

    # Use a fixed seed for consistent pronoun assignment across runs
    rng = random.Random(42)

    user_data = [
        # Organization Alpha
        ("alice.owner@example.com", "Alice Owner", "org_alpha_owner"),
        ("bob.staff@example.com", "Bob Staff", "org_alpha_staff"),
        ("charlie.member@example.com", "Charlie Member", "org_alpha_member"),
        # Organization Beta
        ("diana.owner@example.com", "Diana Owner", "org_beta_owner"),
        ("eve.staff@example.com", "Eve Staff", "org_beta_staff"),
        ("frank.member@example.com", "Frank Member", "org_beta_member"),
        # Regular attendees
        ("george.attendee@example.com", "George Attendee", "attendee_1"),
        ("hannah.attendee@example.com", "Hannah Attendee", "attendee_2"),
        ("ivan.attendee@example.com", "Ivan Attendee", "attendee_3"),
        ("julia.attendee@example.com", "Julia Attendee", "attendee_4"),
        # Multi-org user
        ("karen.multiorg@example.com", "Karen Multi", "multi_org_user"),
        # Pending/invited users
        ("leo.pending@example.com", "Leo Pending", "pending_user"),
        ("maria.invited@example.com", "Maria Invited", "invited_user"),
    ]

    for email, full_name, key in user_data:
        # Pick random pronouns (may be None for no pronouns)
        pronouns = rng.choice(PRONOUNS_OPTIONS)

        user = RevelUser.objects.create_user(
            username=email,
            password="password123",
            email=email,
            email_verified=True,
        )
        # Set first/last name if possible
        name_parts = full_name.split()
        if len(name_parts) >= 2:
            user.first_name = name_parts[0]
            user.last_name = " ".join(name_parts[1:])

        # Set pronouns if specified
        if pronouns:
            user.pronouns = pronouns

        user.save()

        state.users[key] = user

    logger.info(f"Created {len(state.users)} users")
