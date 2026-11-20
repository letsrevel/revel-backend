"""Bootstrap the application by creating a superuser and generating example data."""

import typing as t

from decouple import config
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Bootstrap the application by creating a superuser and generating example data."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Bootstrap the application by creating a superuser and generating example data."""
        call_command("migrate")
        User = get_user_model()
        default_username, default_password, default_email = "admin@letsrevel.io", "password", "admin@letsrevel.io"
        username = config("DEFAULT_SUPERUSER_USERNAME", default=default_username)
        password = config("DEFAULT_SUPERUSER_PASSWORD", default=default_password)
        email = config("DEFAULT_SUPERUSER_EMAIL", default=default_email)

        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f"Superuser '{username}' already exists."))
        else:
            User.objects.create_superuser(username=username, password=password, email=email)
            self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' created successfully."))

            if password == default_password:
                self.stdout.write(
                    self.style.WARNING("The default password is being used. Please change it immediately.")
                )

        # Invoke the generate_example_data command
        call_command("bootstrap_events")
        call_command("bootstrap_test_events")
        call_command("generate_test_jwts")
