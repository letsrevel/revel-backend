"""Generate test JWTs for development purposes."""

import typing as t

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from ninja_jwt.tokens import RefreshToken


class Command(BaseCommand):
    help = "Generate test JWTs for development purposes."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Generate example data to populate the database for development purposes."""
        self.stdout.write("Generating example JWTs...")

        User = get_user_model()
        superuser = User.objects.filter(is_superuser=True).first()
        if not superuser:
            self.stdout.write("No superuser found - skipping JWT generation", self.style.WARNING)
            return

        refresh = RefreshToken.for_user(superuser)

        self.stdout.write(f"Superuser Access: {str(refresh.access_token)}", self.style.SUCCESS)  # type: ignore[attr-defined]
        self.stdout.write(f"Superuser Refresh: {str(refresh)}", self.style.SUCCESS)
