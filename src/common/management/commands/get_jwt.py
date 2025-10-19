"""Get JWT tokens for a specific user by email."""

import typing as t

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from ninja_jwt.tokens import RefreshToken


class Command(BaseCommand):
    """Get JWT access and refresh tokens for a specific user."""

    help = "Get JWT access and refresh tokens for a specific user by email."

    def add_arguments(self, parser: t.Any) -> None:
        """Add command arguments."""
        parser.add_argument(
            "email",
            type=str,
            help="Email address of the user",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Generate JWT tokens for the specified user."""
        email = options["email"]

        User = get_user_model()

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f'User with email "{email}" does not exist')

        # Generate tokens
        refresh = RefreshToken.for_user(user)

        self.stdout.write(self.style.SUCCESS(f"\nJWT Tokens for: {user.email}"))
        self.stdout.write(self.style.SUCCESS(f"User ID: {user.id}"))
        self.stdout.write(self.style.SUCCESS(f"Username: {user.username}"))
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Access Token:"))
        self.stdout.write(str(refresh.access_token))  # type: ignore[attr-defined]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Refresh Token:"))
        self.stdout.write(str(refresh))
        self.stdout.write("")
