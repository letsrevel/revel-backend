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
            self.stdout.write("No superuser found", self.style.WARNING)

        refresh = RefreshToken.for_user(superuser)  # type: ignore[arg-type]

        self.stdout.write(f"Superuser Access: {str(refresh.access_token)}", self.style.SUCCESS)  # type: ignore[attr-defined]
        self.stdout.write(f"Superuser Refresh: {str(refresh)}", self.style.SUCCESS)

        org_alpha_owner = User.objects.get(username="org-alpha-owner@example.com")
        refresh = RefreshToken.for_user(org_alpha_owner)
        self.stdout.write(f"Org Alpha Owner: {str(refresh.access_token)}", self.style.SUCCESS)  # type: ignore[attr-defined]
        self.stdout.write(f"Org Alpha Refresh: {str(refresh)}", self.style.SUCCESS)

        random_user = User.objects.get(username="random-user@example.com")
        refresh = RefreshToken.for_user(random_user)
        self.stdout.write(f"Random User: {str(refresh.access_token)}", self.style.SUCCESS)  # type: ignore[attr-defined]
        self.stdout.write(f"Random User Refresh: {str(refresh)}", self.style.SUCCESS)
